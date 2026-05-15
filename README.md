# Build a Daily Trading Bot with the Stock Screener API and eToro

This tutorial walks through building a daily-rebalancing trading bot that:

1. Pulls fresh stock picks from the [Stock Screener API](https://rapidapi.com/stock-screener-stock-screener-default/api/stock-screener6) each morning.
2. Routes those picks into [eToro's Public API](https://api-portal.etoro.com/) on demo (paper) mode.
3. Sells positions that are no longer in the screener and opens equal-weighted positions in new picks.

eToro's API is global (US, UK, EU, Australia and more), so this tutorial works in regions where US-only brokerages don't. We use **demo trading** throughout so no real money is at risk.

---

## What You'll Build

```
   Stock Screener API           eToro Public API (demo)
   ┌──────────────────┐          ┌────────────────────────┐
   │ /tickers/latest  │  picks   │ /market-data/search    │
   │ /stock-screeners │─────────▶│ /trading/info/demo/    │
   │   /performance   │          │     portfolio          │
   └──────────────────┘          │ /trading/execution/    │
              ▲                  │     demo/...           │
              │                  └────────────────────────┘
              └────────── your daily cron ────────────────┘
```

The bot's daily logic:

1. Fetch the screener's latest US-listed picks (e.g. "Quality / Compounder").
2. Resolve each ticker to eToro's numeric `instrumentID` (and cache it).
3. Fetch your current demo portfolio.
4. Close any open position whose ticker is no longer in today's picks.
5. Open new equal-weighted positions for tickers you don't already hold.

---

## Prerequisites

| Tool | Purpose | Cost |
|---|---|---|
| [RapidAPI account](https://rapidapi.com/) with Stock Screener subscription | Source of daily picks | Free tier available |
| [eToro account](https://www.etoro.com/), verified | Brokerage execution | Free |
| Python 3.10 or newer | Runtime | Free |

You'll need four credentials:

- `RAPIDAPI_KEY` and `RAPIDAPI_HOST` from your RapidAPI subscription page.
- `ETORO_API_KEY` (the Public API key) and `ETORO_USER_KEY` (your generated User Key).

### How to generate eToro API credentials

1. Log into eToro on the web.
2. Navigate to **Settings -> Trading**.
3. Find **API Key Management** and click **Create New Key**.
4. Name the key, choose **Demo** (we'll use this for testing), and select **Read** + **Write** permissions.
5. Complete SMS mobile verification.
6. Copy the generated `ETORO_USER_KEY`. The `ETORO_API_KEY` (Public API Key) is shown alongside.

Set them as environment variables:

```bash
export RAPIDAPI_KEY="..."
export RAPIDAPI_HOST="..."
export ETORO_API_KEY="..."
export ETORO_USER_KEY="..."
```

---

## Step 1: Install Dependencies

```bash
pip install requests
```

eToro doesn't ship an official Python SDK, so we'll call the REST API directly with `requests`. That's actually fine: there are only a handful of endpoints we need.

---

## Step 2: Build an eToro HTTP Client

Every eToro request needs three headers: `x-api-key`, `x-user-key`, and a fresh `x-request-id` UUID. Wrap that boilerplate once.

```python
import os
import uuid
import requests

ETORO_BASE = "https://public-api.etoro.com/api/v1"
ETORO_API_KEY = os.environ["ETORO_API_KEY"]
ETORO_USER_KEY = os.environ["ETORO_USER_KEY"]


def etoro_headers() -> dict:
    return {
        "x-api-key": ETORO_API_KEY,
        "x-user-key": ETORO_USER_KEY,
        "x-request-id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


def etoro_get(path: str, **params) -> dict:
    resp = requests.get(f"{ETORO_BASE}{path}", headers=etoro_headers(),
                        params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def etoro_post(path: str, body: dict) -> dict:
    resp = requests.post(f"{ETORO_BASE}{path}", headers=etoro_headers(),
                         json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()
```

A fresh `x-request-id` per call helps eToro trace and dedupe requests on their side.

---

## Step 3: Resolve Tickers to eToro Instrument IDs

Unlike Alpaca and most US brokers, eToro uses immutable **numeric instrument IDs**, not ticker symbols, for every order. You have to look them up once and cache them.

```python
import json
from pathlib import Path

CACHE_FILE = Path("instrument_cache.json")


def load_cache() -> dict[str, int]:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}


def save_cache(cache: dict[str, int]):
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def resolve_instrument(ticker: str, cache: dict) -> int | None:
    """Return eToro's numeric instrument ID for a ticker like 'AAPL'."""
    if ticker in cache:
        return cache[ticker]

    results = etoro_get("/market-data/search", internalSymbolFull=ticker)
    for item in results.get("instruments", []):
        if item.get("internalSymbolFull", "").upper() == ticker.upper():
            cache[ticker] = item["instrumentId"]
            return item["instrumentId"]

    print(f"WARN: no eToro instrument found for {ticker}")
    return None
```

Instrument IDs never change, so you only pay the lookup cost once per ticker per machine.

---

## Step 4: Fetch the Demo Portfolio

```python
def get_portfolio() -> dict:
    """Returns the demo portfolio: positions, orders, account info."""
    return etoro_get("/trading/info/demo/portfolio")


if __name__ == "__main__":
    portfolio = get_portfolio()
    positions = portfolio["clientPortfolio"]["positions"]
    print(f"Holding {len(positions)} positions on demo")
    for p in positions:
        print(f"  positionID={p['positionID']} "
              f"instrumentID={p['instrumentID']} "
              f"amount=${p['amount']:.2f}")
```

A fresh demo account starts with $100,000 of virtual cash.

---

## Step 5: Place Your First Demo Trade

Open a $1,000 long position in AAPL.

```python
def open_position(instrument_id: int, amount_usd: float, leverage: int = 1) -> dict:
    """Open a long market position by cash amount on the demo account."""
    return etoro_post(
        "/trading/execution/demo/market-open-orders/by-amount",
        {
            "InstrumentID": instrument_id,
            "Amount": amount_usd,
            "Leverage": leverage,
            "IsBuy": True,
        },
    )


if __name__ == "__main__":
    cache = load_cache()
    aapl_id = resolve_instrument("AAPL", cache)
    save_cache(cache)

    result = open_position(aapl_id, amount_usd=1000)
    print(f"Order placed: {result}")
```

Note: `Leverage=1` means no leverage (a straight stock buy). Higher values would create CFDs.

To close a position later, you need its `positionID` (from the portfolio call) and its `instrumentID`:

```python
def close_position(position_id: int, instrument_id: int) -> dict:
    """Close a position fully on the demo account."""
    return etoro_post(
        f"/trading/execution/demo/market-close-orders/positions/{position_id}",
        {"InstrumentID": instrument_id},  # omit UnitsToDeduct = full close
    )
```

---

## Step 6: Daily Rebalance from a Screener

Now wire it together. Each morning the bot will:

1. Fetch picks for a chosen screener.
2. Resolve each ticker to an instrument ID (skipping any not on eToro).
3. Compute equal-weight target allocation.
4. Close positions that aren't in today's picks.
5. Open new positions for any new picks.

```python
RAPIDAPI_KEY = os.environ["RAPIDAPI_KEY"]
RAPIDAPI_HOST = os.environ["RAPIDAPI_HOST"]
SCREENER_HEADERS = {
    "X-RapidAPI-Key": RAPIDAPI_KEY,
    "X-RapidAPI-Host": RAPIDAPI_HOST,
}


def fetch_picks(screener_id: str) -> list[str]:
    resp = requests.get(
        f"https://{RAPIDAPI_HOST}/tickers/latest",
        headers=SCREENER_HEADERS,
        params={"screener_id": screener_id},
        timeout=15,
    )
    resp.raise_for_status()
    return [row["ticker"] for row in resp.json()]


def rebalance(screener_id: str):
    cache = load_cache()
    tickers = fetch_picks(screener_id)
    if not tickers:
        print("No picks today, skipping.")
        return

    # Resolve tickers, dropping any not available on eToro
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
    portfolio_value = portfolio["clientPortfolio"]["accountBalance"]["totalEquity"]
    target_per_position = portfolio_value / len(pick_ids)

    held_iids = {p["instrumentID"] for p in open_positions}
    target_iids = set(pick_ids.values())

    # 1. Close positions that fell out of the screener
    for p in open_positions:
        if p["instrumentID"] not in target_iids:
            print(f"CLOSE positionID={p['positionID']} (no longer in screener)")
            close_position(p["positionID"], p["instrumentID"])

    # 2. Open new positions for picks we don't already hold
    for ticker, iid in pick_ids.items():
        if iid in held_iids:
            continue
        print(f"OPEN ${target_per_position:,.0f} of {ticker} (id={iid})")
        open_position(iid, amount_usd=round(target_per_position, 2))

    print(f"Rebalance complete. Target: {len(pick_ids)} positions.")


if __name__ == "__main__":
    rebalance("quality-compounder")
```

Notes:

- `accountBalance.totalEquity` includes cash plus market value of open positions, so target sizes scale as the account grows (or shrinks).
- We compare on `instrumentID`, not ticker, because the portfolio response uses IDs.
- eToro orders by **cash amount**, which maps perfectly to equal-weight rebalancing without computing share counts.

---

## Step 7: Pick the Best Performing Screener Automatically

Use the screener API's performance endpoint to rotate into whatever's been working.

```python
def best_screener(window: str = "1m") -> str:
    resp = requests.get(
        f"https://{RAPIDAPI_HOST}/stock-screeners/performance",
        headers=SCREENER_HEADERS,
        params={"window": window},
        timeout=15,
    )
    resp.raise_for_status()
    screeners = resp.json()["screeners"]  # already sorted desc
    top = next(s for s in screeners if s.get("avg_return_pct") is not None)
    print(f"Best {window} screener: {top['short_name']} "
          f"({top['avg_return_pct']:.2f}%)")
    return top["screener_id"]


if __name__ == "__main__":
    rebalance(best_screener("1m"))
```

Be careful: chasing recent performance is its own well-known failure mode. Consider longer windows (`3m`, `6m`) or blending several screeners.

---

## Step 8: Schedule the Bot

Run the rebalance once per trading day, after market open.

### Option A: Local cron (Linux / macOS)

```bash
crontab -e
# 35 9 * * 1-5 cd /path/to/bot && /usr/bin/python3 bot.py >> bot.log 2>&1
```

### Option B: GitHub Actions

Create `.github/workflows/trade.yml`:

```yaml
name: Daily eToro rebalance
on:
  schedule:
    - cron: "35 13 * * 1-5"   # 9:35 AM ET
  workflow_dispatch:

jobs:
  trade:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install requests
      - run: python bot.py
        env:
          RAPIDAPI_KEY: ${{ secrets.RAPIDAPI_KEY }}
          RAPIDAPI_HOST: ${{ secrets.RAPIDAPI_HOST }}
          ETORO_API_KEY: ${{ secrets.ETORO_API_KEY }}
          ETORO_USER_KEY: ${{ secrets.ETORO_USER_KEY }}
```

Add the four secrets in your repo's Settings -> Secrets and Variables -> Actions.

For the instrument cache to persist across GitHub Actions runs, commit `instrument_cache.json` to the repo or use the `actions/cache` action keyed on the file's contents.

### Option C: AWS Lambda + EventBridge

Same idea as the screener API itself in this repo. Package the script as a Lambda, schedule via EventBridge with `cron(35 13 ? * MON-FRI *)`. Use S3 or DynamoDB for the instrument cache instead of a local file.

---

## Switching from Demo to Real

When you're ready to move from `Demo` to `Real` capital:

1. Generate a new API key in eToro Settings, this time choosing **Real** environment.
2. Update `ETORO_USER_KEY` to the new value.
3. Replace `/demo/` with `/real/` in every endpoint path:

```python
# Demo
"/trading/execution/demo/market-open-orders/by-amount"
"/trading/info/demo/portfolio"
"/trading/execution/demo/market-close-orders/positions/{positionId}"

# Real
"/trading/execution/real/market-open-orders/by-amount"
"/trading/info/real/portfolio"
"/trading/execution/real/market-close-orders/positions/{positionId}"
```

Easiest pattern: parameterize the mode.

```python
MODE = os.environ.get("ETORO_MODE", "demo")  # "demo" or "real"

def trading_path(suffix: str) -> str:
    return f"/trading/execution/{MODE}/{suffix}"
```

---

## Safeguards Before Going Live

Demo trading is forgiving. Real money is not. Before flipping to real:

- **Position cap**: refuse to trade if `len(picks) > MAX_POSITIONS` to avoid over-diversifying into illiquid names.
- **Min cash buffer**: bail if `accountBalance.availableCash < portfolio_value * 0.05` (account in margin trouble).
- **Leverage lock**: hard-code `Leverage=1` so a config typo can't accidentally open a 5x or 10x position.
- **No-short safety**: hard-code `IsBuy=True` unless you specifically want short exposure (the API supports both).
- **Kill switch**: keep an `if os.environ.get("BOT_DISABLED"): return` early-exit so you can pause via env var.
- **Order log**: write every order request and response to a file or database so you can reconcile.
- **Rate limiting**: insert `time.sleep(0.5)` between API calls if rebalancing a large universe to be polite to eToro's infra.
- **Start small**: when going real, fund the account with a small amount and watch for a full week before scaling.

---

## Full Script

```python
"""Daily screener-driven rebalancing bot for eToro demo trading."""

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
MODE = os.environ.get("ETORO_MODE", "demo")
CACHE_FILE = Path("instrument_cache.json")

SCREENER_HEADERS = {
    "X-RapidAPI-Key": RAPIDAPI_KEY,
    "X-RapidAPI-Host": RAPIDAPI_HOST,
}


# --- eToro HTTP helpers ---
def etoro_headers():
    return {
        "x-api-key": ETORO_API_KEY,
        "x-user-key": ETORO_USER_KEY,
        "x-request-id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


def etoro_get(path, **params):
    r = requests.get(f"{ETORO_BASE}{path}", headers=etoro_headers(),
                     params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def etoro_post(path, body):
    r = requests.post(f"{ETORO_BASE}{path}", headers=etoro_headers(),
                      json=body, timeout=15)
    r.raise_for_status()
    return r.json()


# --- Instrument cache ---
def load_cache():
    return json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}


def save_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def resolve_instrument(ticker, cache):
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
def get_portfolio():
    return etoro_get(f"/trading/info/{MODE}/portfolio")


def open_position(instrument_id, amount_usd, leverage=1):
    return etoro_post(
        f"/trading/execution/{MODE}/market-open-orders/by-amount",
        {
            "InstrumentID": instrument_id,
            "Amount": amount_usd,
            "Leverage": leverage,
            "IsBuy": True,
        },
    )


def close_position(position_id, instrument_id):
    return etoro_post(
        f"/trading/execution/{MODE}/market-close-orders/positions/{position_id}",
        {"InstrumentID": instrument_id},
    )


# --- Screener API ---
def fetch_picks(screener_id):
    r = requests.get(f"https://{RAPIDAPI_HOST}/tickers/latest",
                     headers=SCREENER_HEADERS,
                     params={"screener_id": screener_id}, timeout=15)
    r.raise_for_status()
    return [row["ticker"] for row in r.json()]


def best_screener(window="1m"):
    r = requests.get(f"https://{RAPIDAPI_HOST}/stock-screeners/performance",
                     headers=SCREENER_HEADERS,
                     params={"window": window}, timeout=15)
    r.raise_for_status()
    top = next(s for s in r.json()["screeners"]
               if s.get("avg_return_pct") is not None)
    print(f"Best {window} screener: {top['short_name']} "
          f"({top['avg_return_pct']:.2f}%)")
    return top["screener_id"]


# --- Main rebalance ---
def rebalance(screener_id):
    cache = load_cache()
    tickers = fetch_picks(screener_id)
    if not tickers:
        print("No picks today.")
        return

    pick_ids = {t: i for t in tickers
                if (i := resolve_instrument(t, cache)) is not None}
    save_cache(cache)
    if not pick_ids:
        print("No tradeable picks on eToro.")
        return

    portfolio = get_portfolio()
    open_positions = portfolio["clientPortfolio"]["positions"]
    equity = portfolio["clientPortfolio"]["accountBalance"]["totalEquity"]
    target = equity / len(pick_ids)

    held = {p["instrumentID"] for p in open_positions}
    targets = set(pick_ids.values())

    for p in open_positions:
        if p["instrumentID"] not in targets:
            print(f"CLOSE positionID={p['positionID']}")
            close_position(p["positionID"], p["instrumentID"])

    for ticker, iid in pick_ids.items():
        if iid not in held:
            print(f"OPEN ${target:,.0f} of {ticker} (id={iid})")
            open_position(iid, amount_usd=round(target, 2))

    print(f"Rebalance complete. Target: {len(pick_ids)} positions.")


if __name__ == "__main__":
    rebalance(best_screener("1m"))
```

---

## Where to Go Next

- **Stop loss / take profit**: pass `StopLossRate` and `TakeProfitRate` in the open-position body so each position has a built-in exit.
- **Partial closes**: pass `UnitsToDeduct` to the close endpoint to scale out of winners gradually instead of all-or-nothing.
- **Drift management**: only rebalance when allocations drift more than 5% from target instead of every day, to reduce churn and fees.
- **Multi-asset**: the screener returns US stocks, but eToro also covers crypto, ETFs, commodities, and global equities. You could blend the screener picks with crypto allocations.
- **Copy trading hybrid**: eToro's social API exposes top investor performance. You could allocate a slice of the portfolio to copying a top performer and the rest to screener picks.
- **Position sizing by conviction**: weight positions by the screener's pick rank rather than equal-weighting all of them.

---

## Resources

- Stock Screener API reference: [RAPIDAPI_README.md](../../RAPIDAPI_README.md)
- eToro Public API portal: https://api-portal.etoro.com/
- eToro authentication guide: https://api-portal.etoro.com/getting-started/authentication
- eToro market orders guide: https://api-portal.etoro.com/guides/market-orders
- eToro instrument lookup guide: https://api-portal.etoro.com/guides/get-instrument-id

---

## Disclaimer

This tutorial is for educational purposes only. It is not investment advice. eToro offers leveraged products (CFDs) which carry a high risk of losing money rapidly; this tutorial uses `Leverage=1` (no leverage) but the same API can place leveraged trades, so test thoroughly. Past performance of any screener does not guarantee future results. Always test in demo mode and understand the strategy before risking real capital.
