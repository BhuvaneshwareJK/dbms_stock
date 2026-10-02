"""
services/analytics_service.py
=============================
TIER 2 - SERVICE LAYER (business intelligence / read-only analytics).

Turns the raw Transactions ledger into the DataFrames the dashboard charts.
Everything here is READ-ONLY - this module never writes to the database.

The central idea: the ledger is the source of truth, and every derived figure
(position size on any past date, portfolio value curve, allocation, ROI) is
recomputed from it with Pandas rather than stored. That means historical
numbers can never drift out of sync with the trades that produced them.

Public functions
----------------
get_transaction_ledger(user_id)          full trade history, typed & enriched
get_portfolio_timeseries(user_id, days)  daily market value / cash / total value
get_asset_allocation(user_id)            per-stock weights
get_sector_allocation(user_id)           per-sector weights
get_roi_summary(user_id)                 headline return figures
get_performance_metrics(user_id, days)   drawdown, volatility, best/worst day
get_realized_pnl_by_stock(user_id)       booked P&L league table
get_monthly_activity(user_id)            trades and turnover per month
get_trade_statistics(user_id)            win rate, average ticket size, etc.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import numpy as np
import pandas as pd

from db.database import fetch_df
from services import portfolio_service

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252


# ===========================================================================
#  1. The ledger  (every other function starts from this)
# ===========================================================================

def get_transaction_ledger(user_id: int, limit: int | None = None) -> pd.DataFrame:
    """
    Full trade history for a user as a typed DataFrame.

    Columns: txn_id, executed_at, date, ticker, company_name, sector, txn_type,
             quantity, price_per_share, fees, gross_amount, net_amount,
             realized_pnl, signed_quantity, cash_flow, notes

    `signed_quantity` (+buy / -sell) and `cash_flow` (-buy / +sell) are added
    here so downstream functions can just `.cumsum()` instead of repeating the
    sign logic five times.
    """
    sql = """
        SELECT t.txn_id, t.executed_at, s.ticker, s.company_name,
               COALESCE(s.sector, 'Unclassified') AS sector,
               t.txn_type, t.quantity, t.price_per_share, t.fees,
               t.gross_amount, t.net_amount, t.realized_pnl, t.notes
        FROM Transactions t
        JOIN Stocks s ON s.stock_id = t.stock_id
        WHERE t.user_id = :uid
        ORDER BY t.executed_at DESC, t.txn_id DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    df = fetch_df(sql, {"uid": user_id})
    if df.empty:
        return df

    # --- type coercion: MySQL hands back Decimal objects, which Pandas keeps
    # as dtype=object. Left alone they break arithmetic and plotting.
    numeric_cols = [
        "quantity", "price_per_share", "fees",
        "gross_amount", "net_amount", "realized_pnl",
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["realized_pnl"] = df["realized_pnl"].fillna(0.0)

    df["executed_at"] = pd.to_datetime(df["executed_at"])
    df["date"] = df["executed_at"].dt.normalize()

    is_buy = df["txn_type"].eq("BUY")
    df["signed_quantity"] = np.where(is_buy, df["quantity"], -df["quantity"])
    df["cash_flow"] = np.where(is_buy, -df["net_amount"], df["net_amount"])

    return df


# ===========================================================================
#  2. Portfolio value time series  (the flagship BI pipeline)
# ===========================================================================

def get_portfolio_timeseries(user_id: int, lookback_days: int = 180) -> pd.DataFrame:
    """
    Reconstruct daily portfolio value over the lookback window.

    Method
    ------
    1. Pivot the ledger into a (date x ticker) matrix of signed quantities.
    2. Cumulative-sum down the date axis -> shares held on every calendar day.
       The cumsum runs over the FULL history first, then the window is sliced,
       so positions opened before the window are still counted.
    3. Download daily closes for those tickers and forward-fill across
       weekends and holidays (a position's value on Sunday is Friday's close).
    4. market_value(t) = sum over tickers of qty(t) * price(t)
    5. Rebuild the cash curve by walking today's known balance backwards
       through the ledger's cash flows.
    6. total_value(t) = market_value(t) + cash(t)

    Returns a DataFrame indexed by date with columns:
        market_value, cash, total_value, invested_capital, roi_pct, daily_return
    Returns an empty DataFrame when the user has never traded.

    Known limitation (stated rather than hidden): step 5 assumes deposits and
    withdrawals all happened before the window starts, because the Users table
    stores only a running `total_deposits` total and not a dated cash-movement
    ledger. Add a CashFlows table if you need deposit-accurate history.
    """
    ledger = get_transaction_ledger(user_id)
    if ledger.empty:
        return pd.DataFrame()

    today = pd.Timestamp.today().normalize()
    first_trade = ledger["date"].min()
    window_start = max(first_trade, today - pd.Timedelta(days=lookback_days))

    # --- step 1 & 2: share count on every calendar day ---------------------
    full_calendar = pd.date_range(first_trade, today, freq="D")
    qty_matrix = (
        ledger.pivot_table(
            index="date", columns="ticker", values="signed_quantity", aggfunc="sum"
        )
        .reindex(full_calendar)
        .fillna(0.0)
        .cumsum()
        .clip(lower=0)  # guards against float dust on fully-closed positions
    )

    # --- step 3: historical closes ----------------------------------------
    tickers = [t for t in qty_matrix.columns if qty_matrix[t].abs().sum() > 0]
    prices = portfolio_service.fetch_price_history(
        tickers, window_start - pd.Timedelta(days=10), today
    )

    if prices.empty:
        # Offline / API failure fallback: value positions at their cost basis
        # so the curve still shows capital deployed over time. Flagged via the
        # `price_basis` attribute so the UI can warn the user.
        logger.warning("No price history available; falling back to cost basis")
        cost_basis = (
            ledger.loc[ledger["txn_type"] == "BUY"]
            .groupby("ticker")["price_per_share"]
            .mean()
        )
        prices = pd.DataFrame(
            {t: cost_basis.get(t, 0.0) for t in qty_matrix.columns},
            index=full_calendar,
        )
        price_basis = "cost_basis"
    else:
        price_basis = "market"

    prices = prices.reindex(full_calendar).ffill().bfill()
    # Align both matrices on the same columns before multiplying.
    prices = prices.reindex(columns=qty_matrix.columns).fillna(0.0)

    # --- step 4: mark to market -------------------------------------------
    market_value = (qty_matrix * prices).sum(axis=1)

    # --- step 5: cash curve ------------------------------------------------
    cash_now = portfolio_service.get_cash_balance(user_id)
    flows = (
        ledger.groupby("date")["cash_flow"].sum().reindex(full_calendar).fillna(0.0)
    )
    cum_flow = flows.cumsum()
    # cash(t) = cash_today - (all flows after t) = cash_today - (total - cum(t))
    cash_series = cash_now - (cum_flow.iloc[-1] - cum_flow)

    # --- step 6: assemble --------------------------------------------------
    ts = pd.DataFrame(
        {
            "market_value": market_value,
            "cash": cash_series,
        }
    )
    ts["total_value"] = ts["market_value"] + ts["cash"]
    ts["invested_capital"] = portfolio_service.get_total_deposits(user_id)
    ts["roi_pct"] = np.where(
        ts["invested_capital"] > 0,
        (ts["total_value"] / ts["invested_capital"] - 1) * 100,
        0.0,
    )
    ts["daily_return"] = ts["total_value"].pct_change().fillna(0.0) * 100

    ts = ts.loc[ts.index >= window_start].round(2)
    ts.index.name = "date"
    ts.attrs["price_basis"] = price_basis  # metadata, survives most Pandas ops
    return ts


# ===========================================================================
#  3. Allocation
# ===========================================================================

def get_asset_allocation(user_id: int) -> pd.DataFrame:
    """
    Per-stock allocation of the *invested* portfolio (cash excluded).

    Columns: ticker, company_name, sector, market_value, weight_pct,
             unrealized_pnl, unrealized_pnl_pct
    """
    holdings = portfolio_service.get_holdings_with_market_data(user_id)
    if holdings.empty:
        return pd.DataFrame(
            columns=["ticker", "company_name", "sector", "market_value", "weight_pct"]
        )

    alloc = holdings[
        [
            "ticker", "company_name", "sector", "quantity", "avg_cost",
            "current_price", "market_value", "invested_value",
            "unrealized_pnl", "unrealized_pnl_pct",
        ]
    ].copy()
    total = alloc["market_value"].sum()
    alloc["weight_pct"] = (alloc["market_value"] / total * 100) if total else 0.0
    return alloc.sort_values("market_value", ascending=False).reset_index(drop=True)


def get_allocation_with_cash(user_id: int) -> pd.DataFrame:
    """
    Allocation including the cash sleeve - the honest view of asset mix,
    since idle cash is an allocation decision too.
    """
    alloc = get_asset_allocation(user_id)
    cash = portfolio_service.get_cash_balance(user_id)
    rows = [] if alloc.empty else alloc[["ticker", "market_value"]].to_dict("records")
    rows.append({"ticker": "CASH", "market_value": cash})

    df = pd.DataFrame(rows)
    total = df["market_value"].sum()
    df["weight_pct"] = (df["market_value"] / total * 100) if total else 0.0
    return df.sort_values("market_value", ascending=False).reset_index(drop=True)


def get_sector_allocation(user_id: int) -> pd.DataFrame:
    """
    Concentration by sector - a groupby/agg on the marked-to-market holdings.
    Columns: sector, market_value, weight_pct, positions, unrealized_pnl
    """
    alloc = get_asset_allocation(user_id)
    if alloc.empty:
        return pd.DataFrame(columns=["sector", "market_value", "weight_pct", "positions"])

    grouped = (
        alloc.groupby("sector")
        .agg(
            market_value=("market_value", "sum"),
            unrealized_pnl=("unrealized_pnl", "sum"),
            positions=("ticker", "count"),
        )
        .reset_index()
    )
    total = grouped["market_value"].sum()
    grouped["weight_pct"] = (grouped["market_value"] / total * 100) if total else 0.0
    return grouped.sort_values("market_value", ascending=False).reset_index(drop=True)


# ===========================================================================
#  4. Return and risk
# ===========================================================================

def get_roi_summary(user_id: int) -> Dict[str, float]:
    """
    Headline return figures, all consistent with one another because they are
    derived from a single portfolio snapshot.

    total_value    = cash + market value of positions
    net_pnl        = total_value - lifetime deposits
    roi_pct        = net_pnl / deposits * 100
    unrealized_pnl = paper gain on open positions
    realized_pnl   = booked gain from sells
    """
    snap = portfolio_service.get_portfolio_snapshot(user_id)
    return {
        "total_value": snap["total_value"],
        "cash": snap["cash"],
        "market_value": snap["market_value"],
        "invested_value": snap["invested_value"],
        "total_deposits": snap["total_deposits"],
        "net_pnl": snap["net_pnl"],
        "roi_pct": snap["roi_pct"],
        "unrealized_pnl": snap["unrealized_pnl"],
        "unrealized_pnl_pct": snap["unrealized_pnl_pct"],
        "realized_pnl": snap["realized_pnl"],
        "day_change": snap["day_change"],
        "day_change_pct": snap["day_change_pct"],
        "position_count": snap["position_count"],
    }


def get_performance_metrics(user_id: int, lookback_days: int = 180) -> Dict[str, Any]:
    """
    Risk/return statistics derived from the value curve.

    max_drawdown_pct : worst peak-to-trough decline in the window
    volatility_pct   : annualised std-dev of daily returns (sigma * sqrt(252))
    best_day_pct / worst_day_pct
    positive_days_pct: share of days with a gain
    period_return_pct: first-to-last change in total value

    NOTE: volatility is annualised from a small daily sample and, like every
    backward-looking metric here, describes the past only.
    """
    empty = {
        "max_drawdown_pct": 0.0, "volatility_pct": 0.0, "best_day_pct": 0.0,
        "worst_day_pct": 0.0, "positive_days_pct": 0.0, "period_return_pct": 0.0,
        "observations": 0,
    }
    ts = get_portfolio_timeseries(user_id, lookback_days)
    if ts.empty or len(ts) < 2:
        return empty

    value = ts["total_value"]
    returns = value.pct_change().dropna()

    running_peak = value.cummax()
    drawdown = (value / running_peak - 1) * 100

    return {
        "max_drawdown_pct": float(drawdown.min()),
        "volatility_pct": float(returns.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR) * 100)
        if len(returns) > 1 else 0.0,
        "best_day_pct": float(returns.max() * 100),
        "worst_day_pct": float(returns.min() * 100),
        "positive_days_pct": float((returns > 0).mean() * 100),
        "period_return_pct": float((value.iloc[-1] / value.iloc[0] - 1) * 100)
        if value.iloc[0] else 0.0,
        "observations": int(len(ts)),
    }


