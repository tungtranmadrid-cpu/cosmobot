import ccxt
import sys
import os
from datetime import datetime
from dotenv import dotenv_values

if os.name == "nt":
    os.system("chcp 65001 > nul")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
env = dotenv_values(ENV_PATH)

API_KEY = env.get("API_KEY", "")
SECRET_KEY = env.get("SECRET_KEY", "")

if not API_KEY or not SECRET_KEY:
    print("\033[91m  ✗ Thiếu API_KEY hoặc SECRET_KEY trong .env\033[0m")
    sys.exit(1)

GREEN  = "\033[92m"
RED    = "\033[91m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"


def create_exchange():
    exchange = ccxt.binance({
        "apiKey": API_KEY,
        "secret": SECRET_KEY,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    exchange.set_sandbox_mode(True)
    return exchange


FIAT = {"USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USD",
        "EUR", "GBP", "JPY", "TRY", "BRL", "ARS", "ZAR", "UAH",
        "PLN", "MXN", "COP", "CZK", "IDR", "RON", "NGN", "KES"}


def check_balance(exchange):
    print(f"\n  {BOLD}═══ SỐ DƯ TÀI KHOẢN (Binance Testnet) ═══{RESET}\n")
    bal = exchange.fetch_balance()

    non_zero = {}
    for currency, total in bal["total"].items():
        if total > 0:
            non_zero[currency] = total

    PRIORITY = {"BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "DOT", "AVAX", "LINK"}
    coins_to_price = [c for c in non_zero if c not in FIAT and f"{c}/USDT" in exchange.markets]
    prices = {}

    priority_coins = [c for c in coins_to_price if c in PRIORITY]
    for c in priority_coins:
        try:
            t = exchange.fetch_ticker(f"{c}/USDT")
            p = t.get("last") or 0
            if p > 0:
                prices[c] = p
        except Exception:
            pass

    remaining = [c for c in coins_to_price if c not in prices]
    if remaining:
        try:
            syms = [f"{c}/USDT" for c in remaining]
            tickers = exchange.fetch_tickers(syms)
            for sym, t in tickers.items():
                base = sym.split("/")[0]
                p = t.get("last") or 0
                if p > 0:
                    prices[base] = p
        except Exception:
            pass

    total_usdt = 0.0
    rows = []
    for currency, total in non_zero.items():
        free = bal["free"].get(currency, 0)
        used = bal["used"].get(currency, 0)
        if currency == "USDT":
            usdt_val = total
        elif currency in prices:
            usdt_val = total * prices[currency]
        else:
            usdt_val = 0.0
        rows.append((currency, free, used, total, usdt_val))
        total_usdt += usdt_val

    rows.sort(key=lambda r: r[4], reverse=True)

    print(f"  {'Coin':<10} {'Free':>14} {'Locked':>14} {'Total':>14} {'≈ USDT':>14}")
    print(f"  {'─' * 68}")
    for cur, free, used, total, uval in rows:
        if uval < 0.01 and cur not in ("USDT", "BTC", "ETH", "BNB"):
            continue
        c = GREEN if uval > 1 else DIM
        print(f"  {c}{cur:<10} {free:>14.8f} {used:>14.8f} {total:>14.8f} {uval:>14.4f}{RESET}")

    print(f"  {'─' * 68}")
    print(f"  {BOLD}{'Tổng':>54} {total_usdt:>14.4f} USDT{RESET}\n")
    return rows


def sell_all_to_usdt(exchange, assets=("BTC", "ETH", "BNB")):
    print(f"\n  {BOLD}═══ BÁN TẤT CẢ → USDT ═══{RESET}\n")
    exchange.load_markets()
    bal = exchange.fetch_balance()

    results = []
    for coin in assets:
        free = bal["free"].get(coin, 0)
        if free <= 0:
            print(f"  {DIM}{coin}: 0 — bỏ qua{RESET}")
            results.append((coin, 0, 0, "skip", "Không có số dư"))
            continue

        symbol = f"{coin}/USDT"
        if symbol not in exchange.markets:
            print(f"  {RED}{coin}: Không có cặp {symbol}{RESET}")
            results.append((coin, free, 0, "error", f"Không có cặp {symbol}"))
            continue

        market = exchange.markets[symbol]
        min_amount = market.get("limits", {}).get("amount", {}).get("min") or 0
        min_cost = market.get("limits", {}).get("cost", {}).get("min") or 0

        amount = float(exchange.amount_to_precision(symbol, free))
        if amount < min_amount:
            print(f"  {YELLOW}{coin}: {amount} < min_amount {min_amount} — quá ít{RESET}")
            results.append((coin, free, 0, "skip", f"< min amount {min_amount}"))
            continue

        try:
            ticker = exchange.fetch_ticker(symbol)
            est_cost = amount * (ticker.get("last") or 0)
        except Exception:
            est_cost = 0

        if min_cost and est_cost < min_cost:
            print(f"  {YELLOW}{coin}: {amount} ≈ {est_cost:.4f} USDT < min_cost {min_cost} — quá ít{RESET}")
            results.append((coin, free, 0, "skip", f"Notional {est_cost:.4f} < {min_cost}"))
            continue

        try:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"  {CYAN}[{ts}] Bán {amount} {coin} → USDT (market)...{RESET}", end="", flush=True)
            order = exchange.create_market_sell_order(symbol, amount)

            filled = order.get("filled", 0)
            cost = order.get("cost", 0)
            avg = order.get("average", 0) or order.get("price", 0) or 0
            status = order.get("status", "?")

            print(f"  {GREEN}✓ {filled} {coin} @ {avg:.2f} = {cost:.4f} USDT ({status}){RESET}")
            results.append((coin, free, cost, "sold", f"@ {avg:.2f}"))
        except Exception as e:
            print(f"  {RED}✗ Lỗi: {e}{RESET}")
            results.append((coin, free, 0, "error", str(e)))

    print(f"\n  {BOLD}── Kết quả ──{RESET}")
    total_received = 0.0
    for coin, amount, received, status, note in results:
        if status == "sold":
            total_received += received
            c = GREEN
        elif status == "skip":
            c = DIM
        else:
            c = RED
        print(f"  {c}{coin:<6} {amount:>14.8f} → {received:>12.4f} USDT  [{status}] {note}{RESET}")

    print(f"\n  {BOLD}Tổng nhận: {GREEN}{total_received:.4f} USDT{RESET}\n")
    return results


def main():
    print(f"\n  {BOLD}{'═' * 50}{RESET}")
    print(f"  {BOLD}  QUẢN LÝ TÀI KHOẢN — Binance Testnet{RESET}")
    print(f"  {BOLD}{'═' * 50}{RESET}")

    exchange = create_exchange()
    exchange.load_markets()
    print(f"  {GREEN}✓ Kết nối thành công{RESET}")

    while True:
        print(f"\n  {BOLD}Chọn hành động:{RESET}")
        print(f"    {BOLD}1{RESET}. Xem số dư")
        print(f"    {BOLD}2{RESET}. Bán BTC, ETH, BNB → USDT")
        print(f"    {BOLD}3{RESET}. Bán 1 coin cụ thể → USDT")
        print(f"    {BOLD}0{RESET}. Thoát")
        print()

        choice = input(f"  {BOLD}▶ Chọn: {RESET}").strip()

        if choice == "1":
            check_balance(exchange)

        elif choice == "2":
            check_balance(exchange)
            confirm = input(f"  {YELLOW}Bán tất cả BTC, ETH, BNB → USDT? (y/n): {RESET}").strip().lower()
            if confirm == "y":
                sell_all_to_usdt(exchange, ("BTC", "ETH", "BNB"))
                print(f"  {DIM}Số dư sau khi bán:{RESET}")
                check_balance(exchange)
            else:
                print(f"  {DIM}Đã hủy.{RESET}")

        elif choice == "3":
            coin = input(f"  {BOLD}▶ Nhập coin (VD: BTC, SOL, XRP): {RESET}").strip().upper()
            if coin:
                confirm = input(f"  {YELLOW}Bán toàn bộ {coin} → USDT? (y/n): {RESET}").strip().lower()
                if confirm == "y":
                    sell_all_to_usdt(exchange, (coin,))
                    check_balance(exchange)
                else:
                    print(f"  {DIM}Đã hủy.{RESET}")

        elif choice == "0":
            print(f"\n  {DIM}Bye!{RESET}\n")
            break
        else:
            print(f"  {RED}Chọn 0-3.{RESET}")


if __name__ == "__main__":
    main()
