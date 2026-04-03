import ccxt
import sys
import time
import os
from datetime import datetime

if os.name == "nt":
    os.system("chcp 65001 > nul")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ═══════════════════════════════════════════════════════════════
#  CẤU HÌNH
# ═══════════════════════════════════════════════════════════════

EXCHANGE_NAME = "binance"
REFRESH_INTERVAL = 0.5  # giây

# ═══════════════════════════════════════════════════════════════

GREEN  = "\033[92m"
RED    = "\033[91m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
WHITE  = "\033[97m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"


def create_exchange():
    ex_class = getattr(ccxt, EXCHANGE_NAME, None)
    if ex_class is None:
        print(f"{RED}Exchange '{EXCHANGE_NAME}' không tồn tại.{RESET}")
        raise SystemExit(1)
    return ex_class({"enableRateLimit": True})


def normalize_symbol(raw_input, exchange):
    """Chuyển đổi input như 'BTCUSDT', 'btc/usdt', 'ethbtc' thành symbol chuẩn ccxt."""
    s = raw_input.strip().upper()

    if "/" in s:
        candidate = s
        if candidate in exchange.markets:
            return candidate
        raise ValueError(f"Cặp '{candidate}' không tồn tại trên {EXCHANGE_NAME}")

    quote_currencies = [
        "USDT", "USDC", "BUSD", "FDUSD", "TUSD",
        "BTC", "ETH", "BNB",
        "JPY", "EUR", "GBP", "TRY", "BRL", "ARS",
        "BIDR", "DAI", "IDRT", "UAH", "NGN", "PLN", "RON", "ZAR",
    ]

    for quote in quote_currencies:
        if s.endswith(quote) and len(s) > len(quote):
            base = s[: -len(quote)]
            candidate = f"{base}/{quote}"
            if candidate in exchange.markets:
                return candidate

    raise ValueError(
        f"Không nhận dạng được cặp '{raw_input}'. "
        f"Thử nhập dạng 'BTC/USDT' hoặc 'BTCUSDT'."
    )


def get_quote_currency(symbol):
    return symbol.split("/")[1] if "/" in symbol else "?"


def fetch_bid_ask(exchange, symbol):
    ob = exchange.fetch_order_book(symbol, limit=5)
    if not ob["bids"] or not ob["asks"]:
        raise ValueError(f"Order book trống cho {symbol}")
    return (
        ob["bids"][0][0],
        ob["asks"][0][0],
        ob["bids"][0][1],
        ob["asks"][0][1],
    )


_vol_cache = {"data": None, "ts": 0}
VOL_REFRESH = 15

def fetch_1h_volume(exchange, symbol):
    now = time.time()
    if _vol_cache["data"] is not None and (now - _vol_cache["ts"]) < VOL_REFRESH:
        return _vol_cache["data"]
    try:
        candles = exchange.fetch_ohlcv(symbol, timeframe="1h", limit=2)
        if candles and len(candles) >= 1:
            latest = candles[-1]
            vol_base = latest[5]
            vol_quote = latest[5] * ((latest[2] + latest[3]) / 2)
            high = latest[2]
            low = latest[3]
            _vol_cache["data"] = {"base": vol_base, "quote": vol_quote,
                                  "high": high, "low": low}
            _vol_cache["ts"] = now
            return _vol_cache["data"]
    except Exception:
        pass
    return _vol_cache["data"]


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def auto_fmt(price):
    """Chọn format phù hợp dựa trên độ lớn giá."""
    if price >= 1000:
        return f"{price:>20,.2f}"
    if price >= 1:
        return f"{price:>20,.4f}"
    if price >= 0.01:
        return f"{price:>20,.6f}"
    return f"{price:>20.8f}"


def fmt_vol(v):
    if v >= 1_000_000_000: return f"{v / 1_000_000_000:,.2f}B"
    if v >= 1_000_000:     return f"{v / 1_000_000:,.2f}M"
    if v >= 1_000:         return f"{v / 1_000:,.2f}K"
    return f"{v:,.2f}"


