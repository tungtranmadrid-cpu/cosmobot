import ccxt
import csv
import sys
import time
import os
from datetime import datetime
from dotenv import dotenv_values

if os.name == "nt":
    os.system("chcp 65001 > nul")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ═══════════════════════════════════════════════════════════════
#  LOAD .env
# ═══════════════════════════════════════════════════════════════

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
env = dotenv_values(ENV_PATH)

API_KEY = env.get("API_KEY", "")
SECRET_KEY = env.get("SECRET_KEY", "")
IS_TESTNET = env.get("TESTNET", "true").strip().lower() in ("true", "1", "yes")
TRADE_AMOUNT = float(env.get("TRADE_AMOUNT", "0"))

if not API_KEY or not SECRET_KEY:
    print("\033[91m  ✗ Thiếu API_KEY hoặc SECRET_KEY trong .env\033[0m")
    sys.exit(1)

BUY_RETRY_TIMEOUT = float(env.get("BUY_RETRY_TIMEOUT", "5"))
POLL_INTERVAL = 0.5
COOLDOWN = 0.5

# SPREAD_<PAIR>=<pct> — giá bán = giá mua × (1 + pct/100)
SPREAD_MAP = {}
for key, val in env.items():
    if key.startswith("SPREAD_"):
        pair = key[7:]
        try:
            SPREAD_MAP[pair.upper()] = float(val)
        except ValueError:
            pass

# ═══════════════════════════════════════════════════════════════

GREEN   = "\033[92m"
RED     = "\033[91m"
CYAN    = "\033[96m"
YELLOW  = "\033[93m"
WHITE   = "\033[97m"
MAGENTA = "\033[95m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
RESET   = "\033[0m"
BG_GREEN = "\033[42m"
BG_RED   = "\033[41m"

CSV_COLUMNS = [
    "cycle", "timestamp", "side", "symbol", "order_price", "filled_price",
    "amount", "filled_amount", "cost", "status", "order_id", "pnl", "duration_s", "note",
]

MAX_HISTORY = 15


def get_spread_pct(symbol):
    key = symbol.replace("/", "")
    return SPREAD_MAP.get(key)


# ═══════════════════════════════════════════════════════════════
#  EXCHANGE & HELPERS
# ═══════════════════════════════════════════════════════════════

def create_exchange():
    exchange = ccxt.binance({
        "apiKey": API_KEY,
        "secret": SECRET_KEY,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    if IS_TESTNET:
        exchange.set_sandbox_mode(True)
    return exchange


def normalize_symbol(raw_input, exchange):
    s = raw_input.strip().upper()
    if "/" in s:
        if s in exchange.markets:
            return s
        raise ValueError(f"Cặp '{s}' không tồn tại trên testnet")
    quote_currencies = [
        "USDT", "USDC", "BUSD", "FDUSD", "TUSD",
        "BTC", "ETH", "BNB", "JPY", "EUR", "GBP", "TRY", "BRL",
    ]
    for quote in quote_currencies:
        if s.endswith(quote) and len(s) > len(quote):
            candidate = f"{s[:-len(quote)]}/{quote}"
            if candidate in exchange.markets:
                return candidate
    raise ValueError(f"Không nhận dạng được '{raw_input}'. Thử 'BTC/USDT' hoặc 'BTCUSDT'.")


def fetch_bid_ask(exchange, symbol):
    ob = exchange.fetch_order_book(symbol, limit=5)
    if not ob["bids"] or not ob["asks"]:
        raise ValueError(f"Order book trống cho {symbol}")
    return ob["bids"][0][0], ob["asks"][0][0]


def auto_fmt(price):
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:,.4f}"
    if price >= 0.01:
        return f"{price:,.6f}"
    return f"{price:.8f}"


def safe_amount(exchange, symbol, amount):
    return float(exchange.amount_to_precision(symbol, amount))


def safe_price(exchange, symbol, price):
    return float(exchange.price_to_precision(symbol, price))


def get_market_limits(exchange, symbol):
    market = exchange.markets[symbol]
    min_amount = market.get("limits", {}).get("amount", {}).get("min")
    min_cost = market.get("limits", {}).get("cost", {}).get("min")
    return min_amount, min_cost


_bal_cache = {"data": None, "ts": 0}
_BAL_CACHE_TTL = 1.0

def get_free_balance(exchange, currency):
    now = time.time()
    if _bal_cache["data"] is None or (now - _bal_cache["ts"]) > _BAL_CACHE_TTL:
        _bal_cache["data"] = exchange.fetch_balance()
        _bal_cache["ts"] = now
    return float(_bal_cache["data"].get(currency, {}).get("free", 0) or 0)


def invalidate_balance_cache():
    _bal_cache["data"] = None
    _bal_cache["ts"] = 0


RATE_LIMIT_MAX = 1200

def get_rate_limit_used(exchange):
    headers = getattr(exchange, "last_response_headers", None) or {}
    used = headers.get("x-mbx-used-weight-1m") or headers.get("X-MBX-USED-WEIGHT-1M")
    order_count = headers.get("x-mbx-order-count-1m") or headers.get("X-MBX-ORDER-COUNT-1M")
    return int(used) if used else None, int(order_count) if order_count else None


def wait_for_fill_timeout(exchange, order_id, symbol, timeout):
    start = time.time()
    while time.time() - start < timeout:
        try:
            o = exchange.fetch_order(order_id, symbol)
            if o["status"] == "closed":
                return o, "filled"
            if o["status"] == "canceled":
                return o, "cancelled"
        except ccxt.OrderNotFound:
            return {"status": "canceled", "filled": 0, "id": order_id}, "cancelled"
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)
    try:
        o = exchange.fetch_order(order_id, symbol)
        if o["status"] == "closed":
            return o, "filled"
        return o, "timeout"
    except Exception:
        return {"status": "unknown", "filled": 0}, "timeout"


