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

EXCHANGE_DEFAULT = "binance"

EXCHANGE_OVERRIDES = {
    # "BTC/JPY": "bitbank",
    # "BNB/JPY": "bitbank",
}

PROFIT_THRESHOLD = 1.01
REFRESH_INTERVAL = 2  # giây

# ═══════════════════════════════════════════════════════════════
#  KỊCH BẢN GIAO DỊCH
# ═══════════════════════════════════════════════════════════════
#
#  KB1: JPY → BTC → BNB → JPY
#    Mua BTC bằng JPY (ask) → Mua BNB bằng BTC (ask) → Bán BNB lấy JPY (bid)
#    profit = C / (A × B)
#
#  KB2: JPY → BNB → BTC → JPY
#    Mua BNB bằng JPY (ask) → Bán BNB lấy BTC (bid) → Bán BTC lấy JPY (bid)
#    profit = (B × C) / A
#
# ═══════════════════════════════════════════════════════════════

SCENARIOS = [
    {
        "id": "KB1",
        "name": "JPY → BTC → BNB → JPY",
        "desc": "Mua BTC bằng JPY → Mua BNB bằng BTC → Bán BNB lấy JPY",
        "steps": [
            {"key": "A", "symbol": "BTC/JPY", "side": "ask", "unit": "JPY", "fmt": ",.2f"},
            {"key": "B", "symbol": "BNB/BTC", "side": "ask", "unit": "BTC", "fmt": ".8f"},
            {"key": "C", "symbol": "BNB/JPY", "side": "bid", "unit": "JPY", "fmt": ",.2f"},
        ],
        "calc": lambda p: p["C"] / (p["A"] * p["B"]),
        "formula_str": "C / (A × B)",
    },
    {
        "id": "KB2",
        "name": "JPY → BNB → BTC → JPY",
        "desc": "Mua BNB bằng JPY → Bán BNB lấy BTC → Bán BTC lấy JPY",
        "steps": [
            {"key": "A", "symbol": "BNB/JPY", "side": "ask", "unit": "JPY", "fmt": ",.2f"},
            {"key": "B", "symbol": "BNB/BTC", "side": "bid", "unit": "BTC", "fmt": ".8f"},
            {"key": "C", "symbol": "BTC/JPY", "side": "bid", "unit": "JPY", "fmt": ",.2f"},
        ],
        "calc": lambda p: (p["B"] * p["C"]) / p["A"],
        "formula_str": "(B × C) / A",
    },
]

# ═══════════════════════════════════════════════════════════════

GREEN  = "\033[92m"
RED    = "\033[91m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
WHITE  = "\033[97m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"
BG_GREEN = "\033[42m"
BG_RED   = "\033[41m"


def create_exchanges():
    exchanges = {}
    all_names = {EXCHANGE_DEFAULT}
    all_names.update(EXCHANGE_OVERRIDES.values())
    for name in all_names:
        ex_class = getattr(ccxt, name, None)
        if ex_class is None:
            print(f"{RED}Exchange '{name}' không tồn tại trong ccxt.{RESET}")
            raise SystemExit(1)
        exchanges[name] = ex_class({"enableRateLimit": True})
    return exchanges


def get_exchange_for(symbol, exchanges):
    name = EXCHANGE_OVERRIDES.get(symbol, EXCHANGE_DEFAULT)
    return name, exchanges[name]


def collect_unique_symbols():
    symbols = set()
    for sc in SCENARIOS:
        for step in sc["steps"]:
            symbols.add(step["symbol"])
    return symbols


def fetch_all_books(exchanges, symbols, limit=5):
    books = {}
    for sym in symbols:
        name, ex = get_exchange_for(sym, exchanges)
        books[sym] = ex.fetch_order_book(sym, limit=limit)
    return books


def get_price(books, symbol, side):
    ob = books[symbol]
    entries = ob["asks"] if side == "ask" else ob["bids"]
    if not entries:
        raise ValueError(f"Không có lệnh {side} cho {symbol}")
    return entries[0][0]


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def fmt_price(value, fmt_spec):
    return format(value, fmt_spec)


