import ccxt
import sys
import time
import os
import argparse
from datetime import datetime


if os.name == "nt":
    os.system("chcp 65001 > nul")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


GREEN = "\033[92m"
RED = "\033[91m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"


def fmt(v: float) -> str:
    if v is None:
        return "—"
    try:
        v = float(v)
    except Exception:
        return str(v)
    abs_v = abs(v)
    if abs_v >= 1_000_000_000:
        return f"{v / 1_000_000_000:,.3f}B"
    if abs_v >= 1_000_000:
        return f"{v / 1_000_000:,.3f}M"
    if abs_v >= 1_000:
        return f"{v / 1_000:,.3f}K"
    return f"{v:,.8f}".rstrip("0").rstrip(".")


def create_exchange():
    raise RuntimeError("create_exchange() is deprecated in this file")


def create_exchange_by_name(name: str):
    name = (name or "").strip().lower()
    if name == "binance":
        ex = ccxt.binance({
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })
        return ex

    if name == "mexc":
        # ccxt typically uses class name 'mexc'
        ex_class = getattr(ccxt, "mexc", None)
        if ex_class is None:
            raise ValueError("ccxt không hỗ trợ exchange 'mexc' trong môi trường hiện tại.")
        return ex_class({"enableRateLimit": True})

    raise ValueError(f"Sàn '{name}' không hỗ trợ. Dùng 'binance' hoặc 'mexc'.")


def normalize_quote_list(raw: str):
    if not raw:
        return []
    return [x.strip().upper() for x in raw.split(",") if x.strip()]


def compute_spread_pct_from_ba(bid: float, ask: float) -> float:
    if bid is None or ask is None:
        return None
    if bid <= 0:
        return None
    return (ask - bid) / bid * 100.0


def fetch_quote_volume_top(ex, quote_filter=None, top_n=50):
    """
    Lấy volume 24h từ fetch_tickers() và trả về danh sách [(symbol, quoteVolume), ...]
    """
    tickers = ex.fetch_tickers()
    rows = []
    for sym, t in tickers.items():
        if not sym or sym not in ex.markets:
            continue
        if "/" not in sym:
            continue
        base, quote = sym.split("/", 1)
        if quote_filter and quote not in quote_filter:
            continue
        qv = t.get("quoteVolume")
        if qv is None:
            # fallback: dùng baseVolume nếu có
            qv = t.get("baseVolume")
        try:
            qv = float(qv) if qv is not None else 0.0
        except Exception:
            qv = 0.0
        if qv > 0:
            rows.append((sym, qv))

    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:top_n]


def fetch_order_book_best_bid_ask(ex, symbol):
    ob = ex.fetch_order_book(symbol, limit=5)
    bids = ob.get("bids") or []
    asks = ob.get("asks") or []
    if not bids or not asks:
        return None, None
    bid = bids[0][0]
    ask = asks[0][0]
    return bid, ask


