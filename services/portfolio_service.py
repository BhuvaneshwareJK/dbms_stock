"""
services/portfolio_service.py
=============================
TIER 2 - SERVICE LAYER (market data + valuation).

Responsibilities
----------------
* Read users and positions out of the database.
* Fetch live and historical quotes from Yahoo Finance via `yfinance`.
* Mark positions to market and produce the valuation snapshot the dashboard
  metrics are built from.

Deliberate design choices
-------------------------
1. NO STREAMLIT IMPORTS. Caching is done with a plain TTL dictionary rather
   than `st.cache_data`, so this module stays usable from a cron job, a
   notebook, or a FastAPI process without dragging the UI framework along.
2. Network calls NEVER raise upward. yfinance is an unofficial scraper of a
   third-party endpoint; it fails during market maintenance, on rate limits,
   and on delisted tickers. Every quote function degrades gracefully and the
   caller is told which prices are stale (`price_source` column), rather than
   the dashboard crashing.
3. Decimal comes out of MySQL; floats go into Pandas. The conversion happens
   exactly once, here at the boundary. Money arithmetic that must be exact
   (cash, cost basis) stays in Decimal inside transaction_service.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from db.database import fetch_df, fetch_one

logger = logging.getLogger(__name__)

# yfinance is imported defensively: the rest of the app (ledger, holdings,
# cost-basis analytics) must still work on a machine with no internet access.
try:
    import yfinance as yf

    _YF_AVAILABLE = True
except Exception as exc:  # pragma: no cover - environment dependent
    yf = None  # type: ignore[assignment]
    _YF_AVAILABLE = False
    logger.warning("yfinance unavailable (%s); live prices disabled", exc)


PRICE_CACHE_TTL = int(os.getenv("PRICE_CACHE_TTL", "60"))  # seconds


class MarketDataError(RuntimeError):
    """Raised only when a caller explicitly demands a price and none exists."""


# ===========================================================================
#  Quote cache
# ===========================================================================

_cache_lock = threading.Lock()
# ticker -> {"price": float, "prev_close": float|None, "ts": float}
_quote_cache: Dict[str, Dict[str, Any]] = {}


def _cache_get(ticker: str) -> Optional[Dict[str, Any]]:
    """Return a cached quote if it is still inside the TTL window."""
    with _cache_lock:
        entry = _quote_cache.get(ticker.upper())
        if entry and (time.time() - entry["ts"]) < PRICE_CACHE_TTL:
            return entry
    return None


def _cache_put(ticker: str, price: float, prev_close: Optional[float]) -> None:
    with _cache_lock:
        _quote_cache[ticker.upper()] = {
            "price": float(price),
            "prev_close": float(prev_close) if prev_close else None,
            "ts": time.time(),
        }


def clear_price_cache() -> None:
    """Force the next quote request to hit the network (used by the Refresh button)."""
    with _cache_lock:
        _quote_cache.clear()


# ===========================================================================
#  Market data
# ===========================================================================

def _extract_close_series(raw: pd.DataFrame, ticker: str) -> pd.Series:
    """
    Pull the Close column for one ticker out of a yfinance download.

    yfinance returns a flat frame for a single ticker and a MultiIndex frame
    for several, so this normalises both shapes.
    """
    if raw is None or raw.empty:
        return pd.Series(dtype="float64")
    try:
        if isinstance(raw.columns, pd.MultiIndex):
            # Layout is either (ticker, field) or (field, ticker) depending on
            # the group_by argument and yfinance version, so try both.
            if ticker in raw.columns.get_level_values(0):
                return raw[ticker]["Close"].dropna()
            if ticker in raw.columns.get_level_values(-1):
                return raw["Close"][ticker].dropna()
            return pd.Series(dtype="float64")
        return raw["Close"].dropna()
    except (KeyError, IndexError):
        return pd.Series(dtype="float64")


def fetch_quotes(tickers: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """
    Batch-fetch last price + previous close for several tickers.

    Returns {TICKER: {"price": float, "prev_close": float|None}} and simply
    omits tickers that could not be resolved. One batched call is used instead
    of N single calls because Yahoo rate-limits aggressively.
    """
    wanted = sorted({t.strip().upper() for t in tickers if t and str(t).strip()})
    if not wanted:
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    to_fetch: List[str] = []

    # 1. serve whatever is still fresh in the cache
    for ticker in wanted:
        cached = _cache_get(ticker)
        if cached:
            out[ticker] = {"price": cached["price"], "prev_close": cached["prev_close"]}
        else:
            to_fetch.append(ticker)

    if not to_fetch or not _YF_AVAILABLE:
        return out

    # 2. one network round-trip for everything still missing.
    #    5 days of history covers weekends and market holidays, guaranteeing at
    #    least two sessions for the day-change calculation.
    try:
        raw = yf.download(
            tickers=to_fetch,
            period="5d",
            interval="1d",
            auto_adjust=False,
            progress=False,
            group_by="ticker",
            threads=True,
        )
    except Exception as exc:  # noqa: BLE001 - network layer, anything can happen
        logger.error("yfinance batch download failed for %s: %s", to_fetch, exc)
        return out

    for ticker in to_fetch:
        closes = _extract_close_series(raw, ticker)
        if closes.empty:
            logger.warning("No quote returned for ticker %s", ticker)
            continue
        price = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2]) if len(closes) > 1 else None
        _cache_put(ticker, price, prev_close)
        out[ticker] = {"price": price, "prev_close": prev_close}

    return out


def get_live_price(ticker: str, *, required: bool = False) -> Optional[float]:
    """
    Last traded price for a single ticker, or None if it cannot be resolved.

    Set `required=True` to raise MarketDataError instead - transaction_service
    uses that so an order is never booked against a phantom price.
    """
    quotes = fetch_quotes([ticker])
    entry = quotes.get(ticker.strip().upper())
    if entry:
        return entry["price"]
    if required:
        raise MarketDataError(
            f"Could not fetch a live price for '{ticker}'. "
            "Check the symbol (NSE stocks need a .NS suffix, e.g. TCS.NS) "
            "or enter the price manually."
        )
    return None


def fetch_company_profile(ticker: str) -> Dict[str, Any]:
    """
    Best-effort reference data for a newly traded ticker.
    Falls back to the raw symbol as the company name; never raises.
    """
    symbol = ticker.strip().upper()
    profile = {
        "ticker": symbol,
        "company_name": symbol,
        "sector": "Unclassified",
        "industry": None,
        "exchange": None,
        "currency": "INR" if symbol.endswith((".NS", ".BO")) else "USD",
    }
    if not _YF_AVAILABLE:
        return profile
    try:
        info = yf.Ticker(symbol).get_info() or {}
        profile.update(
            {
                "company_name": info.get("longName") or info.get("shortName") or symbol,
                "sector": info.get("sector") or "Unclassified",
                "industry": info.get("industry"),
                "exchange": info.get("exchange"),
                "currency": info.get("currency") or profile["currency"],
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Profile lookup failed for %s: %s", symbol, exc)
    return profile


def fetch_price_history(tickers: Iterable[str], start, end) -> pd.DataFrame:
    """
    Daily close prices for a basket of tickers over [start, end].

    Returns a DataFrame indexed by date with one column per ticker. Used by
    analytics_service to rebuild the historical portfolio value curve.
    """
    wanted = sorted({t.strip().upper() for t in tickers if t})
    if not wanted or not _YF_AVAILABLE:
        return pd.DataFrame()
    try:
        raw = yf.download(
            tickers=wanted,
            start=pd.Timestamp(start).strftime("%Y-%m-%d"),
            end=(pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            interval="1d",
            auto_adjust=False,
            progress=False,
            group_by="ticker",
            threads=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Historical download failed: %s", exc)
        return pd.DataFrame()

    frame = pd.DataFrame(
        {t: _extract_close_series(raw, t) for t in wanted}
    ).dropna(how="all")
    if not frame.empty:
        frame.index = pd.to_datetime(frame.index).tz_localize(None)
    return frame


# ===========================================================================
#  Users
# ===========================================================================

def list_users() -> pd.DataFrame:
    """All accounts, for the sidebar account switcher."""
    return fetch_df(
        """
        SELECT user_id, username, full_name, email, cash_balance,
               total_deposits, base_currency
        FROM Users
        ORDER BY username
        """
    )


def get_user(user_id: int) -> Optional[dict]:
    """Single account row, or None if the id does not exist."""
    return fetch_one(
        """
        SELECT user_id, username, full_name, email, cash_balance,
               total_deposits, base_currency, created_at
        FROM Users
        WHERE user_id = :uid
        """,
        {"uid": user_id},
    )


def get_cash_balance(user_id: int) -> float:
    """Settled buying power as a float (0.0 if the user is unknown)."""
    row = fetch_one(
        "SELECT cash_balance FROM Users WHERE user_id = :uid", {"uid": user_id}
    )
    return float(row["cash_balance"]) if row else 0.0


def get_total_deposits(user_id: int) -> float:
    """Lifetime capital contributed - the denominator for ROI."""
    row = fetch_one(
        "SELECT total_deposits FROM Users WHERE user_id = :uid", {"uid": user_id}
    )
    return float(row["total_deposits"]) if row else 0.0


# ===========================================================================
#  Holdings and valuation
# ===========================================================================

def get_holdings(user_id: int, *, include_closed: bool = False) -> pd.DataFrame:
    """
    Raw (un-priced) positions for a user, joined to instrument reference data.

    Columns: stock_id, ticker, company_name, sector, currency, quantity,
             avg_cost, invested_value, realized_pnl, first_bought_at
    """
    qty_filter = "" if include_closed else "AND h.quantity > 0"
    df = fetch_df(
        f"""
        SELECT s.stock_id, s.ticker, s.company_name,
               COALESCE(s.sector, 'Unclassified') AS sector, s.currency,
               h.quantity, h.avg_cost, h.realized_pnl, h.first_bought_at
        FROM Holdings h
        JOIN Stocks s ON s.stock_id = h.stock_id
        WHERE h.user_id = :uid {qty_filter}
        ORDER BY (h.quantity * h.avg_cost) DESC
        """,
        {"uid": user_id},
    )
    if df.empty:
        return df

    # Decimal -> float exactly once, here at the tier boundary.
    for col in ("quantity", "avg_cost", "realized_pnl"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).astype(float)
    df["invested_value"] = df["quantity"] * df["avg_cost"]
    return df


def get_holdings_with_market_data(user_id: int) -> pd.DataFrame:
    """
    Positions marked to market.

    Adds: current_price, prev_close, market_value, unrealized_pnl,
          unrealized_pnl_pct, day_change, day_change_pct, weight_pct,
          price_source ('live' | 'cost_basis').

    When a quote is unavailable the cost basis is substituted so totals stay
    coherent, and `price_source` records the substitution so the UI can warn.
    """
    df = get_holdings(user_id)
    if df.empty:
        return df

    quotes = fetch_quotes(df["ticker"].tolist())

    df["current_price"] = df["ticker"].map(lambda t: quotes.get(t, {}).get("price"))
    df["prev_close"] = df["ticker"].map(lambda t: quotes.get(t, {}).get("prev_close"))
    df["price_source"] = df["current_price"].apply(
        lambda p: "live" if pd.notna(p) else "cost_basis"
    )
    # Fall back to avg_cost -> unrealised P&L shows as 0 rather than as a loss.
    df["current_price"] = df["current_price"].fillna(df["avg_cost"]).astype(float)
    df["prev_close"] = df["prev_close"].fillna(df["current_price"]).astype(float)

    df["market_value"] = df["quantity"] * df["current_price"]
    df["unrealized_pnl"] = df["market_value"] - df["invested_value"]
    df["unrealized_pnl_pct"] = (
        df["unrealized_pnl"] / df["invested_value"].replace(0, pd.NA) * 100
    ).astype(float).fillna(0.0)

    df["day_change"] = (df["current_price"] - df["prev_close"]) * df["quantity"]
    df["day_change_pct"] = (
        (df["current_price"] - df["prev_close"]) / df["prev_close"].replace(0, pd.NA) * 100
    ).astype(float).fillna(0.0)

    total_mv = df["market_value"].sum()
    df["weight_pct"] = (df["market_value"] / total_mv * 100) if total_mv else 0.0

    return df.sort_values("market_value", ascending=False).reset_index(drop=True)


def get_portfolio_snapshot(user_id: int) -> Dict[str, Any]:
    """
    The single call the dashboard header makes. Everything the st.metric row
    needs, computed once so the page cannot show internally inconsistent numbers.

    Keys
    ----
    holdings            DataFrame from get_holdings_with_market_data
    cash                settled cash
    market_value        sum of position values
    total_value         cash + market_value  (net worth inside the platform)
    invested_value      cost basis of open positions
    unrealized_pnl      market_value - invested_value
    realized_pnl        booked P&L from closed/partially closed positions
    total_deposits      lifetime capital in
    net_pnl             total_value - total_deposits
    roi_pct             net_pnl / total_deposits * 100
    day_change          today's move in currency terms
    day_change_pct      today's move relative to yesterday's close
    stale_prices        list of tickers whose quote could not be fetched
    """
    holdings = get_holdings_with_market_data(user_id)
    cash = get_cash_balance(user_id)
    deposits = get_total_deposits(user_id)

    if holdings.empty:
        market_value = invested = unrealized = day_change = 0.0
        realized = _sum_realized_pnl(user_id)
        stale: List[str] = []
    else:
        market_value = float(holdings["market_value"].sum())
        invested = float(holdings["invested_value"].sum())
        unrealized = float(holdings["unrealized_pnl"].sum())
        day_change = float(holdings["day_change"].sum())
        realized = _sum_realized_pnl(user_id)
        stale = holdings.loc[holdings["price_source"] != "live", "ticker"].tolist()

    total_value = cash + market_value
    net_pnl = total_value - deposits
    prev_total = total_value - day_change

    return {
        "holdings": holdings,
        "cash": cash,
        "market_value": market_value,
        "total_value": total_value,
        "invested_value": invested,
        "unrealized_pnl": unrealized,
        "unrealized_pnl_pct": (unrealized / invested * 100) if invested else 0.0,
        "realized_pnl": realized,
        "total_deposits": deposits,
        "net_pnl": net_pnl,
        "roi_pct": (net_pnl / deposits * 100) if deposits else 0.0,
        "day_change": day_change,
        "day_change_pct": (day_change / prev_total * 100) if prev_total else 0.0,
        "position_count": int(len(holdings)),
        "stale_prices": stale,
    }


def _sum_realized_pnl(user_id: int) -> float:
    """Total booked P&L across every SELL the user has ever made."""
    row = fetch_one(
        """
        SELECT COALESCE(SUM(realized_pnl), 0) AS total
        FROM Transactions
        WHERE user_id = :uid AND txn_type = 'SELL'
        """,
        {"uid": user_id},
    )
    return float(row["total"]) if row else 0.0


def get_position(user_id: int, ticker: str) -> Optional[dict]:
    """Current quantity + cost basis for one ticker; None if not held."""
    return fetch_one(
        """
        SELECT h.quantity, h.avg_cost, s.ticker, s.company_name
        FROM Holdings h
        JOIN Stocks s ON s.stock_id = h.stock_id
        WHERE h.user_id = :uid AND s.ticker = :ticker
        """,
        {"uid": user_id, "ticker": ticker.strip().upper()},
    )


def search_known_tickers(prefix: str = "", limit: int = 50) -> List[str]:
    """Ticker autocomplete source, backed by the Stocks reference table."""
    df = fetch_df(
        """
        SELECT ticker FROM Stocks
        WHERE is_active = 1 AND ticker LIKE :pattern
        ORDER BY ticker
        LIMIT :lim
        """,
        {"pattern": f"{prefix.upper()}%", "lim": limit},
    )
    return df["ticker"].tolist() if not df.empty else []