def get_drawdown_series(user_id: int, lookback_days: int = 180) -> pd.DataFrame:
    """Underwater curve: % below the running high-water mark, for plotting."""
    ts = get_portfolio_timeseries(user_id, lookback_days)
    if ts.empty:
        return pd.DataFrame()
    value = ts["total_value"]
    return pd.DataFrame({"drawdown_pct": (value / value.cummax() - 1) * 100})


# ===========================================================================
#  5. Trade analytics
# ===========================================================================

def get_realized_pnl_by_stock(user_id: int) -> pd.DataFrame:
    """
    Booked P&L league table: which names actually made money when closed.
    Columns: ticker, company_name, realized_pnl, sells, shares_sold, proceeds
    """
    ledger = get_transaction_ledger(user_id)
    if ledger.empty:
        return pd.DataFrame()

    sells = ledger[ledger["txn_type"] == "SELL"]
    if sells.empty:
        return pd.DataFrame()

    out = (
        sells.groupby(["ticker", "company_name"])
        .agg(
            realized_pnl=("realized_pnl", "sum"),
            sells=("txn_id", "count"),
            shares_sold=("quantity", "sum"),
            proceeds=("net_amount", "sum"),
        )
        .reset_index()
        .sort_values("realized_pnl", ascending=False)
    )
    return out.round(2)


