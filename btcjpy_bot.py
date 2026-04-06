import ccxt
import csv
import sys
import time
import os
from datetime import datetime
from dotenv import dotenv_values

if os.name == "nt":
    os.system("chcp 65001 > nul")
else:
    # Railway/container thường không set TERM; tránh cảnh báo khi gọi `clear`
    os.environ.setdefault("TERM", "dumb")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════

USE_PCT = 0.99

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
env = dotenv_values(ENV_PATH)
ENV_MAP = {}
for k, v in (env or {}).items():
    ks = str(k).strip() if k is not None else ""
    if not ks:
        continue
    vs = str(v).strip() if v is not None else ""
    ENV_MAP[ks] = vs

# Railway/production thường truyền secrets qua environment variables thay vì file .env
def _cfg(name, default=""):
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        v = ENV_MAP.get(name, default)
    return str(v).strip() if v is not None else default


API_KEY = _cfg("API_KEY", "")
SECRET_KEY = _cfg("SECRET_KEY", "")
MEXC_API_KEY = _cfg("MEXC_API_KEY", "")
MEXC_SECRET_KEY = _cfg("MEXC_SECRET_KEY", "")
IS_TESTNET = _cfg("TESTNET", "true").lower() in ("true", "1", "yes")
DEFAULT_PAIR = _cfg("DEFAULT_PAIR", "")

EXCHANGE_ID = _cfg("EXCHANGE", "binance").lower() or "binance"
if EXCHANGE_ID not in ("binance", "mexc"):
    print(f"\033[91m  ✗ EXCHANGE trong .env phải là 'binance' hoặc 'mexc' (hiện: {EXCHANGE_ID})\033[0m")
    sys.exit(1)

# Kiểm tra API keys theo sàn được chọn
if EXCHANGE_ID == "binance":
    if not API_KEY or not SECRET_KEY:
        print("\033[91m  ✗ Thiếu API_KEY hoặc SECRET_KEY trong .env cho Binance\033[0m")
        sys.exit(1)
else:
    # Cho phép fallback: nếu bạn chưa set MEXC_API_KEY/MEXC_SECRET_KEY thì dùng API_KEY/SECRET_KEY
    if (not MEXC_API_KEY or not MEXC_SECRET_KEY) and (not API_KEY or not SECRET_KEY):
        print("\033[91m  ✗ Thiếu MEXC_API_KEY/MEXC_SECRET_KEY (hoặc API_KEY/SECRET_KEY) trong .env cho MEXC\033[0m")
        sys.exit(1)

BUY_TIMEOUT = float(_cfg("BUY_RETRY_TIMEOUT", "5"))
# Nghỉ sau khi một vòng BUY->SELL đã chốt xong
POST_TRADE_PAUSE = float(_cfg("POST_TRADE_PAUSE", "30"))
# SELL của nhánh Binance OTO: chờ tối đa N giây rồi reprice theo ASK thấp nhất
OTO_SELL_TIMEOUT = float(_cfg("OTO_SELL_TIMEOUT", "5"))
POLL_INTERVAL = 0.5
COOLDOWN = 0.3

SPREAD_MAP = {}
FEE_MAP = {}
for key, val in ENV_MAP.items():
    if key.startswith("SPREAD_"):
        try: SPREAD_MAP[key[7:].upper()] = float(val)
        except ValueError: pass
    if key.startswith("FEE_") and key != "FEE_DEFAULT":
        try: FEE_MAP[key[4:].upper()] = float(val)
        except ValueError: pass
# Railway: SPREAD_* / FEE_* chỉ có trong biến môi trường (không có file .env) — merge và ghi đè
for key, val in os.environ.items():
    k = str(key).strip()
    if not k:
        continue
    v = str(val).strip() if val is not None else ""
    if k.startswith("SPREAD_"):
        try:
            SPREAD_MAP[k[7:].upper()] = float(v)
        except ValueError:
            pass
    elif k.startswith("FEE_") and k != "FEE_DEFAULT":
        try:
            FEE_MAP[k[4:].upper()] = float(v)
        except ValueError:
            pass
FEE_DEFAULT = float(_cfg("FEE_DEFAULT", "0.1"))

SYMBOL = ""
PAIR = ""
QUOTE = ""
BASE = ""
SPREAD_PCT = 0.0
FEE_PCT = 0.0
FEE_RATE = 0.0

# Ước lượng tần suất request MEXC (client-side, rolling ~10s) — Spot doc: IP/UID limit theo nhóm endpoint
_MEXC_REQ_TS = []
_MEXC_SELL_REPRICE_COUNT = 0

# ═══════════════════════════════════════════════════════════════
#  COLORS
# ═══════════════════════════════════════════════════════════════

G = "\033[92m"; R = "\033[91m"; C = "\033[96m"; Y = "\033[93m"
M = "\033[95m"; B = "\033[1m"; D = "\033[2m"; X = "\033[0m"
BG_G = "\033[42m"; BG_R = "\033[41m"; W = "\033[97m"

CSV_COLS = [
    "cycle", "timestamp", "side", "order_price", "filled_price",
    "amount", "filled_amount", "cost", "status", "order_id",
    "pnl", "duration_s", "fee_quote", "note",
]
MAX_HIST = 20
RATE_MAX = 1200


# ═══════════════════════════════════════════════════════════════
#  EXCHANGE
# ═══════════════════════════════════════════════════════════════

def create_exchange():
    if EXCHANGE_ID == "binance":
        ex = ccxt.binance({
            "apiKey": API_KEY,
            "secret": SECRET_KEY,
            "enableRateLimit": True,
            "timeout": 20000,
            "options": {"defaultType": "spot"},
        })
        if IS_TESTNET:
            ex.set_sandbox_mode(True)
        return ex

    # MEXC
    mexc_key = MEXC_API_KEY or API_KEY
    mexc_secret = MEXC_SECRET_KEY or SECRET_KEY
    ex = ccxt.mexc({
        "apiKey": mexc_key,
        "secret": mexc_secret,
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "spot"},
    })
    # MEXC testnet/sandbox có thể không tồn tại tùy ccxt; chỉ bật nếu có method
    if IS_TESTNET and hasattr(ex, "set_sandbox_mode"):
        try:
            ex.set_sandbox_mode(True)
        except Exception:
            pass
    return ex


# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════

def fetch_ba(ex):
    # MEXC đôi lúc timeout khi gọi depth. Thêm retry/backoff để bot không crash.
    last_err = None
    for i in range(3):
        try:
            ob = ex.fetch_order_book(SYMBOL, limit=5)
            if not ob.get("bids") or not ob.get("asks"):
                raise ValueError("Order book trống")
            _mexc_bump_request()
            return ob["bids"][0][0], ob["asks"][0][0]
        except (ccxt.RequestTimeout, ccxt.NetworkError) as e:
            last_err = e
            time.sleep(0.8 + i * 0.6)
        except Exception as e:
            last_err = e
            time.sleep(0.3 + i * 0.2)
    raise last_err if last_err else Exception("fetch_ba failed")


def fmt(v):
    if v >= 1000:   return f"{v:,.2f}"
    if v >= 1:      return f"{v:,.4f}"
    if v >= 0.01:   return f"{v:,.6f}"
    return f"{v:.8f}"


def s_amt(ex, a):
    return float(ex.amount_to_precision(SYMBOL, a))


def s_price(ex, p):
    return float(ex.price_to_precision(SYMBOL, p))


_bc = {"d": None, "t": 0}

def bal(ex, cur):
    now = time.time()
    if _bc["d"] is None or (now - _bc["t"]) > 1.0:
        _bc["d"] = ex.fetch_balance()
        _mexc_bump_request()
        _bc["t"] = now
    return float(_bc["d"].get(cur, {}).get("free", 0) or 0)


def inv_bal():
    _bc["d"] = None; _bc["t"] = 0


def _mexc_bump_request():
    if EXCHANGE_ID != "mexc":
        return
    now = time.time()
    _MEXC_REQ_TS.append(now)
    while _MEXC_REQ_TS and now - _MEXC_REQ_TS[0] > 10.0:
        _MEXC_REQ_TS.pop(0)


