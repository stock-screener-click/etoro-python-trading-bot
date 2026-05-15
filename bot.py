"""Daily screener-driven rebalancing bot for eToro demo trading.

Reads today's picks from the Stock Screener API, resolves each ticker to
eToro's numeric instrumentID, and rebalances an eToro demo account to
hold an equal-weight basket of those picks.

Set ETORO_MODE=real once you've tested and want to trade live.

See README.md for setup, scheduling, and safeguards.
"""

import json
import os
import uuid
from pathlib import Path

import requests

# --- Config ---
ETORO_BASE = "https://public-api.etoro.com/api/v1"
ETORO_API_KEY = os.environ["ETORO_API_KEY"]
ETORO_USER_KEY = os.environ["ETORO_USER_KEY"]
RAPIDAPI_KEY = os.environ["RAPIDAPI_KEY"]
RAPIDAPI_HOST = os.environ["RAPIDAPI_HOST"]
MODE = os.environ.get("ETORO_MODE", "demo")  # "demo" or "real"
CACHE_FILE = Path("instrument_cache.json")

SCREENER_HEADERS = {
    "X-RapidAPI-Key": RAPIDAPI_KEY,
    "X-RapidAPI-Host": RAPIDAPI_HOST,
}


# --- eToro HTTP helpers ---
def etoro_headers() -> dict:
    return {
        "x-api-key": ETORO_API_KEY,
        "x-user-key": ETORO_USER_KEY,
        "x-request-id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


def etoro_get(path: str, **params) -> dict:
    r = requests.get(
        f"{ETORO_BASE}{path}",
        headers=etoro_headers(),
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def etoro_post(path: str, body: dict) -> dict:
    r = requests.post(
        f"{ETORO_BASE}{path}",
        headers=etoro_headers(),
        json=body,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


# --- Instrument cache ---
def load_cache() -> dict[str, int]:
    return json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}


def save_cache(cache: dict[str, int]) -> None:
    CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True))


def resolve_instrument(ticker: str, cache: dict) -> int | None:
    """Return eToro's numeric instrumentID for a ticker like 'AAPL'."""
    if ticker in cache:
        return cache[ticker]
    results = etoro_get("/market-data/search", internalSymbolFull=ticker)
    for item in results.get("instruments", []):
        if item.get("internalSymbolFull", "").upper() == ticker.upper():
            cache[ticker] = item["instrumentId"]
            return item["instrumentId"]
    print(f"WARN: no eToro instrument for {ticker}")
    return None


# --- Trading actions ---
def get_portfolio() -> dict:
    return etoro_get(f"/trading/info/{MODE}/portfolio")


def open_position(instrument_id: int, amount_usd: float, leverage: int = 1) -> dict:
    return etoro_post(
        f"/trading/execution/{MODE}/market-open-orders/by-amount",
        {
            "InstrumentID": instrument_id,
            "Amount": amount_usd,
            "Leverage": leverage,
            "IsBuy": True,
        },
    )


def close_position(position_id: int, instrument_id: int) -> dict:
    return etoro_post(
        f"/trading/execution/{MODE}/market-close-orders/positions/{position_id}",
        {"InstrumentID": instrument_id},
    )


# --- Screener API ---
def fetch_picks(screener_id: str) -> list[str]:
    r = requests.get(
        f"https://{RAPIDAPI_HOST}/tickers/latest",
        headers=SCREENER_HEADERS,
        params={"screener_id": screener_id},
        timeout=15,
    )
    r.raise_for_status()
    return [row["ticker"] for row in r.json()]


def best_screener(window: str = "1m") -> str:
    r = requests.get(
        f"https://{RAPIDAPI_HOST}/stock-screeners/performance",
        headers=SCREENER_HEADERS,
        params={"window": window},
        timeout=15,
    )
    r.raise_for_status()
    top = next(
        s for s in r.json()["screeners"] if s.get("avg_return_pct") is not None
    )
    print(
        f"Best {window} screener: {top['short_name']} "
        f"({top['avg_return_pct']:.2f}%)"
    )
    return top["screener_id"]


# --- Main rebalance ---
def rebalance(screener_id: str) -> None:
    cache = load_cache()
    tickers = fetch_picks(screener_id)
    if not tickers:
        print("No picks today, skipping.")
        return

    pick_ids: dict[str, int] = {}
    for t in tickers:
        iid = resolve_instrument(t, cache)
        if iid is not None:
            pick_ids[t] = iid
    save_cache(cache)

    if not pick_ids:
        print("None of today's picks are tradeable on eToro.")
        return

    portfolio = get_portfolio()
    open_positions = portfolio["clientPortfolio"]["positions"]
    equity = portfolio["clientPortfolio"]["accountBalance"]["totalEquity"]
    target = equity / len(pick_ids)

    held_iids = {p["instrumentID"] for p in open_positions}
    target_iids = set(pick_ids.values())

    for p in open_positions:
        if p["instrumentID"] not in target_iids:
            print(f"CLOSE positionID={p['positionID']} (no longer in screener)")
            close_position(p["positionID"], p["instrumentID"])

    for ticker, iid in pick_ids.items():
        if iid in held_iids:
            continue
        print(f"OPEN ${target:,.0f} of {ticker} (id={iid})")
        open_position(iid, amount_usd=round(target, 2))

    print(f"Rebalance complete. Target: {len(pick_ids)} positions ({MODE}).")


if __name__ == "__main__":
    if os.environ.get("BOT_DISABLED"):
        print("BOT_DISABLED is set, exiting.")
        raise SystemExit(0)
    rebalance(best_screener("1m"))