def get_monthly_activity(user_id: int) -> pd.DataFrame:
    """
    Trading activity aggregated by calendar month - a resample on the ledger.
    Columns: month, buy_value, sell_value, net_investment, trades, turnover
    """
    ledger = get_transaction_ledger(user_id)
    if ledger.empty:
        return pd.DataFrame()

    df = ledger.copy()
    df["month"] = df["date"].dt.to_period("M").dt.to_timestamp()
    df["buy_value"] = np.where(df["txn_type"] == "BUY", df["net_amount"], 0.0)
    df["sell_value"] = np.where(df["txn_type"] == "SELL", df["net_amount"], 0.0)

    monthly = (
        df.groupby("month")
        .agg(
            buy_value=("buy_value", "sum"),
            sell_value=("sell_value", "sum"),
            trades=("txn_id", "count"),
            fees=("fees", "sum"),
        )
        .reset_index()
    )
    monthly["net_investment"] = monthly["buy_value"] - monthly["sell_value"]
    monthly["turnover"] = monthly["buy_value"] + monthly["sell_value"]

    # Round numeric columns only - .round() on the whole frame warns because
    # `month` is a datetime column.
    numeric = monthly.select_dtypes("number").columns
    monthly[numeric] = monthly[numeric].round(2)
    return monthly