def _mexc_requests_last_10s():
    if EXCHANGE_ID != "mexc":
        return 0
    now = time.time()
    while _MEXC_REQ_TS and now - _MEXC_REQ_TS[0] > 10.0:
        _MEXC_REQ_TS.pop(0)
    return len(_MEXC_REQ_TS)


def rate_status(ex):
    """Chuỗi hiển thị rate/limit (Binance: weight 1m; MEXC Spot: header + đếm client + ghi chú doc)."""
    h = getattr(ex, "last_response_headers", None) or {}
    hl = {str(k).lower(): str(v).strip() for k, v in h.items()}

    if EXCHANGE_ID == "binance":
        u = hl.get("x-mbx-used-weight-1m")
        if u is None:
            return ""
        try:
            n = int(u)
        except ValueError:
            return ""
        pct = min(100.0, n / RATE_MAX * 100) if RATE_MAX else 0.0
        rc = R if pct > 80 else (Y if pct > 50 else G)
        return f"  |  Binance API: {rc}{n}/{RATE_MAX} ({pct:.0f}%){X}"

    if EXCHANGE_ID == "mexc":
        hdr = []
        for k in sorted(hl):
            if any(s in k for s in ("rate", "limit", "retry")):
                hdr.append(f"{k}={hl[k]}")
        n10 = _mexc_requests_last_10s()
        lim_ms = int(getattr(ex, "rateLimit", 0) or 0)
        parts = [
            f"~{n10} req/10s (client)",
            f"ccxt min {lim_ms}ms",
            "Spot doc: IP 300/10s · UID 500/10s (theo nhóm endpoint)",
        ]
        if hdr:
            parts.append("hdr: " + " · ".join(hdr[:5]))
        return f"  |  {Y}MEXC {' · '.join(parts)}{X}"

    return ""


def limits(ex):
    m = ex.markets[SYMBOL]
    min_amt = m.get("limits", {}).get("amount", {}).get("min")
    min_cost = m.get("limits", {}).get("cost", {}).get("min")
    if not min_cost:
        for f in m.get("info", {}).get("filters", []):
            if f.get("filterType") in ("NOTIONAL", "MIN_NOTIONAL"):
                min_cost = float(f.get("minNotional", 0))
                break
    return min_amt, min_cost


def cancel_safe(ex, oid):
    try: ex.cancel_order(oid, SYMBOL)
    except Exception: pass


def log(msg, color=""):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"  {D}{ts}{X}  {color}{msg}{X}")


def pause_after_trade():
    if POST_TRADE_PAUSE > 0:
        log(f"Nghỉ {POST_TRADE_PAUSE:.0f}s sau chu kỳ vừa chốt...", Y)
        time.sleep(POST_TRADE_PAUSE)


def handle_mexc_open_sell(ex, summary):
    """
    Nếu MEXC còn lệnh SELL đang mở, bot sẽ ưu tiên theo dõi/reprice để thoát hàng,
    không đi vào nhánh "số dư quá thấp -> dừng".
    Trả về True nếu đã xử lý và nên skip phần còn lại của vòng lặp hiện tại.
    """
    global _MEXC_SELL_REPRICE_COUNT
    if EXCHANGE_ID != "mexc":
        return False
    try:
        opens = ex.fetch_open_orders(SYMBOL, limit=20)
    except Exception:
        return False

    sells = []
    for o in opens or []:
        side = str(o.get("side", "")).lower()
        status = str(o.get("status", "")).lower()
        if side == "sell" and status in ("open", "new", "partially_filled"):
            sells.append(o)

    if not sells:
        return False

    sells.sort(key=lambda x: x.get("timestamp") or 0)
    o = sells[0]
    oid = o.get("id")
    price = float(o.get("price", 0) or 0)
    amount = float(o.get("amount", 0) or 0)
    filled = float(o.get("filled", 0) or 0)
    rem = max(0.0, amount - filled)
    ts = o.get("timestamp")
    elapsed = ((time.time() * 1000 - ts) / 1000) if ts else None

    if rem <= 0:
        return True

    if elapsed is not None and elapsed < OTO_SELL_TIMEOUT:
        log(f"[MEXC] Đang chờ SELL #{oid} còn {fmt(rem)} @ {fmt(price)} ({elapsed:.1f}s)", D)
        time.sleep(COOLDOWN)
        return True

    cancel_safe(ex, oid)
    time.sleep(0.2)
    try:
        _, ask_now = fetch_ba(ex)
        ask_reprice = s_price(ex, ask_now)
    except Exception:
        ask_reprice = price if price > 0 else 0

    rem = s_amt(ex, rem)
    if rem <= 0:
        time.sleep(COOLDOWN)
        return True

    try:
        so = ex.create_limit_sell_order(SYMBOL, rem, ask_reprice)
        inv_bal()
        _MEXC_SELL_REPRICE_COUNT += 1
        summary["retries"] += 1
        log(f"[MEXC] SELL timeout {OTO_SELL_TIMEOUT}s → hủy #{oid}, đặt lại {rem} @ ASK {fmt(ask_reprice)} (#{so['id']})", Y)
        if _MEXC_SELL_REPRICE_COUNT % 5 == 0:
            log(f"[MEXC] Đã reprice SELL {_MEXC_SELL_REPRICE_COUNT} lần, tiếp tục cho đến khi bán xong.", Y)
    except Exception as e:
        log(f"[MEXC] Lỗi reprice SELL: {e}", R)
    time.sleep(COOLDOWN)
    return True


def net_pnl(sell_cost, buy_cost):
    return sell_cost * (1 - FEE_RATE) - buy_cost


def est_fees(sell_cost, buy_cost):
    return buy_cost * FEE_RATE + sell_cost * FEE_RATE


# ═══════════════════════════════════════════════════════════════
#  OTO ORDER — 1 API call = BUY LIMIT + auto SELL LIMIT
# ═══════════════════════════════════════════════════════════════

def _binance_oto_plain_str(n):
    """
    Binance OTO yêu cầu workingPrice/pendingPrice dạng thuần số thập phân
    (regex [0-9]+(\\.[0-9]+)?). str(float) có thể ra '1.5e-05' → lỗi -1100.
    """
    x = float(n)
    s = f"{x:.20f}".rstrip("0").rstrip(".")
    if s in ("", "-0"):
        s = "0"
    return s


def place_oto(ex, buy_price, buy_qty, sell_price, sell_qty):
    params = {
        "symbol": PAIR,
        "workingType": "LIMIT",
        "workingSide": "BUY",
        "workingPrice": _binance_oto_plain_str(buy_price),
        "workingQuantity": _binance_oto_plain_str(buy_qty),
        "workingTimeInForce": "GTC",
        "pendingType": "LIMIT",
        "pendingSide": "SELL",
        "pendingPrice": _binance_oto_plain_str(sell_price),
        "pendingQuantity": _binance_oto_plain_str(sell_qty),
        "pendingTimeInForce": "GTC",
        "newOrderRespType": "FULL",
    }
    return ex.request("orderList/oto", api="private", method="POST", params=params)


def cancel_order_list(ex, list_id):
    try:
        return ex.request("orderList", api="private", method="DELETE",
                          params={"symbol": PAIR, "orderListId": list_id})
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  WAIT / POLL
# ═══════════════════════════════════════════════════════════════