def wait_until_filled(exchange, order_id, symbol, state_cb=None):
    elapsed = 0.0
    last_cb = 0.0
    cb_interval = 2.0
    while True:
        try:
            o = exchange.fetch_order(order_id, symbol)
            if o["status"] == "closed":
                return o
            if o["status"] == "canceled":
                return o
        except ccxt.OrderNotFound:
            return {"status": "canceled", "filled": 0, "id": order_id}
        except Exception:
            pass

        poll_sleep = min(POLL_INTERVAL * (1 + elapsed // 120), 3.0)
        time.sleep(poll_sleep)
        elapsed += poll_sleep

        if state_cb and (elapsed - last_cb) >= cb_interval:
            state_cb(elapsed)
            last_cb = elapsed


def cancel_safe(exchange, order_id, symbol):
    try:
        exchange.cancel_order(order_id, symbol)
    except Exception:
        pass


def log(msg, color=""):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"  {DIM}{ts}{RESET}  {color}{msg}{RESET}")


# ═══════════════════════════════════════════════════════════════
#  CSV LOGGER
# ═══════════════════════════════════════════════════════════════

class TradeLogger:
    def __init__(self, symbol):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_sym = symbol.replace("/", "_")
        self.filename = f"trades_{safe_sym}_{ts}.csv"
        self.count = 0
        with open(self.filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()

    def write(self, row):
        with open(self.filename, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writerow(row)
        self.count += 1

    def close(self):
        pass


# ═══════════════════════════════════════════════════════════════
#  PROMPTS
# ═══════════════════════════════════════════════════════════════

def prompt_symbol(exchange):
    print(f"\n  {BOLD}═══ CHỌN CẶP GIAO DỊCH ═══{RESET}")
    print(f"  {DIM}Binance Testnet ({len(exchange.markets):,} cặp){RESET}")

    configured = [f"{CYAN}{k}{RESET} ({v}%)" for k, v in SPREAD_MAP.items()]
    if configured:
        print(f"  Đã cấu hình spread: {', '.join(configured)}")
    print()

    while True:
        try:
            raw = input(f"  {BOLD}▶ Nhập cặp: {RESET}").strip()
            if not raw:
                continue
            symbol = normalize_symbol(raw, exchange)
            sp = get_spread_pct(symbol)
            if sp is None:
                print(f"  {YELLOW}⚠ Chưa có SPREAD cho {symbol} trong .env{RESET}")
                custom = input(f"  {BOLD}▶ Nhập spread % (VD: 0.01): {RESET}").strip()
                try:
                    sp = float(custom)
                    key = symbol.replace("/", "")
                    SPREAD_MAP[key] = sp
                    print(f"  {GREEN}✓ Spread {symbol} = {sp}%{RESET}")
                except ValueError:
                    print(f"  {RED}Không hợp lệ. Thử lại.{RESET}")
                    continue
            else:
                print(f"  {GREEN}✓ {symbol}  |  Spread bán: {sp}%{RESET}")
            print()
            return symbol
        except ValueError as e:
            print(f"  {RED}✗ {e}{RESET}")
            if raw.strip():
                q = raw.strip().upper().replace("/", "")
                hits = [s for s in sorted(exchange.markets) if q in s.replace("/", "")][:8]
                if hits:
                    print(f"  {YELLOW}Gợi ý: {', '.join(hits)}{RESET}")
            print()


# ═══════════════════════════════════════════════════════════════
#  DISPLAY
# ═══════════════════════════════════════════════════════════════

def display(cycle, symbol, quote, base, bid, ask, state, summary, history, spread_pct,
            open_trade=None, bal_quote=0, bal_base=0, rate_info=None):
    os.system("cls" if os.name == "nt" else "clear")
    w = 76
    now = datetime.now().strftime("%H:%M:%S")
    spread = ask - bid

    unrealized = 0.0
    if open_trade:
        unrealized = (bid - open_trade["buy_price"]) * open_trade["amount"]

    realized = summary['total_pnl']
    total_all = realized + unrealized

    print()
    mode_label = "ALL-IN" if TRADE_AMOUNT <= 0 else f"FIXED {TRADE_AMOUNT}"
    net_label = "TESTNET" if IS_TESTNET else "🔴 MAINNET"
    title = f"SPREAD BOT — {mode_label} BUY@BID → SELL@(BID+SPREAD)"
    print(f"  {BOLD}╔{'═' * w}╗{RESET}")
    print(f"  {BOLD}║{title:^{w}}║{RESET}")
    print(f"  {BOLD}╚{'═' * w}╝{RESET}")
    rl_str = ""
    if rate_info:
        used_w, order_cnt = rate_info
        if used_w is not None:
            pct = used_w / RATE_LIMIT_MAX * 100
            if pct > 80:
                rl_c = RED
            elif pct > 50:
                rl_c = YELLOW
            else:
                rl_c = GREEN
            rl_str = f"  |  API: {rl_c}{used_w}/{RATE_LIMIT_MAX} ({pct:.0f}%){RESET}"
            if order_cnt is not None:
                rl_str += f"  Ord: {order_cnt}/min"
    print(f"  {DIM}{now}  |  {net_label}  |  {symbol}  |  #{cycle}  |  Spread: {spread_pct}%  |  Retry: {BUY_RETRY_TIMEOUT}s{RESET}{rl_str}")
    print()

    print(f"  ┌{'─' * w}┐")
    print(f"  │  {RED}{BOLD}BID{RESET} {CYAN}{auto_fmt(bid):>14}{RESET}       "
          f"{GREEN}{BOLD}ASK{RESET} {CYAN}{auto_fmt(ask):>14}{RESET}       "
          f"{BOLD}Spread{RESET} {YELLOW}{auto_fmt(spread):>12}{RESET}      │")
    print(f"  ├{'─' * w}┤")

    bal_str = f"{auto_fmt(bal_quote)} {quote}" if bal_quote > 0 else ""
    base_str = f"{auto_fmt(bal_base)} {base}" if bal_base > 0 else ""
    bal_parts = [x for x in [bal_str, base_str] if x]
    print(f"  │  Số dư: {CYAN}{BOLD}{' + '.join(bal_parts) if bal_parts else '—'}{RESET}"
          f"{'':>{w - 12 - len(' + '.join(bal_parts)) if bal_parts else w - 13}}│")
    print(f"  ├{'─' * w}┤")

    wins = summary['wins']
    losses = summary['losses']
    retries = summary['retries']
    r_c = GREEN if realized >= 0 else RED

    print(f"  │  Xong: {BOLD}{summary['total']}{RESET}   "
          f"Lãi: {GREEN}{wins}{RESET}   Lỗ: {RED}{losses}{RESET}   "
          f"Retry: {YELLOW}{retries}{RESET}   "
          f"PnL đã chốt: {r_c}{BOLD}{auto_fmt(realized)} {quote}{RESET}     │")

    if open_trade:
        u_c = GREEN if unrealized >= 0 else RED
        t_c = GREEN if total_all >= 0 else RED
        print(f"  │  {DIM}Lệnh #{open_trade['cycle']}: mua {auto_fmt(open_trade['buy_price'])} → "
              f"bán {auto_fmt(open_trade['sell_target'])}{RESET}   "
              f"Chưa chốt: {u_c}{BOLD}{auto_fmt(unrealized)}{RESET}   "
              f"Tổng: {t_c}{BOLD}{auto_fmt(total_all)} {quote}{RESET}  │")

    print(f"  └{'─' * w}┘")

    if history:
        print(f"\n  {BOLD}{'#':>4}  {'Giờ':<10} {'Mua @':>14} {'Bán @':>14} {'SL':>10} {'PnL':>14} {'T.gian':>8} {'':>3}{RESET}")
        print(f"  {'─' * w}")
        for h in history[-MAX_HISTORY:]:
            pnl_val = h.get("pnl", 0)
            dur = h.get("duration_s")
            dur_str = f"{dur:.0f}s" if dur is not None else ""
            if h.get("sell_price"):
                if pnl_val > 0:
                    pc, icon = GREEN, "▲"
                elif pnl_val < 0:
                    pc, icon = RED, "▼"
                else:
                    pc, icon = DIM, "─"
                sell_str = auto_fmt(h['sell_price'])
                pnl_str = auto_fmt(pnl_val)
            else:
                pc, icon = DIM, "…"
                sell_str = "chờ..."
                pnl_str = ""
            status = h.get("status", "")
            print(f"  {pc}{h['cycle']:>4}  {h['time']:<10} {auto_fmt(h['buy_price']):>14} "
                  f"{sell_str:>14} {h['amount']:>10} {pnl_str:>14} {dur_str:>8} {icon:>3} {status}{RESET}")

        completed = [h for h in history if h.get("duration_s") is not None]
        if completed:
            avg_dur = sum(h["duration_s"] for h in completed) / len(completed)
            print(f"  {'─' * w}")
            print(f"  {DIM}{'Thời gian TB:':>68} {avg_dur:.1f}s{RESET}")
        print(f"  {'─' * w}")
    else:
        print(f"\n  {DIM}Chưa có giao dịch.{RESET}")

    print(f"\n  {DIM}{state}{RESET}\n")


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
#
#  Hỗ trợ 2 mode: ALL-IN (TRADE_AMOUNT=0) hoặc FIXED (TRADE_AMOUNT>0)
#  Hỗ trợ TESTNET=true/false trong .env
#
# ═══════════════════════════════════════════════════════════════

def main():
    is_allin = TRADE_AMOUNT <= 0
    mode_str = "ALL-IN" if is_allin else f"FIXED {TRADE_AMOUNT}"
    net_str = "TESTNET" if IS_TESTNET else "MAINNET (TIỀN THẬT!)"

    print(f"\n  {BOLD}{'═' * 60}{RESET}")
    print(f"  {BOLD}  SPREAD BOT — {mode_str} BUY@BID → SELL@(BID+SPREAD%){RESET}")
    print(f"  {BOLD}{'═' * 60}{RESET}")

    if not IS_TESTNET:
        print(f"  {BG_RED}{WHITE}{BOLD} ⚠  MAINNET — GIAO DỊCH TIỀN THẬT  ⚠ {RESET}")

    if is_allin:
        print(f"  {DIM}Mode: ALL-IN — dùng toàn bộ số dư mỗi chu kỳ{RESET}")
    else:
        print(f"  {DIM}Mode: FIXED — mỗi lệnh mua tối đa {TRADE_AMOUNT} quote currency{RESET}")
    print(f"  {DIM}Mạng: {net_str}  |  Retry mua: {BUY_RETRY_TIMEOUT}s  |  Lệnh bán: chờ vô hạn{RESET}")

    if SPREAD_MAP:
        print(f"\n  {BOLD}Spread (.env):{RESET}")
        for pair, pct in SPREAD_MAP.items():
            print(f"    {CYAN}{pair}{RESET}: {pct}%")

    quote = "USDT"
    base = ""
    tlog = None

    print(f"\n  {DIM}Kết nối Binance {'Testnet' if IS_TESTNET else 'Mainnet'}...{RESET}")
    exchange = create_exchange()
    exchange.load_markets()
    print(f"  {GREEN}✓ {len(exchange.markets):,} cặp.{RESET}")

    symbol = prompt_symbol(exchange)
    quote = symbol.split("/")[1]
    base = symbol.split("/")[0]
    spread_pct = get_spread_pct(symbol)
    min_amount, min_cost = get_market_limits(exchange, symbol)

    bid, ask = fetch_bid_ask(exchange, symbol)
    q_bal = get_free_balance(exchange, quote)
    b_bal = get_free_balance(exchange, base)

    print(f"\n  {BOLD}═══ SỐ DƯ ═══{RESET}")
    print(f"  {CYAN}{quote}{RESET}: {auto_fmt(q_bal)}")
    print(f"  {CYAN}{base}{RESET}: {auto_fmt(b_bal)}")
    print(f"  {DIM}Giá BID: {auto_fmt(bid)} | ASK: {auto_fmt(ask)}{RESET}")
    if is_allin:
        if q_bal > 0:
            est = safe_amount(exchange, symbol, q_bal / bid)
            print(f"  {DIM}ALL-IN: ~{auto_fmt(q_bal)} {quote} ≈ {est} {base}{RESET}")
    else:
        use_q = min(TRADE_AMOUNT, q_bal)
        if use_q > 0 and bid > 0:
            est = safe_amount(exchange, symbol, use_q / bid)
            print(f"  {DIM}FIXED: {auto_fmt(use_q)} {quote} ≈ {est} {base}{RESET}")
    if min_cost:
        print(f"  {DIM}Min notional: {min_cost} {quote}{RESET}")

    if not IS_TESTNET:
        print(f"\n  {RED}{BOLD}⚠  BẠN ĐANG DÙNG MAINNET — MẤT TIỀN THẬT NẾU SAI! ⚠{RESET}")
        confirm = input(f"  {BOLD}▶ Xác nhận chạy {mode_str} trên MAINNET? (yes/no): {RESET}").strip().lower()
        if confirm != "yes":
            print(f"  {DIM}Đã hủy.{RESET}")
            return
    else:
        confirm = input(f"\n  {BOLD}▶ Chạy {mode_str}? (y/n) [{CYAN}y{RESET}{BOLD}]: {RESET}").strip().lower()
        if confirm not in ("", "y", "yes"):
            print(f"  {DIM}Đã hủy.{RESET}")
            return

    tlog = TradeLogger(symbol)
    summary = {"total": 0, "wins": 0, "losses": 0, "retries": 0, "total_pnl": 0.0}
    history = []
    cycle = 0
    buy_filled_at = time.time()

    net_tag = "TESTNET" if IS_TESTNET else "MAINNET"
    print(f"\n  {BG_GREEN}{WHITE}{BOLD} BOT CHẠY — {mode_str} | {net_tag} {RESET}  {DIM}CSV: {tlog.filename}  |  Ctrl+C dừng{RESET}\n")

    try:
        while True:
            cycle += 1
            ts_short = datetime.now().strftime("%H:%M:%S")
            recovery_mode = False

            # ══════════════════════════════════════════════
            #  PRE-CHECK: quote quá thấp + còn base → recovery sell
            # ══════════════════════════════════════════════
            try:
                bid, ask = fetch_bid_ask(exchange, symbol)
                q_pre = get_free_balance(exchange, quote)
                b_pre = get_free_balance(exchange, base)
                buy_price_ref = safe_price(exchange, symbol, bid)

                if is_allin:
                    use_q_pre = q_pre
                else:
                    use_q_pre = min(TRADE_AMOUNT, q_pre)

                quote_too_low = use_q_pre <= 0 or (min_cost and use_q_pre < min_cost)
                base_sellable = (b_pre > 0
                                 and (not min_amount or b_pre >= min_amount)
                                 and (not min_cost or b_pre * buy_price_ref >= min_cost))

                if quote_too_low and base_sellable:
                    recovery_mode = True
                    buy_fill_price = buy_price_ref
                    buy_fill_amount = safe_amount(exchange, symbol, b_pre)
                    buy_cost = buy_fill_price * buy_fill_amount
                    buy_filled_at = time.time()
                    log(f"[RECOVERY] {quote} thấp ({auto_fmt(q_pre)}) — còn {auto_fmt(b_pre)} {base} → bán recovery", YELLOW)
                elif quote_too_low and not base_sellable:
                    log(f"Cả {quote} ({auto_fmt(q_pre)}) lẫn {base} ({auto_fmt(b_pre)}) đều quá thấp → bỏ qua",
                        RED)
                    display(cycle, symbol, quote, base, bid, ask,
                            f"Số dư quá thấp cả 2 phía: {auto_fmt(q_pre)} {quote} + {auto_fmt(b_pre)} {base}",
                            summary, history, spread_pct, bal_quote=q_pre, bal_base=b_pre,
                            rate_info=get_rate_limit_used(exchange))
                    time.sleep(5)
                    cycle -= 1
                    continue
            except Exception as e:
                log(f"Pre-check lỗi: {e}", RED)
                time.sleep(COOLDOWN)
                cycle -= 1
                continue

            # ══════════════════════════════════════════════
            #  BƯỚC 1: MUA (skip nếu recovery)
            # ══════════════════════════════════════════════
            if not recovery_mode:
                buy_fill_price = 0
                buy_fill_amount = 0
                buy_cost = 0
                attempt = 0

                while True:
                    attempt += 1
                    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    try:
                        bid, ask = fetch_bid_ask(exchange, symbol)
                    except Exception as e:
                        log(f"Lỗi giá: {e}", RED)
                        time.sleep(COOLDOWN)
                        continue

                    q_bal = get_free_balance(exchange, quote)
                    b_bal = get_free_balance(exchange, base)
                    buy_price = safe_price(exchange, symbol, bid)

                    if is_allin:
                        use_quote = q_bal
                    else:
                        use_quote = min(TRADE_AMOUNT, q_bal)

                    if use_quote <= 0 or (min_cost and use_quote < min_cost):
                        base_can_sell = (b_bal > 0
                                         and (not min_amount or b_bal >= min_amount)
                                         and (not min_cost or b_bal * buy_price >= min_cost))
                        if base_can_sell:
                            log(f"[RECOVERY] {quote} thấp ({auto_fmt(q_bal)}) → chuyển bán {auto_fmt(b_bal)} {base}", YELLOW)
                            recovery_mode = True
                            buy_fill_price = buy_price
                            buy_fill_amount = safe_amount(exchange, symbol, b_bal)
                            buy_cost = buy_price * buy_fill_amount
                            buy_filled_at = time.time()
                            break

                        log(f"Số dư {quote} quá thấp ({auto_fmt(q_bal)}), {base} cũng thấp ({auto_fmt(b_bal)}). Chờ...", YELLOW)
                        display(cycle, symbol, quote, base, bid, ask,
                                f"Số dư quá thấp: {auto_fmt(q_bal)} {quote} + {auto_fmt(b_bal)} {base}. Chờ...",
                                summary, history, spread_pct, bal_quote=q_bal, bal_base=b_bal,
                                rate_info=get_rate_limit_used(exchange))
                        time.sleep(3)
                        continue

                    base_amount = safe_amount(exchange, symbol, use_quote * 0.999 / buy_price)

                    if min_amount and base_amount < min_amount:
                        log(f"Amount {base_amount} < min {min_amount}", YELLOW)
                        time.sleep(3)
                        continue

                    sell_target = safe_price(exchange, symbol, buy_price * (1 + spread_pct / 100))
                    retry_label = f" (lần {attempt})" if attempt > 1 else ""

                    buy_label = f"ALL-IN {auto_fmt(q_bal)}" if is_allin else f"FIXED {auto_fmt(use_quote)}"
                    display(cycle, symbol, quote, base, bid, ask,
                            f"[#{cycle}] {buy_label} {quote} → MUA {base_amount} {base} @ {auto_fmt(buy_price)}{retry_label}",
                            summary, history, spread_pct, bal_quote=q_bal, bal_base=b_bal,
                            rate_info=get_rate_limit_used(exchange))

                    try:
                        log(f"[MUA] Limit: {base_amount} {base} @ {auto_fmt(buy_price)} "
                            f"({buy_label} {quote}){retry_label}", CYAN)
                        order = exchange.create_limit_buy_order(symbol, base_amount, buy_price)
                        invalidate_balance_cache()
                        order_id = order["id"]

                        result, status = wait_for_fill_timeout(exchange, order_id, symbol, BUY_RETRY_TIMEOUT)

                        if status == "filled":
                            buy_filled_at = time.time()
                            buy_fill_price = float(result.get("average", 0) or result.get("price", 0) or buy_price)
                            buy_fill_amount = float(result.get("filled", 0) or base_amount)
                            buy_cost = float(result.get("cost", 0) or buy_fill_price * buy_fill_amount)
                            log(f"[MUA] ✓ KHỚP {buy_fill_amount} {base} @ {auto_fmt(buy_fill_price)}", GREEN)
                            tlog.write({"cycle": cycle, "timestamp": ts_now, "side": "BUY",
                                        "symbol": symbol, "order_price": buy_price,
                                        "filled_price": buy_fill_price, "amount": base_amount,
                                        "filled_amount": buy_fill_amount, "cost": buy_cost,
                                        "status": "filled", "order_id": order_id,
                                        "pnl": 0, "duration_s": 0, "note": f"all-in {auto_fmt(q_bal)} {quote}"})
                            break
                        else:
                            cancel_safe(exchange, order_id, symbol)
                            time.sleep(0.3)
                            try:
                                cancelled = exchange.fetch_order(order_id, symbol)
                            except Exception:
                                cancelled = result

                            partial_filled = float(cancelled.get("filled", 0) or 0)
                            partial_cost = float(cancelled.get("cost", 0) or 0)
                            partial_avg = float(cancelled.get("average", 0) or cancelled.get("price", 0) or buy_price)

                            if partial_filled > 0 and (not min_amount or partial_filled >= min_amount):
                                buy_filled_at = time.time()
                                buy_fill_price = partial_avg
                                buy_fill_amount = partial_filled
                                buy_cost = partial_cost if partial_cost > 0 else partial_avg * partial_filled
                                log(f"[MUA] ⚡ PARTIAL: {partial_filled}/{base_amount} {base} @ {auto_fmt(partial_avg)}", YELLOW)
                                tlog.write({"cycle": cycle, "timestamp": ts_now, "side": "BUY",
                                            "symbol": symbol, "order_price": buy_price,
                                            "filled_price": partial_avg, "amount": base_amount,
                                            "filled_amount": partial_filled, "cost": buy_cost,
                                            "status": "partial", "order_id": order_id,
                                            "pnl": 0, "duration_s": 0,
                                            "note": f"partial {partial_filled}/{base_amount}"})
                                break

                            summary["retries"] += 1
                            log(f"[MUA] ✗ Không khớp {BUY_RETRY_TIMEOUT}s → hủy → quét lại...", YELLOW)

                    except Exception as e:
                        log(f"[MUA] Lỗi: {e}", RED)
                        time.sleep(COOLDOWN)
                        continue

            rec_tag = " [RECOVERY]" if recovery_mode else ""
            trade_record = {
                "cycle": cycle, "time": ts_short,
                "buy_price": buy_fill_price, "sell_price": None,
                "amount": buy_fill_amount, "pnl": 0, "duration_s": None,
                "status": f"→ BÁN{rec_tag}"
            }
            history.append(trade_record)

            # ══════════════════════════════════════════════
            #  BƯỚC 2: BÁN
            #  Recovery: bán tại BID hiện tại (nhanh)
            #  Normal:   bán tại buy_price + spread% (chờ vô hạn)
            # ══════════════════════════════════════════════
            if recovery_mode:
                sell_price = safe_price(exchange, symbol, bid)
            else:
                sell_price = safe_price(exchange, symbol, buy_fill_price * (1 + spread_pct / 100))

            b_bal_now = get_free_balance(exchange, base)
            if is_allin or recovery_mode:
                sell_amount = safe_amount(exchange, symbol, max(buy_fill_amount, b_bal_now))
            else:
                sell_amount = safe_amount(exchange, symbol, buy_fill_amount)

            open_trade = {
                "cycle": cycle, "buy_price": buy_fill_price,
                "sell_target": sell_price, "amount": sell_amount,
            }

            q_bal = get_free_balance(exchange, quote)
            sell_label = "BÁN-RECOVERY @BID" if recovery_mode else f"BÁN @ {auto_fmt(sell_price)}"
            display(cycle, symbol, quote, base, bid, ask,
                    f"[#{cycle}]{rec_tag} {sell_amount} {base} → {sell_label}",
                    summary, history, spread_pct, open_trade, bal_quote=q_bal, bal_base=sell_amount,
                    rate_info=get_rate_limit_used(exchange))

            try:
                if recovery_mode:
                    log(f"[BÁN-RECOVERY] Limit: {sell_amount} {base} @ {auto_fmt(sell_price)} (bán tại BID)", YELLOW)
                else:
                    log(f"[BÁN] Limit: {sell_amount} {base} @ {auto_fmt(sell_price)} "
                        f"(+{spread_pct}% từ {auto_fmt(buy_fill_price)})", MAGENTA)
                sell_order = exchange.create_limit_sell_order(symbol, sell_amount, sell_price)
                invalidate_balance_cache()
                sell_id = sell_order["id"]

                last_bid, last_ask = bid, ask

                def sell_cb(elapsed):
                    nonlocal last_bid, last_ask
                    trade_record["status"] = f"→ BÁN ({int(elapsed)}s)"
                    try:
                        last_bid, last_ask = fetch_bid_ask(exchange, symbol)
                    except Exception:
                        pass
                    display(cycle, symbol, quote, base, last_bid, last_ask,
                            f"[#{cycle}] Chờ BÁN #{sell_id} @ {auto_fmt(sell_price)}... ({int(elapsed)}s)",
                            summary, history, spread_pct, open_trade,
                            bal_quote=q_bal, bal_base=sell_amount,
                            rate_info=get_rate_limit_used(exchange))

                sell_result = wait_until_filled(exchange, sell_id, symbol, sell_cb)

                if sell_result.get("status") == "canceled":
                    sell_partial = float(sell_result.get("filled", 0) or 0)
                    sell_partial_cost = float(sell_result.get("cost", 0) or 0)
                    sell_partial_avg = float(sell_result.get("average", 0) or sell_result.get("price", 0) or 0)
                    dur = round(time.time() - buy_filled_at, 1)

                    if sell_partial > 0:
                        partial_pnl = sell_partial_cost - (buy_fill_price * sell_partial)
                        log(f"[BÁN] ⚡ Hủy ngoài: khớp {sell_partial}/{sell_amount}", YELLOW)
                        tlog.write({"cycle": cycle, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "side": "SELL", "symbol": symbol, "order_price": sell_price,
                                    "filled_price": sell_partial_avg, "amount": sell_amount,
                                    "filled_amount": sell_partial, "cost": sell_partial_cost,
                                    "status": "partial", "order_id": sell_id,
                                    "pnl": round(partial_pnl, 8), "duration_s": dur,
                                    "note": f"partial {sell_partial}/{sell_amount}"})
                        trade_record["sell_price"] = sell_partial_avg
                        trade_record["pnl"] = partial_pnl
                        trade_record["duration_s"] = dur
                        trade_record["status"] = "⚡partial"
                        summary["total"] += 1
                        summary["total_pnl"] += partial_pnl
                    else:
                        log(f"[BÁN] Lệnh bị hủy ngoài!", RED)
                        trade_record["status"] = "HỦY"
                        tlog.write({"cycle": cycle, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "side": "SELL", "symbol": symbol, "order_price": sell_price,
                                    "filled_price": 0, "amount": sell_amount, "filled_amount": 0,
                                    "cost": 0, "status": "cancelled", "order_id": sell_id,
                                    "pnl": 0, "duration_s": dur, "note": "Cancelled externally"})
                    time.sleep(COOLDOWN)
                    continue

            except Exception as e:
                log(f"[BÁN] Lỗi: {e}", RED)
                trade_record["status"] = "LỖI"
                time.sleep(COOLDOWN)
                continue

            # ── Bán khớp! ──
            sell_filled_at = time.time()
            duration_s = round(sell_filled_at - buy_filled_at, 1)

            sell_fill_price = float(sell_result.get("average", 0) or sell_result.get("price", 0) or sell_price)
            sell_fill_amount = float(sell_result.get("filled", 0) or sell_amount)
            sell_cost_val = float(sell_result.get("cost", 0) or sell_fill_price * sell_fill_amount)
            pnl = sell_cost_val - buy_cost

            pnl_c = GREEN if pnl >= 0 else RED
            log(f"[BÁN] ✓ KHỚP @ {auto_fmt(sell_fill_price)} → PnL: {pnl_c}{BOLD}{auto_fmt(pnl)} {quote}{RESET}  ⏱ {duration_s}s", GREEN)

            tlog.write({"cycle": cycle, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "side": "SELL", "symbol": symbol, "order_price": sell_price,
                        "filled_price": sell_fill_price, "amount": sell_amount,
                        "filled_amount": sell_fill_amount, "cost": sell_cost_val,
                        "status": "filled", "order_id": sell_id,
                        "pnl": round(pnl, 8), "duration_s": duration_s,
                        "note": "recovery-sell @BID" if recovery_mode else f"spread={spread_pct}%"})

            trade_record["sell_price"] = sell_fill_price
            trade_record["pnl"] = pnl
            trade_record["duration_s"] = duration_s
            trade_record["status"] = "✓" if pnl >= 0 else "✗"
            summary["total"] += 1
            summary["total_pnl"] += pnl
            if pnl > 0:
                summary["wins"] += 1
            elif pnl < 0:
                summary["losses"] += 1

            q_bal = get_free_balance(exchange, quote)
            b_bal = get_free_balance(exchange, base)
            try:
                b, a = fetch_bid_ask(exchange, symbol)
            except Exception:
                b, a = bid, ask
            display(cycle, symbol, quote, base, b, a,
                    f"Chu kỳ #{cycle} xong. PnL: {auto_fmt(pnl)} {quote}. Số dư: {auto_fmt(q_bal)} {quote}",
                    summary, history, spread_pct, bal_quote=q_bal, bal_base=b_bal,
                    rate_info=get_rate_limit_used(exchange))

            time.sleep(COOLDOWN)

    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"Lỗi: {e}", RED)
    finally:
        if tlog:
            tlog.close()
        print(f"\n\n  {BOLD}{'═' * 60}{RESET}")
        print(f"  {BOLD}  BOT DỪNG{RESET}")
        print(f"  {BOLD}{'═' * 60}{RESET}")
        print(f"  Chu kỳ: {summary['total']}  |  Lãi: {GREEN}{summary['wins']}{RESET}  |  "
              f"Lỗ: {RED}{summary['losses']}{RESET}  |  Retry: {YELLOW}{summary['retries']}{RESET}")
        pnl_c = GREEN if summary["total_pnl"] >= 0 else RED
        print(f"  PnL: {pnl_c}{BOLD}{auto_fmt(summary['total_pnl'])} {quote}{RESET}")

        q_final = 0
        try:
            q_final = get_free_balance(exchange, quote)
            b_final = get_free_balance(exchange, base)
            print(f"  Số dư cuối: {CYAN}{auto_fmt(q_final)} {quote}{RESET} + {CYAN}{auto_fmt(b_final)} {base}{RESET}")
        except Exception:
            pass

        if history:
            print(f"\n  {BOLD}{'#':>4}  {'Giờ':<10} {'Mua @':>14} {'Bán @':>14} {'SL':>10} {'PnL':>14} {'T.gian':>8}{RESET}")
            print(f"  {'─' * 80}")
            for h in history:
                pv = h.get("pnl", 0)
                dur = h.get("duration_s")
                dur_str = f"{dur:.0f}s" if dur is not None else "—"
                pc = GREEN if pv > 0 else (RED if pv < 0 else DIM)
                sp = auto_fmt(h['sell_price']) if h.get('sell_price') else "—"
                pp = auto_fmt(pv) if h.get('sell_price') else "—"
                print(f"  {pc}{h['cycle']:>4}  {h['time']:<10} {auto_fmt(h['buy_price']):>14} "
                      f"{sp:>14} {h['amount']:>10} {pp:>14} {dur_str:>8}{RESET}")
            completed = [h for h in history if h.get("duration_s") is not None]
            if completed:
                avg_dur = sum(h["duration_s"] for h in completed) / len(completed)
                print(f"  {'─' * 80}")
                print(f"  {BOLD}{'Thời gian vào lệnh TB:':>76} {avg_dur:.1f}s{RESET}")

        if tlog and tlog.count > 0:
            print(f"\n  {GREEN}{BOLD}✓ {tlog.count} dòng → {tlog.filename}{RESET}")
        elif tlog:
            print(f"\n  {YELLOW}Không có giao dịch.{RESET}")
            try:
                os.remove(tlog.filename)
            except OSError:
                pass
        print()


if __name__ == "__main__":
    main()