def get_trade_statistics(user_id: int) -> Dict[str, Any]:
    """
    Behavioural stats about how the account is traded.
    win_rate_pct counts CLOSED trades only - open positions have no outcome yet.
    """
    ledger = get_transaction_ledger(user_id)
    if ledger.empty:
        return {
            "total_trades": 0, "buy_trades": 0, "sell_trades": 0,
            "total_fees": 0.0, "avg_ticket": 0.0, "win_rate_pct": 0.0,
            "most_traded": "-", "first_trade": None, "last_trade": None,
        }

    sells = ledger[ledger["txn_type"] == "SELL"]
    wins = int((sells["realized_pnl"] > 0).sum()) if not sells.empty else 0

    return {
        "total_trades": int(len(ledger)),
        "buy_trades": int((ledger["txn_type"] == "BUY").sum()),
        "sell_trades": int(len(sells)),
        "total_fees": float(ledger["fees"].sum()),
        "avg_ticket": float(ledger["gross_amount"].mean()),
        "win_rate_pct": float(wins / len(sells) * 100) if len(sells) else 0.0,
        "most_traded": str(ledger["ticker"].mode().iloc[0]),
        "first_trade": ledger["executed_at"].min(),
        "last_trade": ledger["executed_at"].max(),
    }


def get_cumulative_investment_curve(user_id: int) -> pd.DataFrame:
    """
    Capital deployed over time: cumulative buy outlay vs cumulative sell
    proceeds. Plotting it against portfolio value shows whether growth came
    from performance or simply from putting more money in.
    """
    ledger = get_transaction_ledger(user_id)
    if ledger.empty:
        return pd.DataFrame()

    df = ledger.sort_values("executed_at").copy()
    df["cum_invested"] = np.where(df["txn_type"] == "BUY", df["net_amount"], 0.0).cumsum()
    df["cum_divested"] = np.where(df["txn_type"] == "SELL", df["net_amount"], 0.0).cumsum()
    df["net_deployed"] = df["cum_invested"] - df["cum_divested"]

    return (
        df.set_index("executed_at")[["cum_invested", "cum_divested", "net_deployed"]]
        .resample("D")
        .last()
        .ffill()
        .round(2)
    )