def sample_spread_tb(ex, symbols, duration_s=60, interval_s=2.0):
    """
    SPREAD_TB = trung bình spread_pct trong (duration_s) với bước (interval_s).
    """
    start = time.time()
    buckets = {s: [] for s in symbols}

    next_tick = start
    while time.time() - start < duration_s:
        now = time.time()
        if now < next_tick:
            time.sleep(min(0.2, next_tick - now))
            continue

        # Quét tuần tự để tránh quá nhiều request/weight
        for sym in symbols:
            try:
                bid, ask = fetch_order_book_best_bid_ask(ex, sym)
                spread_pct = compute_spread_pct_from_ba(bid, ask)
                if spread_pct is not None:
                    buckets[sym].append(spread_pct)
            except Exception:
                # bỏ qua lỗi cho cặp đó tại thời điểm mẫu
                pass

        next_tick += interval_s

    result = {}
    for sym, arr in buckets.items():
        if arr:
            result[sym] = sum(arr) / len(arr)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--exchanges",
        type=str,
        default="binance,mexc",
        help="Danh sách sàn muốn kiểm tra (mặc định: binance,mexc). Ví dụ: binance hoặc mexc",
    )
    parser.add_argument("--top-volume", type=int, default=30,
                        help="Lấy top cặp theo volume để quét spread (mặc định 30)")
    parser.add_argument("--sample-duration", type=int, default=60,
                        help="Thời gian quét orderbook để tính SPREAD_TB (giây)")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="Khoảng cách 2 lần mẫu spread (giây)")
    parser.add_argument("--quote", type=str, default="USDT,USDC,BUSD,FDUSD",
                        help="Chỉ lấy các cặp có quote thuộc danh sách (cách nhau bởi dấu ,). Ví dụ: USDT,JPY,XAUT/U")
    parser.add_argument("--top-spread", type=int, default=30,
                        help="In ra top N theo SPREAD_TB")
    parser.add_argument("--top-volume-out", type=int, default=30,
                        help="In ra top N theo volume")
    args = parser.parse_args()

    quote_filter = normalize_quote_list(args.quote)

    exchanges = [x.strip().lower() for x in (args.exchanges or "").split(",") if x.strip()]
    if not exchanges:
        print(f"{RED}Không có sàn nào được chọn. Dùng --exchanges binance,mexc{RESET}")
        return

    for ex_name in exchanges:
        ex = create_exchange_by_name(ex_name)
        ex.load_markets()
        print()
        print(f"{BOLD}===== {ex_name.upper()} ====={RESET}")
        print(f"{BOLD}[{datetime.now().strftime('%H:%M:%S')}] Lấy top volume...{RESET}")

        top = fetch_quote_volume_top(ex, quote_filter=quote_filter, top_n=args.top_volume)
        if not top:
            print(f"{RED}Không tìm thấy cặp nào theo filter quote cho {ex_name}.{RESET}")
            continue

        symbols = [s for s, _ in top]
        volume_map = {s: v for s, v in top}

        print(f"{BOLD}[{datetime.now().strftime('%H:%M:%S')}] Quét orderbook để tính SPREAD_TB...{RESET}")
        spread_tb_map = sample_spread_tb(
            ex,
            symbols=symbols,
            duration_s=args.sample_duration,
            interval_s=args.interval,
        )

        if not spread_tb_map:
            print(f"{RED}Không tính được SPREAD_TB cho {ex_name} (có thể do lỗi fetch orderbook).{RESET}")
            continue

        ranked_spread = sorted(spread_tb_map.items(), key=lambda x: x[1], reverse=True)
        ranked_volume = sorted(volume_map.items(), key=lambda x: x[1], reverse=True)

        print()
        print(f"{BOLD}Top {args.top_spread} theo SPREAD_TB (trung bình %){RESET}")
        print(f"{CYAN}{'Pair':<18}{RESET} {'SPREAD_TB%':>12}  {'Volume24h(quote)':>18}")
        for sym, spread_tb in ranked_spread[:args.top_spread]:
            print(f"{sym:<18}  {spread_tb:>12.6f}  {fmt(volume_map.get(sym)) :>18}")

        print()
        print(f"{BOLD}Top {args.top_volume_out} theo volume 24h{RESET}")
        print(f"{CYAN}{'Pair':<18}{RESET} {'Volume24h(quote)':>18}  {'SPREAD_TB%':>12}")
        for sym, v in ranked_volume[:args.top_volume_out]:
            sp = spread_tb_map.get(sym)
            sp_s = f"{sp:.6f}" if sp is not None else "—"
            print(f"{sym:<18}  {fmt(v):>18}  {sp_s:>12}")

        print()
        print(
            f"{YELLOW}Ghi chú:{RESET} SPREAD_TB của {ex_name} được tính từ mẫu bid/ask trong "
            f"{args.sample_duration}s (mỗi {args.interval}s), nên chỉ là ước lượng ngắn hạn."
        )


if __name__ == "__main__":
    main()