def wait_buy(ex, oid, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            o = ex.fetch_order(oid, SYMBOL)
            if o["status"] == "closed":   return o, "filled"
            if o["status"] == "canceled": return o, "cancelled"
        except ccxt.OrderNotFound:
            return {"status": "canceled", "filled": 0, "id": oid}, "cancelled"
        except Exception: pass
        time.sleep(POLL_INTERVAL)
    try:
        o = ex.fetch_order(oid, SYMBOL)
        if o["status"] == "closed": return o, "filled"
        return o, "timeout"
    except Exception:
        return {"status": "unknown", "filled": 0}, "timeout"


def wait_sell(ex, oid, cb=None):
    elapsed = 0.0
    last_cb = 0.0
    while True:
        try:
            o = ex.fetch_order(oid, SYMBOL)
            if o["status"] == "closed":   return o
            if o["status"] == "canceled": return o
        except ccxt.OrderNotFound:
            return {"status": "canceled", "filled": 0, "id": oid}
        except Exception: pass
        poll = min(POLL_INTERVAL * (1 + elapsed // 120), 3.0)
        time.sleep(poll)
        elapsed += poll
        if cb and (elapsed - last_cb) >= 2.0:
            cb(elapsed)
            last_cb = elapsed


# ═══════════════════════════════════════════════════════════════
#  CSV LOGGER
# ═══════════════════════════════════════════════════════════════

class TLog:
    def __init__(self, pair_tag):
        # Theo yêu cầu: không xuất CSV.
        self.fn = "(disabled)"
        self.n = 0

    def write(self, row):
        return


# ═══════════════════════════════════════════════════════════════
#  DISPLAY
# ═══════════════════════════════════════════════════════════════

def display(cycle, bid, ask, state, summary, history,
            q_bal=0, b_bal=0, open_trade=None, rl=None):
    if os.name == "nt":
        os.system("cls")
    else:
        # Không gọi `clear` (cần TERM hợp lệ trên một số container)
        print("\033[2J\033[H", end="")
    w = 76
    now = datetime.now().strftime("%H:%M:%S")
    spread = ask - bid
    realized = summary["total_pnl"]
    total_fees = summary["total_fees"]

    unrealized = 0.0
    if open_trade and bid > 0:
        gross_u = (bid - open_trade["buy_price"]) * open_trade["amount"]
        unrealized = gross_u * (1 - FEE_RATE) - open_trade["buy_price"] * open_trade["amount"] * FEE_RATE
        unrealized = gross_u
    total_all = realized + unrealized

    net_tag = "TESTNET" if IS_TESTNET else f"{BG_R}{W} MAINNET {X}"
    rl_str = rl if isinstance(rl, str) else ""

    print()
    print(f"  {B}╔{'═'*w}╗{X}")
    title = f"{PAIR} OTO BOT — BUY@BID + auto SELL@(BID+SPREAD)"
    print(f"  {B}║{title:^{w}}║{X}")
    print(f"  {B}╚{'═'*w}╝{X}")
    print(f"  {D}{now}  |  {net_tag}  |  #{cycle}  |  "
          f"Spread: {SPREAD_PCT}%  |  Fee: {FEE_PCT}%/side  |  "
          f"Timeout: {BUY_TIMEOUT}s{X}{rl_str}")
    print()

    print(f"  ┌{'─'*w}┐")
    print(f"  │  {R}{B}BID{X} {C}{fmt(bid):>16}{X}     "
          f"{G}{B}ASK{X} {C}{fmt(ask):>16}{X}     "
          f"{B}Spread{X} {Y}{fmt(spread):>12}{X}    │")
    print(f"  ├{'─'*w}┤")

    parts = []
    if q_bal > 0: parts.append(f"{fmt(q_bal)} {QUOTE}")
    if b_bal > 0: parts.append(f"{fmt(b_bal)} {BASE}")
    bal_s = f"{C}{B}{' + '.join(parts)}{X}" if parts else "—"
    print(f"  │  Số dư: {bal_s}{'':>{w - 12 - len(' + '.join(parts)) if parts else w - 13}}│")
    print(f"  ├{'─'*w}┤")

    rc = G if realized >= 0 else R
    fee_s = f"  Phí: {Y}{fmt(total_fees)}{X}" if total_fees > 0 else ""
    print(f"  │  Xong: {B}{summary['total']}{X}   "
          f"Lãi: {G}{summary['wins']}{X}   Lỗ: {R}{summary['losses']}{X}   "
          f"Retry: {Y}{summary['retries']}{X}   "
          f"PnL: {rc}{B}{fmt(realized)} {QUOTE}{X}{fee_s}     │")

    if open_trade:
        uc = G if unrealized >= 0 else R
        tc = G if total_all >= 0 else R
        print(f"  │  {D}OTO #{open_trade['cycle']}: mua {fmt(open_trade['buy_price'])} → "
              f"bán {fmt(open_trade['sell_target'])}{X}   "
              f"Chưa chốt: {uc}{B}{fmt(unrealized)}{X}   "
              f"Tổng: {tc}{B}{fmt(total_all)} {QUOTE}{X}  │")

    print(f"  └{'─'*w}┘")

    if history:
        print(f"\n  {B}{'#':>4}  {'Giờ':<10} {'Mua @':>16} {'Bán @':>16} "
              f"{'SL':>12} {'PnL ròng':>14} {'T.gian':>8} {'':>3}{X}")
        print(f"  {'─'*w}")
        for h in history[-MAX_HIST:]:
            pv = h.get("pnl", 0)
            dur = h.get("dur")
            dur_s = f"{dur:.0f}s" if dur is not None else ""
            if h.get("sell_price"):
                pc = G if pv > 0 else (R if pv < 0 else D)
                icon = "▲" if pv > 0 else ("▼" if pv < 0 else "─")
                sp = fmt(h["sell_price"]); pp = fmt(pv)
            else:
                pc = D; icon = "…"; sp = "chờ..."; pp = ""
            st = h.get("status", "")
            print(f"  {pc}{h['cycle']:>4}  {h['time']:<10} {fmt(h['buy_price']):>16} "
                  f"{sp:>16} {h['amount']:>12} {pp:>14} {dur_s:>8} {icon:>3} {st}{X}")

        done = [h for h in history if h.get("dur") is not None]
        if done:
            avg = sum(h["dur"] for h in done) / len(done)
            print(f"  {'─'*w}")
            print(f"  {D}{'Thời gian TB:':>72} {avg:.1f}s{X}")
        print(f"  {'─'*w}")
    else:
        print(f"\n  {D}Chưa có giao dịch.{X}")

    print(f"\n  {D}{state}{X}\n")


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

def normalize_symbol(raw, exchange):
    s = raw.strip().upper()
    if "/" in s:
        if s in exchange.markets:
            return s
        raise ValueError(f"'{s}' không tồn tại")
    quotes = ["USDT", "USDC", "BUSD", "FDUSD", "TUSD",
              "BTC", "ETH", "BNB", "JPY", "EUR", "GBP", "TRY", "BRL", "U"]
    for q in quotes:
        if s.endswith(q) and len(s) > len(q):
            cand = f"{s[:-len(q)]}/{q}"
            if cand in exchange.markets:
                return cand
    raise ValueError(f"Không nhận dạng '{raw}'. Thử 'BTC/USDT' hoặc 'BTCUSDT'.")


def setup_pair(symbol):
    global SYMBOL, PAIR, BASE, QUOTE, SPREAD_PCT, FEE_PCT, FEE_RATE
    SYMBOL = symbol
    PAIR = symbol.replace("/", "")
    BASE = symbol.split("/")[0]
    QUOTE = symbol.split("/")[1]
    SPREAD_PCT = SPREAD_MAP.get(PAIR, None)
    FEE_PCT = FEE_MAP.get(PAIR, FEE_DEFAULT)
    FEE_RATE = FEE_PCT / 100


def main():
    net_s = "TESTNET" if IS_TESTNET else "MAINNET (TIỀN THẬT!)"
    print(f"\n  {B}{'═'*60}{X}")
    print(f"  {B}  OTO BOT — 1 lệnh OTO = BUY + auto SELL{X}")
    print(f"  {B}{'═'*60}{X}")

    if not IS_TESTNET:
        print(f"  {BG_R}{W}{B} ⚠  MAINNET — GIAO DỊCH TIỀN THẬT  ⚠ {X}")

    print(f"  {D}Mạng: {net_s}  |  99% balance  |  Timeout: {BUY_TIMEOUT}s{X}")

    if SPREAD_MAP:
        print(f"\n  {B}Spread đã cấu hình:{X}")
        for pair, pct in SPREAD_MAP.items():
            fee = FEE_MAP.get(pair, FEE_DEFAULT)
            print(f"    {C}{pair}{X}: spread {pct}%  |  fee {fee}%/side")

    print(f"\n  {D}Kết nối {EXCHANGE_ID.upper()} {'Testnet' if IS_TESTNET else 'Mainnet'}...{X}")
    ex = create_exchange()
    ex.load_markets()
    print(f"  {G}✓ {len(ex.markets):,} cặp{X}")

    # ── Chọn pair (ưu tiên DEFAULT_PAIR trong .env/env vars) ──
    while True:
        try:
            if DEFAULT_PAIR:
                raw = DEFAULT_PAIR
                print(f"\n  {B}▶ DEFAULT_PAIR: {X}{C}{raw}{X}")
            else:
                raw = input(f"\n  {B}▶ Nhập cặp giao dịch: {X}").strip()
                if not raw:
                    continue

            symbol = normalize_symbol(raw, ex)
            setup_pair(symbol)

            if SPREAD_PCT is None:
                if DEFAULT_PAIR:
                    print(f"  {R}✗ Chưa có SPREAD_{PAIR} trong .env cho DEFAULT_PAIR.{X}")
                    return
                custom = input(f"  {Y}Chưa có SPREAD cho {PAIR} trong .env. Nhập spread %: {X}").strip()
                try:
                    SPREAD_MAP[PAIR] = float(custom)
                except ValueError:
                    print(f"  {R}Không hợp lệ.{X}")
                    continue
                setup_pair(symbol)

            print(f"  {G}✓ {SYMBOL}  |  Spread: {SPREAD_PCT}%  |  Fee: {FEE_PCT}%/side{X}")
            break
        except ValueError as e:
            print(f"  {R}{e}{X}")
            if DEFAULT_PAIR:
                return

    min_spread = FEE_PCT * 2
    if SPREAD_PCT <= min_spread:
        print(f"  {Y}⚠ Spread {SPREAD_PCT}% ≤ phí 2 chiều {min_spread}% → có thể lỗ!{X}")

    min_amt, min_cost = limits(ex)
    try:
        bid, ask = fetch_ba(ex)
    except Exception as e:
        log(f"Lỗi fetch BID/ASK ban đầu ({EXCHANGE_ID.upper()}): {e}", R)
        return
    q = bal(ex, QUOTE)
    b = bal(ex, BASE)

    print(f"\n  {B}═══ SỐ DƯ ═══{X}")
    print(f"  {C}{QUOTE}{X}: {fmt(q)}")
    print(f"  {C}{BASE}{X}: {fmt(b)}")
    print(f"  {D}BID: {fmt(bid)} | ASK: {fmt(ask)}{X}")
    if q > 0 and bid > 0:
        est = s_amt(ex, q * USE_PCT / bid)
        print(f"  {D}99% bal: ~{fmt(q * USE_PCT)} {QUOTE} ≈ {est} {BASE}{X}")
    print(f"  {D}Min amount: {min_amt or '?'}  |  Min notional: {min_cost or '?'} {QUOTE}{X}")

    if DEFAULT_PAIR:
        print(f"  {D}Auto-run vì đã có DEFAULT_PAIR={DEFAULT_PAIR}{X}")
    else:
        if not IS_TESTNET:
            c = input(f"\n  {B}▶ Xác nhận chạy {PAIR} trên MAINNET? (yes/no): {X}").strip().lower()
            if c != "yes":
                print(f"  {D}Đã hủy.{X}"); return
        else:
            c = input(f"\n  {B}▶ Chạy {PAIR}? (y/n) [{C}y{X}{B}]: {X}").strip().lower()
            if c not in ("", "y", "yes"):
                print(f"  {D}Đã hủy.{X}"); return

    tlog = TLog(PAIR)
    summary = {"total": 0, "wins": 0, "losses": 0, "retries": 0,
               "total_pnl": 0.0, "total_fees": 0.0}
    history = []
    cycle = 0

    net_tag = "TESTNET" if IS_TESTNET else "MAINNET"
    mode_tag = "OTO" if EXCHANGE_ID == "binance" else "LIMIT(BUY->SELL)"
    print(f"\n  {BG_G}{W}{B} BOT {mode_tag} {PAIR} — {EXCHANGE_ID.upper()} {net_tag} {X}  {D}CSV: OFF  |  Ctrl+C dừng{X}\n")

    try:
        while True:
            cycle += 1
            ts_short = datetime.now().strftime("%H:%M:%S")
            ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # ── Fetch giá + số dư ──
            try:
                bid, ask = fetch_ba(ex)
                inv_bal()
                q = bal(ex, QUOTE)
                b = bal(ex, BASE)
            except Exception as e:
                log(f"Lỗi fetch: {e}", R)
                time.sleep(COOLDOWN); cycle -= 1; continue

            # MEXC: nếu còn SELL treo thì ưu tiên theo dõi/reprice tới khi thoát hàng xong
            if handle_mexc_open_sell(ex, summary):
                continue

            buy_price = s_price(ex, bid)
            sell_price = s_price(ex, buy_price * (1 + SPREAD_PCT / 100))
            use_q = q * USE_PCT

            # ── Recovery: JPY cạn + có BTC → bán thủ công ──
            q_low = use_q <= 0 or (min_cost and use_q < min_cost)
            has_base = b > 0 and (not min_amt or b >= min_amt)

            if q_low and has_base:
                log(f"[RECOVERY] {QUOTE} thấp ({fmt(q)}) — bán {fmt(b)} {BASE} @ BID", Y)
                sell_amt = s_amt(ex, b)
                trade = {"cycle": cycle, "time": ts_short, "buy_price": buy_price,
                         "sell_price": None, "amount": sell_amt, "pnl": 0,
                         "dur": None, "status": "→ BÁN-RCV"}
                history.append(trade)

                display(cycle, bid, ask, f"[#{cycle}] RECOVERY bán {sell_amt} {BASE} @ {fmt(buy_price)}",
                        summary, history, q, b, rl=rate_status(ex))
                try:
                    sell_order = ex.create_limit_sell_order(SYMBOL, sell_amt, buy_price)
                    inv_bal()
                    sr = wait_sell(ex, sell_order["id"])
                    if sr.get("status") != "canceled":
                        sc = float(sr.get("cost", 0) or sell_amt * buy_price)
                        rpnl = sc * (1 - FEE_RATE) - (buy_price * sell_amt)
                        trade["sell_price"] = buy_price
                        trade["pnl"] = rpnl
                        trade["dur"] = 0
                        trade["status"] = "RCV"
                        summary["total"] += 1
                        summary["total_pnl"] += rpnl
                        tlog.write({"cycle": cycle, "timestamp": ts_now, "side": "SELL",
                                    "order_price": buy_price, "filled_price": buy_price,
                                    "amount": sell_amt, "filled_amount": sell_amt,
                                    "cost": sc, "status": "filled", "order_id": sell_order["id"],
                                    "pnl": round(rpnl, 2), "duration_s": 0,
                                    "fee_quote": round(sc * FEE_RATE, 2), "note": "recovery"})
                except Exception as e:
                    log(f"[RECOVERY] Lỗi: {e}", R)
                    trade["status"] = "LỖI"
                time.sleep(COOLDOWN)
                continue

            if q_low and not has_base:
                log(f"Cả {QUOTE} ({fmt(q)}) lẫn {BASE} ({fmt(b)}) quá thấp. Dừng.", R)
                break

            # ── Tính lượng mua/bán ──
            buy_qty = s_amt(ex, use_q / buy_price)
            if min_amt and buy_qty < min_amt:
                buy_qty = s_amt(ex, min_amt * 1.01)
            if min_cost and buy_qty * buy_price < min_cost:
                buy_qty = s_amt(ex, (min_cost / buy_price) * 1.05)

            sell_qty = s_amt(ex, buy_qty * (1 - FEE_RATE * 1.1))

            if buy_qty * buy_price > use_q:
                log(f"Không đủ {QUOTE}: cần {fmt(buy_qty * buy_price)}, có {fmt(use_q)}", Y)
                time.sleep(3); cycle -= 1; continue

            # ══════════════════════════════════════════════
            #  BINANCE: dùng OTO (nếu có). MEXC: fallback "limit buy + timeout cancel + limit sell"
            # ══════════════════════════════════════════════
            if EXCHANGE_ID != "binance":
                buy_filled_at = None
                buy_fp = 0.0
                buy_fa = 0.0
                buy_cost = 0.0

                trade = {"cycle": cycle, "time": ts_short, "buy_price": buy_price,
                          "sell_price": None, "amount": buy_qty, "pnl": 0,
                          "dur": None, "status": f"{EXCHANGE_ID.upper()}: MUA chờ..."}
                history.append(trade)

                display(cycle, bid, ask,
                        f"[#{cycle}] MUA {buy_qty} @ {fmt(buy_price)} → (timeout {BUY_TIMEOUT}s) BÁN @ {fmt(sell_price)}",
                        summary, history, q, b, rl=rate_status(ex))

                try:
                    bo = ex.create_limit_buy_order(SYMBOL, buy_qty, buy_price)
                    buy_oid = bo["id"]
                    inv_bal()
                except Exception as e:
                    log(f"[{EXCHANGE_ID.upper()}] Lỗi đặt BUY: {e}", R)
                    time.sleep(COOLDOWN)
                    cycle -= 1
                    continue

                buy_result, buy_status = wait_buy(ex, buy_oid, BUY_TIMEOUT)

                if buy_status != "filled":
                    # Timeout (có thể partial) → cancel và check filled
                    cancel_safe(ex, buy_oid)
                    time.sleep(0.3)
                    try:
                        can = ex.fetch_order(buy_oid, SYMBOL)
                        pf = float(can.get("filled", 0) or 0)
                        if pf <= 0:
                            pf = 0.0
                    except Exception:
                        pf = 0.0

                    if pf > 0 and (not min_amt or pf >= min_amt):
                        pa = float(can.get("average", 0) or can.get("price", 0) or buy_price)
                        pc_val = float(can.get("cost", 0) or pa * pf)

                        log(f"[{EXCHANGE_ID.upper()}] ⚡ Partial MUA: {pf}/{buy_qty} @ {fmt(pa)}", Y)

                        # Bán thủ công phần partial
                        inv_bal()
                        b_now = bal(ex, BASE)
                        sell_amt_p = s_amt(ex, b_now) if b_now > 0 else s_amt(ex, pf * (1 - FEE_RATE))
                        sp_manual = s_price(ex, pa * (1 + SPREAD_PCT / 100))

                        trade["buy_price"] = pa
                        trade["amount"] = pf
                        trade["status"] = "→ BÁN partial"

                        tlog.write({"cycle": cycle, "timestamp": ts_now, "side": "BUY",
                                    "order_price": buy_price, "filled_price": pa,
                                    "amount": buy_qty, "filled_amount": pf,
                                    "cost": pc_val, "status": "partial", "order_id": buy_oid,
                                    "pnl": 0, "duration_s": 0,
                                    "fee_quote": round(pc_val * FEE_RATE, 2),
                                    "note": f"{EXCHANGE_ID} partial buy {pf}/{buy_qty}"})

                        buy_fp = pa
                        buy_fa = pf
                        buy_cost = pc_val
                        buy_filled_at = time.time()

                        try:
                            so = ex.create_limit_sell_order(SYMBOL, sell_amt_p, sp_manual)
                            inv_bal()
                            sell_oid = so["id"]
                        except Exception as e:
                            log(f"[{EXCHANGE_ID.upper()}] Lỗi đặt SELL partial: {e}", R)
                            trade["status"] = "LỖI"
                            time.sleep(COOLDOWN)
                            continue

                        open_t = {"cycle": cycle, "buy_price": buy_fp,
                                  "sell_target": sp_manual, "amount": sell_amt_p}
                        last_bid, last_ask = bid, ask

                        def sell_cb(elapsed):
                            nonlocal last_bid, last_ask
                            trade["status"] = f"→ BÁN ({int(elapsed)}s)"
                            try:
                                last_bid, last_ask = fetch_ba(ex)
                            except Exception:
                                pass
                            display(cycle, last_bid, last_ask,
                                    f"[#{cycle}] Chờ BÁN #{sell_oid} @ {fmt(sp_manual)}... ({int(elapsed)}s)",
                                    summary, history, bal(ex, QUOTE), sell_amt_p, open_t, rate_status(ex))

                        display(cycle, bid, ask,
                                f"[#{cycle}] ✓ MUA partial {buy_fa} @ {fmt(buy_fp)} → chờ BÁN @ {fmt(sp_manual)}",
                                summary, history, bal(ex, QUOTE), sell_amt_p, open_t, rate_status(ex))

                        sell_result, sell_status = wait_buy(ex, sell_oid, OTO_SELL_TIMEOUT)
                        # SELL timeout -> hủy và đặt lại theo ASK thấp nhất, sau đó quay lại quét
                        if sell_status == "timeout":
                            cancel_safe(ex, sell_oid)
                            time.sleep(0.2)
                            try:
                                sr_now = ex.fetch_order(sell_oid, SYMBOL)
                            except Exception:
                                sr_now = {}
                            sf_partial = float(sr_now.get("filled", 0) or 0)
                            rem = max(0.0, sell_amt_p - sf_partial)
                            rem = s_amt(ex, rem) if rem > 0 else 0.0
                            try:
                                _, ask_now = fetch_ba(ex)
                                ask_reprice = s_price(ex, ask_now)
                            except Exception:
                                ask_reprice = sp_manual

                            if rem > 0 and (not min_amt or rem >= min_amt):
                                try:
                                    so_retry = ex.create_limit_sell_order(SYMBOL, rem, ask_reprice)
                                    inv_bal()
                                    summary["retries"] += 1
                                    trade["status"] = "↻ reprice ASK"
                                    log(
                                        f"[{EXCHANGE_ID.upper()}] SELL timeout {OTO_SELL_TIMEOUT}s → hủy #{sell_oid}, "
                                        f"đặt lại {rem} @ ASK {fmt(ask_reprice)} (#{so_retry['id']})",
                                        Y,
                                    )
                                except Exception as e:
                                    log(f"[{EXCHANGE_ID.upper()}] SELL timeout, lỗi đặt lại ASK: {e}", R)
                            else:
                                summary["retries"] += 1
                                trade["status"] = "↻ reprice bỏ qua (rem quá nhỏ)"
                                log(f"[{EXCHANGE_ID.upper()}] SELL timeout {OTO_SELL_TIMEOUT}s, lượng còn lại quá nhỏ ({fmt(rem)})", Y)

                            time.sleep(COOLDOWN)
                            continue

                        # Xử lý sell
                        if sell_status == "cancelled" or sell_result.get("status") == "canceled":
                            sp_filled = float(sell_result.get("filled", 0) or 0)
                            sp_cost = float(sell_result.get("cost", 0) or 0)
                            dur = round(time.time() - buy_filled_at, 1) if buy_filled_at else 0

                            if sp_filled > 0:
                                ppnl = net_pnl(sp_cost, buy_cost)
                                fees = est_fees(sp_cost, buy_cost)
                                trade["sell_price"] = float(sell_result.get("average", 0) or sp_manual)
                                trade["pnl"] = ppnl
                                trade["dur"] = dur
                                trade["status"] = "⚡partial"
                                summary["total"] += 1
                                summary["total_pnl"] += ppnl
                                summary["total_fees"] += fees

                                tlog.write({"cycle": cycle, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                            "side": "SELL", "order_price": sp_manual,
                                            "filled_price": trade["sell_price"], "amount": sell_amt_p,
                                            "filled_amount": sp_filled, "cost": sp_cost,
                                            "status": "partial", "order_id": sell_oid,
                                            "pnl": round(ppnl, 2), "duration_s": dur,
                                            "fee_quote": round(fees, 2), "note": f"{EXCHANGE_ID} partial sell"})
                            else:
                                trade["status"] = "HỦY"
                            time.sleep(COOLDOWN)
                            continue

                        sf_price = float(sell_result.get("average", 0) or sell_result.get("price", 0) or sp_manual)
                        sf_amt = float(sell_result.get("filled", 0) or sell_amt_p)
                        sf_cost = float(sell_result.get("cost", 0) or sf_price * sf_amt)
                        dur = round(time.time() - buy_filled_at, 1) if buy_filled_at else 0

                        pnl = net_pnl(sf_cost, buy_cost)
                        fees = est_fees(sf_cost, buy_cost)
                        pc_c = G if pnl >= 0 else R
                        log(f"[{EXCHANGE_ID.upper()}] ✓ BÁN partial @ {fmt(sf_price)} → PnL ròng: {pc_c}{B}{fmt(pnl)} {QUOTE}{X} (phí ~{fmt(fees)}) ⏱ {dur}s", G)

                        tlog.write({"cycle": cycle, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "side": "SELL", "order_price": sp_manual,
                                    "filled_price": sf_price, "amount": sell_amt_p,
                                    "filled_amount": sf_amt, "cost": sf_cost,
                                    "status": "filled", "order_id": sell_oid,
                                    "pnl": round(pnl, 2), "duration_s": dur,
                                    "fee_quote": round(fees, 2), "note": f"{EXCHANGE_ID} spread"})

                        trade["sell_price"] = sf_price
                        trade["pnl"] = pnl
                        trade["dur"] = dur
                        trade["status"] = "✓" if pnl >= 0 else "✗"
                        summary["total"] += 1
                        summary["total_pnl"] += pnl
                        summary["total_fees"] += fees
                        if pnl > 0: summary["wins"] += 1
                        elif pnl < 0: summary["losses"] += 1

                        inv_bal()
                        try:
                            nb, na = fetch_ba(ex)
                        except Exception:
                            nb, na = bid, ask
                        q = bal(ex, QUOTE); b = bal(ex, BASE)
                        display(cycle, nb, na,
                                f"Cycle #{cycle} xong. PnL: {fmt(pnl)} {QUOTE}. Số dư: {fmt(q)} {QUOTE}",
                                summary, history, q, b, rl=rate_status(ex))
                        pause_after_trade()
                        continue

                    # Không có partial → retry (pf=0 hoặc dưới min_amt; không có biến sp_filled ở đây)
                    summary["retries"] += 1
                    trade["status"] = "✗ hủy"
                    log(f"[{EXCHANGE_ID.upper()}] ✗ Không khớp {BUY_TIMEOUT}s → hủy → quét lại", Y)
                    time.sleep(COOLDOWN)
                    continue

                # BUY full
                buy_fp = float(buy_result.get("average", 0) or buy_result.get("price", 0) or buy_price)
                buy_fa = float(buy_result.get("filled", 0) or buy_qty)
                buy_cost = float(buy_result.get("cost", 0) or buy_fp * buy_fa)
                buy_filled_at = time.time()

                log(f"[{EXCHANGE_ID.upper()}] ✓ MUA khớp: {buy_fa} @ {fmt(buy_fp)}", G)

                tlog.write({"cycle": cycle, "timestamp": ts_now, "side": "BUY",
                            "order_price": buy_price, "filled_price": buy_fp,
                            "amount": buy_qty, "filled_amount": buy_fa,
                            "cost": buy_cost, "status": "filled", "order_id": buy_oid,
                            "pnl": 0, "duration_s": 0,
                            "fee_quote": round(buy_cost * FEE_RATE, 2),
                            "note": f"{EXCHANGE_ID} buy, sell @ {fmt(sell_price)}"})

                trade["buy_price"] = buy_fp
                trade["status"] = "→ BÁN"

                # Place SELL
                try:
                    so = ex.create_limit_sell_order(SYMBOL, sell_qty, sell_price)
                    inv_bal()
                    sell_oid = so["id"]
                except Exception as e:
                    log(f"[{EXCHANGE_ID.upper()}] Lỗi đặt SELL: {e}", R)
                    summary["retries"] += 1
                    time.sleep(COOLDOWN)
                    continue

                open_t = {"cycle": cycle, "buy_price": buy_fp,
                          "sell_target": sell_price, "amount": sell_qty}
                last_bid, last_ask = bid, ask

                def sell_cb(elapsed):
                    nonlocal last_bid, last_ask
                    trade["status"] = f"→ BÁN ({int(elapsed)}s)"
                    try:
                        last_bid, last_ask = fetch_ba(ex)
                    except Exception:
                        pass
                    display(cycle, last_bid, last_ask,
                            f"[#{cycle}] Chờ BÁN #{sell_oid} @ {fmt(sell_price)}... ({int(elapsed)}s)",
                            summary, history, bal(ex, QUOTE), sell_qty, open_t, rate_status(ex))

                display(cycle, bid, ask,
                        f"[#{cycle}] ✓ MUA {buy_fa} @ {fmt(buy_fp)} → chờ BÁN @ {fmt(sell_price)}",
                        summary, history, bal(ex, QUOTE), sell_qty, open_t, rate_status(ex))

                sell_result, sell_status = wait_buy(ex, sell_oid, OTO_SELL_TIMEOUT)

                # SELL timeout -> hủy và đặt lại theo ASK thấp nhất, sau đó quay lại quét
                if sell_status == "timeout":
                    cancel_safe(ex, sell_oid)
                    time.sleep(0.2)
                    try:
                        sr_now = ex.fetch_order(sell_oid, SYMBOL)
                    except Exception:
                        sr_now = {}
                    sf_partial = float(sr_now.get("filled", 0) or 0)
                    rem = max(0.0, sell_qty - sf_partial)
                    rem = s_amt(ex, rem) if rem > 0 else 0.0
                    try:
                        _, ask_now = fetch_ba(ex)
                        ask_reprice = s_price(ex, ask_now)
                    except Exception:
                        ask_reprice = sell_price

                    if rem > 0 and (not min_amt or rem >= min_amt):
                        try:
                            so_retry = ex.create_limit_sell_order(SYMBOL, rem, ask_reprice)
                            inv_bal()
                            summary["retries"] += 1
                            trade["status"] = "↻ reprice ASK"
                            log(
                                f"[{EXCHANGE_ID.upper()}] SELL timeout {OTO_SELL_TIMEOUT}s → hủy #{sell_oid}, "
                                f"đặt lại {rem} @ ASK {fmt(ask_reprice)} (#{so_retry['id']})",
                                Y,
                            )
                        except Exception as e:
                            log(f"[{EXCHANGE_ID.upper()}] SELL timeout, lỗi đặt lại ASK: {e}", R)
                    else:
                        summary["retries"] += 1
                        trade["status"] = "↻ reprice bỏ qua (rem quá nhỏ)"
                        log(f"[{EXCHANGE_ID.upper()}] SELL timeout {OTO_SELL_TIMEOUT}s, lượng còn lại quá nhỏ ({fmt(rem)})", Y)

                    time.sleep(COOLDOWN)
                    continue

                if sell_status == "cancelled" or sell_result.get("status") == "canceled":
                    sp_filled = float(sell_result.get("filled", 0) or 0)
                    sp_cost = float(sell_result.get("cost", 0) or 0)
                    dur = round(time.time() - buy_filled_at, 1) if buy_filled_at else 0

                    if sp_filled > 0:
                        ppnl = net_pnl(sp_cost, buy_cost)
                        fees = est_fees(sp_cost, buy_cost)
                        trade["sell_price"] = float(sell_result.get("average", 0) or sell_price)
                        trade["pnl"] = ppnl
                        trade["dur"] = dur
                        trade["status"] = "⚡partial"
                        summary["total"] += 1
                        summary["total_pnl"] += ppnl
                        summary["total_fees"] += fees

                        tlog.write({"cycle": cycle, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "side": "SELL", "order_price": sell_price,
                                    "filled_price": trade["sell_price"], "amount": sell_qty,
                                    "filled_amount": sp_filled, "cost": sp_cost,
                                    "status": "partial", "order_id": sell_oid,
                                    "pnl": round(ppnl, 2), "duration_s": dur,
                                    "fee_quote": round(fees, 2), "note": f"{EXCHANGE_ID} partial sell"})
                    else:
                        trade["status"] = "HỦY"
                    time.sleep(COOLDOWN)
                    continue

                sf_price = float(sell_result.get("average", 0) or sell_result.get("price", 0) or sell_price)
                sf_amt = float(sell_result.get("filled", 0) or sell_qty)
                sf_cost = float(sell_result.get("cost", 0) or sf_price * sf_amt)
                dur = round(time.time() - buy_filled_at, 1) if buy_filled_at else 0

                pnl = net_pnl(sf_cost, buy_cost)
                fees = est_fees(sf_cost, buy_cost)
                pc_c = G if pnl >= 0 else R
                log(f"[{EXCHANGE_ID.upper()}] ✓ BÁN @ {fmt(sf_price)} → PnL ròng: {pc_c}{B}{fmt(pnl)} {QUOTE}{X} (phí ~{fmt(fees)}) ⏱ {dur}s", G)

                tlog.write({"cycle": cycle, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "side": "SELL", "order_price": sell_price,
                            "filled_price": sf_price, "amount": sell_qty,
                            "filled_amount": sf_amt, "cost": sf_cost,
                            "status": "filled", "order_id": sell_oid,
                            "pnl": round(pnl, 2), "duration_s": dur,
                            "fee_quote": round(fees, 2), "note": f"{EXCHANGE_ID} spread"})

                trade["sell_price"] = sf_price
                trade["pnl"] = pnl
                trade["dur"] = dur
                trade["status"] = "✓" if pnl >= 0 else "✗"
                summary["total"] += 1
                summary["total_pnl"] += pnl
                summary["total_fees"] += fees
                if pnl > 0: summary["wins"] += 1
                elif pnl < 0: summary["losses"] += 1

                inv_bal()
                q = bal(ex, QUOTE); b = bal(ex, BASE)
                try:
                    nb, na = fetch_ba(ex)
                except Exception:
                    nb, na = bid, ask
                display(cycle, nb, na,
                        f"Cycle #{cycle} xong. PnL: {fmt(pnl)} {QUOTE}. Số dư: {fmt(q)} {QUOTE}",
                        summary, history, q, b, rl=rate_status(ex))

                pause_after_trade()
                continue

            #  OTO: 1 lệnh = BUY LIMIT + auto SELL LIMIT
            # ══════════════════════════════════════════════
            display(cycle, bid, ask,
                    f"[#{cycle}] OTO: MUA {buy_qty} @ {fmt(buy_price)} → BÁN @ {fmt(sell_price)}",
                    summary, history, q, b, rl=rate_status(ex))

            try:
                log(f"[OTO] BUY {buy_qty} @ {fmt(buy_price)} + SELL {sell_qty} @ {fmt(sell_price)}", C)
                oto = place_oto(ex, buy_price, buy_qty, sell_price, sell_qty)
                inv_bal()
            except Exception as e:
                log(f"[OTO] Lỗi đặt lệnh: {e}", R)
                time.sleep(COOLDOWN); cycle -= 1; continue

            oto_id = oto.get("orderListId")
            orders = oto.get("orderReports") or oto.get("orders", [])
            if len(orders) < 2:
                log(f"[OTO] Response bất thường: {oto}", R)
                time.sleep(COOLDOWN); cycle -= 1; continue

            buy_oid = orders[0].get("orderId")
            sell_oid = orders[1].get("orderId")

            buy_immediately_filled = orders[0].get("status") == "FILLED"

            trade = {"cycle": cycle, "time": ts_short, "buy_price": buy_price,
                     "sell_price": None, "amount": buy_qty, "pnl": 0,
                     "dur": None, "status": "OTO chờ..."}
            history.append(trade)

            # ── Chờ BUY fill (hoặc đã fill ngay) ──
            if buy_immediately_filled:
                buy_fp = float(orders[0].get("price") or buy_price)
                buy_fa = float(orders[0].get("executedQty") or buy_qty)
                buy_cost = float(orders[0].get("cummulativeQuoteQty") or buy_fp * buy_fa)
                log(f"[OTO] ✓ MUA khớp ngay: {buy_fa} @ {fmt(buy_fp)}", G)
            else:
                display(cycle, bid, ask,
                        f"[#{cycle}] Chờ MUA #{buy_oid} @ {fmt(buy_price)}...",
                        summary, history, q, b, rl=rate_status(ex))

                buy_result, buy_status = wait_buy(ex, buy_oid, BUY_TIMEOUT)

                if buy_status == "filled":
                    buy_fp = float(buy_result.get("average", 0) or buy_result.get("price", 0) or buy_price)
                    buy_fa = float(buy_result.get("filled", 0) or buy_qty)
                    buy_cost = float(buy_result.get("cost", 0) or buy_fp * buy_fa)
                    log(f"[OTO] ✓ MUA khớp: {buy_fa} @ {fmt(buy_fp)}", G)
                else:
                    # Timeout — hủy OTO
                    cancel_order_list(ex, oto_id)
                    time.sleep(0.3)

                    try:
                        can = ex.fetch_order(buy_oid, SYMBOL)
                        pf = float(can.get("filled", 0) or 0)
                    except Exception:
                        pf = 0

                    if pf > 0 and (not min_amt or pf >= min_amt):
                        pa = float(can.get("average", 0) or can.get("price", 0) or buy_price)
                        pc_val = float(can.get("cost", 0) or pa * pf)
                        log(f"[OTO] ⚡ Partial MUA: {pf}/{buy_qty} @ {fmt(pa)}", Y)

                        # Bán thủ công phần partial
                        inv_bal()
                        b_now = bal(ex, BASE)
                        sell_amt_p = s_amt(ex, b_now) if b_now > 0 else s_amt(ex, pf * (1 - FEE_RATE))
                        sp_manual = s_price(ex, pa * (1 + SPREAD_PCT / 100))

                        trade["buy_price"] = pa
                        trade["amount"] = pf
                        trade["status"] = "→ BÁN partial"

                        tlog.write({"cycle": cycle, "timestamp": ts_now, "side": "BUY",
                                    "order_price": buy_price, "filled_price": pa,
                                    "amount": buy_qty, "filled_amount": pf,
                                    "cost": pc_val, "status": "partial", "order_id": buy_oid,
                                    "pnl": 0, "duration_s": 0, "fee_quote": round(pc_val * FEE_RATE, 2),
                                    "note": f"OTO partial {pf}/{buy_qty}"})

                        try:
                            so = ex.create_limit_sell_order(SYMBOL, sell_amt_p, sp_manual)
                            inv_bal()
                            sell_oid = so["id"]
                            buy_fp = pa; buy_fa = pf; buy_cost = pc_val
                            # Fall through to sell wait below
                        except Exception as e:
                            log(f"[BÁN partial] Lỗi: {e}", R)
                            trade["status"] = "LỖI"
                            time.sleep(COOLDOWN); continue
                    else:
                        summary["retries"] += 1
                        trade["status"] = "✗ hủy"
                        log(f"[OTO] ✗ Không khớp {BUY_TIMEOUT}s → hủy → quét lại", Y)
                        time.sleep(COOLDOWN); continue

            buy_filled_at = time.time()
            tlog.write({"cycle": cycle, "timestamp": ts_now, "side": "BUY",
                        "order_price": buy_price, "filled_price": buy_fp,
                        "amount": buy_qty, "filled_amount": buy_fa,
                        "cost": buy_cost, "status": "filled", "order_id": buy_oid,
                        "pnl": 0, "duration_s": 0, "fee_quote": round(buy_cost * FEE_RATE, 2),
                        "note": f"OTO buy, sell auto @ {fmt(sell_price)}"})

            trade["buy_price"] = buy_fp
            trade["status"] = "→ BÁN"

            # ── Chờ SELL fill (auto-placed bởi OTO) ──
            open_t = {"cycle": cycle, "buy_price": buy_fp,
                      "sell_target": sell_price, "amount": sell_qty}

            last_bid, last_ask = bid, ask

            def sell_cb(elapsed):
                nonlocal last_bid, last_ask
                trade["status"] = f"→ BÁN ({int(elapsed)}s)"
                try: last_bid, last_ask = fetch_ba(ex)
                except Exception: pass
                display(cycle, last_bid, last_ask,
                        f"[#{cycle}] Chờ BÁN #{sell_oid} @ {fmt(sell_price)}... ({int(elapsed)}s)",
                        summary, history, bal(ex, QUOTE), sell_qty, open_t, rate_status(ex))

            display(cycle, bid, ask,
                    f"[#{cycle}] ✓ MUA {buy_fa} @ {fmt(buy_fp)} → chờ BÁN @ {fmt(sell_price)}",
                    summary, history, bal(ex, QUOTE), sell_qty, open_t, rate_status(ex))

            # Chỉ chờ SELL của OTO trong OTO_SELL_TIMEOUT giây.
            # Quá thời gian: hủy SELL hiện tại, đặt lại ở ASK thấp nhất rồi quay lại chu kỳ quét.
            sell_result, sell_status = wait_buy(ex, sell_oid, OTO_SELL_TIMEOUT)
            if sell_status == "timeout":
                cancel_safe(ex, sell_oid)
                time.sleep(0.2)
                try:
                    sr_now = ex.fetch_order(sell_oid, SYMBOL)
                except Exception:
                    sr_now = {}

                sf_partial = float(sr_now.get("filled", 0) or 0)
                rem = max(0.0, sell_qty - sf_partial)
                rem = s_amt(ex, rem) if rem > 0 else 0.0

                try:
                    _, ask_now = fetch_ba(ex)
                    ask_reprice = s_price(ex, ask_now)
                except Exception:
                    ask_reprice = sell_price

                if rem > 0 and (not min_amt or rem >= min_amt):
                    try:
                        so_retry = ex.create_limit_sell_order(SYMBOL, rem, ask_reprice)
                        inv_bal()
                        summary["retries"] += 1
                        trade["status"] = "↻ reprice ASK"
                        log(
                            f"[OTO] SELL timeout {OTO_SELL_TIMEOUT}s → hủy #{sell_oid}, "
                            f"đặt lại {rem} @ ASK {fmt(ask_reprice)} (#{so_retry['id']})",
                            Y,
                        )
                    except Exception as e:
                        log(f"[OTO] SELL timeout, lỗi đặt lại ASK: {e}", R)
                else:
                    summary["retries"] += 1
                    trade["status"] = "↻ reprice bỏ qua (rem quá nhỏ)"
                    log(f"[OTO] SELL timeout {OTO_SELL_TIMEOUT}s, lượng còn lại quá nhỏ ({fmt(rem)})", Y)

                time.sleep(COOLDOWN)
                continue

            if sell_status == "cancelled" or sell_result.get("status") == "canceled":
                sp_filled = float(sell_result.get("filled", 0) or 0)
                sp_cost = float(sell_result.get("cost", 0) or 0)
                dur = round(time.time() - buy_filled_at, 1)

                if sp_filled > 0:
                    ppnl = net_pnl(sp_cost, buy_cost)
                    fees = est_fees(sp_cost, buy_cost)
                    trade["sell_price"] = float(sell_result.get("average", 0) or sell_price)
                    trade["pnl"] = ppnl; trade["dur"] = dur; trade["status"] = "⚡partial"
                    summary["total"] += 1; summary["total_pnl"] += ppnl; summary["total_fees"] += fees
                    tlog.write({"cycle": cycle, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "side": "SELL", "order_price": sell_price,
                                "filled_price": trade["sell_price"], "amount": sell_qty,
                                "filled_amount": sp_filled, "cost": sp_cost,
                                "status": "partial", "order_id": sell_oid,
                                "pnl": round(ppnl, 2), "duration_s": dur,
                                "fee_quote": round(fees, 2), "note": "partial sell"})
                else:
                    trade["status"] = "HỦY"
                if sp_filled > 0:
                    pause_after_trade()
                else:
                    time.sleep(COOLDOWN)
                continue

            # ── Bán khớp! ──
            sf_price = float(sell_result.get("average", 0) or sell_result.get("price", 0) or sell_price)
            sf_amt = float(sell_result.get("filled", 0) or sell_qty)
            sf_cost = float(sell_result.get("cost", 0) or sf_price * sf_amt)
            dur = round(time.time() - buy_filled_at, 1)

            pnl = net_pnl(sf_cost, buy_cost)
            fees = est_fees(sf_cost, buy_cost)

            pc_c = G if pnl >= 0 else R
            log(f"[OTO] ✓ BÁN @ {fmt(sf_price)} → PnL ròng: {pc_c}{B}{fmt(pnl)} {QUOTE}{X} "
                f"(phí ~{fmt(fees)})  ⏱ {dur}s", G)

            tlog.write({"cycle": cycle, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "side": "SELL", "order_price": sell_price,
                        "filled_price": sf_price, "amount": sell_qty,
                        "filled_amount": sf_amt, "cost": sf_cost,
                        "status": "filled", "order_id": sell_oid,
                        "pnl": round(pnl, 2), "duration_s": dur,
                        "fee_quote": round(fees, 2), "note": f"OTO spread={SPREAD_PCT}%"})

            trade["sell_price"] = sf_price
            trade["pnl"] = pnl
            trade["dur"] = dur
            trade["status"] = "✓" if pnl >= 0 else "✗"
            summary["total"] += 1
            summary["total_pnl"] += pnl
            summary["total_fees"] += fees
            if pnl > 0:   summary["wins"] += 1
            elif pnl < 0:  summary["losses"] += 1

            inv_bal()
            q = bal(ex, QUOTE); b = bal(ex, BASE)
            try: bid, ask = fetch_ba(ex)
            except Exception: pass
            display(cycle, bid, ask,
                    f"Cycle #{cycle} xong. PnL: {fmt(pnl)} {QUOTE}. Số dư: {fmt(q)} {QUOTE}",
                    summary, history, q, b, rl=rate_status(ex))

            pause_after_trade()

    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"Lỗi: {e}", R)
        import traceback; traceback.print_exc()
    finally:
        print(f"\n\n  {B}{'═'*60}{X}")
        print(f"  {B}  BOT DỪNG{X}")
        print(f"  {B}{'═'*60}{X}")
        print(f"  Chu kỳ: {summary['total']}  |  Lãi: {G}{summary['wins']}{X}  |  "
              f"Lỗ: {R}{summary['losses']}{X}  |  Retry: {Y}{summary['retries']}{X}")
        pc_c = G if summary["total_pnl"] >= 0 else R
        print(f"  PnL ròng: {pc_c}{B}{fmt(summary['total_pnl'])} {QUOTE}{X}")
        print(f"  Tổng phí: {Y}{fmt(summary['total_fees'])} {QUOTE}{X}")

        try:
            q = bal(ex, QUOTE); b = bal(ex, BASE)
            print(f"  Số dư cuối: {C}{fmt(q)} {QUOTE}{X} + {C}{fmt(b)} {BASE}{X}")
        except Exception: pass

        if history:
            print(f"\n  {B}{'#':>4}  {'Giờ':<10} {'Mua @':>16} {'Bán @':>16} "
                  f"{'SL':>12} {'PnL ròng':>14} {'T.gian':>8}{X}")
            print(f"  {'─'*80}")
            for h in history:
                pv = h.get("pnl", 0); d = h.get("dur")
                ds = f"{d:.0f}s" if d is not None else "—"
                hpc = G if pv > 0 else (R if pv < 0 else D)
                sp = fmt(h["sell_price"]) if h.get("sell_price") else "—"
                pp = fmt(pv) if h.get("sell_price") else "—"
                print(f"  {hpc}{h['cycle']:>4}  {h['time']:<10} {fmt(h['buy_price']):>16} "
                      f"{sp:>16} {h['amount']:>12} {pp:>14} {ds:>8}{X}")

        print(f"\n  {D}CSV export: OFF{X}")
        print()


if __name__ == "__main__":
    main()