def render(symbol, quote, base, bid, ask, bid_vol, ask_vol,
           spread, spread_pct, history, vol_1h=None):
    clear_screen()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    w = 56

    mid = (bid + ask) / 2
    color_spread = GREEN if spread_pct < 0.01 else YELLOW if spread_pct < 0.05 else RED

    title = f"{symbol} SPREAD MONITOR"

    print()
    print(f"  {BOLD}╔{'═' * w}╗{RESET}")
    print(f"  {BOLD}║{title:^{w}}║{RESET}")
    print(f"  {BOLD}╚{'═' * w}╝{RESET}")
    print(f"  {DIM}Exchange: {EXCHANGE_NAME}  |  {now}  |  Refresh: {REFRESH_INTERVAL}s{RESET}")
    print()

    print(f"  ┌{'─' * w}┐")
    print(f"  │  {RED}{BOLD}BID (mua){RESET}  {CYAN}{BOLD}{auto_fmt(bid)}{RESET} {quote:<5} {DIM}vol: {bid_vol:.4f}{RESET}   │")
    print(f"  │  {GREEN}{BOLD}ASK (bán){RESET}  {CYAN}{BOLD}{auto_fmt(ask)}{RESET} {quote:<5} {DIM}vol: {ask_vol:.4f}{RESET}   │")
    print(f"  ├{'─' * w}┤")
    print(f"  │  Mid price   {auto_fmt(mid)} {quote:<5}                    │")
    print(f"  ├{'─' * w}┤")
    print(f"  │  {BOLD}Spread{RESET}      {color_spread}{BOLD}{auto_fmt(spread)}{RESET} {quote:<5}                    │")
    print(f"  │  {BOLD}Spread %{RESET}    {color_spread}{BOLD}{spread_pct:>20.6f}{RESET} %                       │")
    print(f"  └{'─' * w}┘")

    if vol_1h:
        print()
        print(f"  ┌{'─' * w}┐")
        print(f"  │  {BOLD}Volume 1H gần nhất{RESET}{'':>{w - 22}}│")
        print(f"  ├{'─' * w}┤")
        print(f"  │  Vol ({base})     {CYAN}{BOLD}{fmt_vol(vol_1h['base']):>20}{RESET}                    │")
        print(f"  │  Vol ({quote})    {CYAN}{BOLD}{fmt_vol(vol_1h['quote']):>20}{RESET}                    │")
        print(f"  │  1H High       {GREEN}{auto_fmt(vol_1h['high'])}{RESET} {quote:<5}                    │")
        print(f"  │  1H Low        {RED}{auto_fmt(vol_1h['low'])}{RESET} {quote:<5}                    │")
        rng = vol_1h["high"] - vol_1h["low"]
        rng_pct = (rng / vol_1h["low"] * 100) if vol_1h["low"] > 0 else 0
        print(f"  │  1H Range      {YELLOW}{auto_fmt(rng)}{RESET} {quote:<5} {DIM}({rng_pct:.4f}%){RESET}       │")
        print(f"  └{'─' * w}┘")

    if len(history) >= 2:
        spreads = [h["spread"] for h in history]
        pcts = [h["pct"] for h in history]
        avg_s = sum(spreads) / len(spreads)
        avg_p = sum(pcts) / len(pcts)

        print()
        print(f"  ┌{'─' * w}┐")
        print(f"  │  {BOLD}Thống kê ({len(history)} mẫu gần nhất){RESET:<{w + 8}}│")
        print(f"  ├{'─' * w}┤")
        print(f"  │  Spread TB   {auto_fmt(avg_s)} {quote:<5} ({avg_p:.6f}%)      │")
        print(f"  │  Spread Min  {auto_fmt(min(spreads))} {quote:<5} ({min(pcts):.6f}%)      │")
        print(f"  │  Spread Max  {auto_fmt(max(spreads))} {quote:<5} ({max(pcts):.6f}%)      │")
        print(f"  └{'─' * w}┘")

    print()
    print(f"  {DIM}Ctrl+C để dừng{RESET}")
    print()


def prompt_symbol(exchange):
    """Hỏi người dùng nhập cặp giao dịch, hỗ trợ gợi ý và kiểm tra."""
    print(f"\n  {BOLD}═══ CHỌN CẶP GIAO DỊCH ═══{RESET}")
    print(f"  {DIM}Exchange: {EXCHANGE_NAME} ({len(exchange.markets):,} cặp){RESET}")
    print()
    print(f"  Nhập cặp giao dịch. Ví dụ:")
    print(f"    {CYAN}BTCUSDT{RESET}  hoặc  {CYAN}BTC/USDT{RESET}")
    print(f"    {CYAN}ETHBTC{RESET}   hoặc  {CYAN}ETH/BTC{RESET}")
    print(f"    {CYAN}BNBJPY{RESET}   hoặc  {CYAN}BNB/JPY{RESET}")
    print()

    while True:
        try:
            raw = input(f"  {BOLD}▶ Nhập cặp: {RESET}").strip()
            if not raw:
                continue
            symbol = normalize_symbol(raw, exchange)
            print(f"  {GREEN}✓ Đã chọn: {symbol}{RESET}\n")
            return symbol
        except ValueError as e:
            print(f"  {RED}✗ {e}{RESET}")

            if raw.strip():
                query = raw.strip().upper().replace("/", "")
                matches = [
                    s for s in sorted(exchange.markets.keys())
                    if query in s.replace("/", "")
                ][:8]
                if matches:
                    print(f"  {YELLOW}Gợi ý: {', '.join(matches)}{RESET}")
            print()


def main():
    print(f"{BOLD}Khởi tạo {EXCHANGE_NAME}...{RESET}")
    exchange = create_exchange()

    print(f"{DIM}Đang tải danh sách cặp giao dịch...{RESET}")
    exchange.load_markets()
    print(f"{GREEN}Đã tải {len(exchange.markets):,} cặp.{RESET}")

    symbol = prompt_symbol(exchange)
    quote = get_quote_currency(symbol)
    base = symbol.split("/")[0] if "/" in symbol else "?"

    history = []
    max_history = 100

    while True:
        try:
            bid, ask, bid_vol, ask_vol = fetch_bid_ask(exchange, symbol)
            vol_1h = fetch_1h_volume(exchange, symbol)

            spread = ask - bid
            spread_pct = (spread / bid) * 100

            history.append({"spread": spread, "pct": spread_pct})
            if len(history) > max_history:
                history.pop(0)

            render(symbol, quote, base, bid, ask, bid_vol, ask_vol,
                   spread, spread_pct, history, vol_1h)

        except ccxt.NetworkError as e:
            print(f"\n  {RED}Lỗi mạng: {e}{RESET}")
        except ccxt.ExchangeError as e:
            print(f"\n  {RED}Lỗi exchange: {e}{RESET}")
        except KeyboardInterrupt:
            print(f"\n{BOLD}Đã dừng.{RESET}")
            break
        except Exception as e:
            print(f"\n  {RED}Lỗi: {e}{RESET}")

        try:
            time.sleep(REFRESH_INTERVAL)
        except KeyboardInterrupt:
            print(f"\n{BOLD}Đã dừng.{RESET}")
            break


if __name__ == "__main__":
    main()