def render_all(results):
    clear_screen()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    w = 52

    print()
    print(f"  {BOLD}╔{'═' * w}╗{RESET}")
    print(f"  {BOLD}║{'TRIANGULAR ARBITRAGE MONITOR':^{w}}║{RESET}")
    print(f"  {BOLD}╚{'═' * w}╝{RESET}")
    print(f"  {DIM}Thời gian: {now}  |  Ngưỡng: profit > {PROFIT_THRESHOLD}  |  Refresh: {REFRESH_INTERVAL}s{RESET}")

    any_profitable = False

    for res in results:
        sc = res["scenario"]
        prices = res["prices"]
        profit = res["profit"]
        profit_pct = res["profit_pct"]
        is_profit = profit > PROFIT_THRESHOLD
        if is_profit:
            any_profitable = True

        signal_color = GREEN if is_profit else RED
        signal_icon = "▲" if is_profit else "▼"
        signal_label = "CÓ LỜI" if is_profit else "KHÔNG LỜI"
        bar = f"{signal_color}{BOLD}██{RESET}"

        print()
        print(f"  {BOLD}┌{'─' * w}┐{RESET}")
        print(f"  {BOLD}│  {sc['id']}: {sc['name']:<{w - len(sc['id']) - 4}}│{RESET}")
        print(f"  {BOLD}│  {DIM}{sc['desc']:<{w - 2}}{RESET}{BOLD}│{RESET}")
        print(f"  {BOLD}├{'─' * w}┤{RESET}")

        for step in sc["steps"]:
            key = step["key"]
            val = prices[key]
            side_label = step["side"]
            unit = step["unit"]
            formatted = fmt_price(val, step["fmt"])
            line = f"  {key} = {step['symbol']:>8} ({side_label})  {CYAN}{formatted:>18} {unit}{RESET}"
            print(f"  │{line:<{w + 18}}│")

        print(f"  ├{'─' * w}┤")

        formula_line = f"  profit = {sc['formula_str']}"
        val_line = f"  {YELLOW}{BOLD}{profit:>18.6f}{RESET}"
        pct_line = f"  {YELLOW}{BOLD}{profit_pct:>+17.4f}%{RESET}"

        print(f"  │{formula_line:<{w}}│")
        print(f"  │  profit          = {val_line:<{w + 14}}│")
        print(f"  │  profit %        = {pct_line:<{w + 14}}│")
        print(f"  ├{'─' * w}┤")

        signal_str = f"  {bar}  {signal_color}{BOLD}{signal_label} {signal_icon}{RESET}  profit = {profit:.6f} ({profit_pct:+.4f}%)"
        print(f"  │{signal_str:<{w + 28}}│")
        print(f"  └{'─' * w}┘")

    if any_profitable:
        print()
        print(f"  {BG_GREEN}{WHITE}{BOLD}  ★  CƠ HỘI ARBITRAGE PHÁT HIỆN!  ★  {RESET}")

    print()
    print(f"  {DIM}Ctrl+C để dừng{RESET}")
    print()


def main():
    print(f"{BOLD}Khởi tạo exchange...{RESET}")
    exchanges = create_exchanges()
    symbols = collect_unique_symbols()
    print(f"{BOLD}Theo dõi {len(SCENARIOS)} kịch bản | {len(symbols)} cặp: {', '.join(sorted(symbols))}{RESET}")
    print(f"{BOLD}Bắt đầu...{RESET}\n")

    while True:
        try:
            books = fetch_all_books(exchanges, symbols)

            results = []
            for sc in SCENARIOS:
                prices = {}
                for step in sc["steps"]:
                    prices[step["key"]] = get_price(books, step["symbol"], step["side"])

                profit = sc["calc"](prices)
                profit_pct = (profit - 1) * 100

                results.append({
                    "scenario": sc,
                    "prices": prices,
                    "profit": profit,
                    "profit_pct": profit_pct,
                })

            render_all(results)

        except ccxt.NetworkError as e:
            print(f"\n  {RED}Lỗi mạng: {e}{RESET}")
        except ccxt.ExchangeError as e:
            print(f"\n  {RED}Lỗi exchange: {e}{RESET}")
            print(f"  {YELLOW}Kiểm tra cặp giao dịch và exchange.{RESET}")
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
