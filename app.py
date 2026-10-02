"""
app.py
======
TIER 3 - PRESENTATION LAYER.

Architectural rule enforced throughout this file:

        Streamlit UI  ->  Service Layer  ->  Database
                      X   never skips a tier

There is no SQL string and no yfinance call anywhere below. This file only:
collects input, calls a service, renders the result, and formats errors. All
business rules live in `services/`.

One documented exception: `_db_status()` does a local import of
`db.database.healthcheck` to render the sidebar connection badge. That is
infrastructure status, not business logic; move it behind a service function
if you want the rule to be absolute.

Run with:   streamlit run app.py
"""

from __future__ import annotations

import os
from datetime import datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# --- service layer: the ONLY backend imports allowed in this file -----------
from services import analytics_service, portfolio_service, transaction_service
from services.transaction_service import TransactionError

load_dotenv()

CURRENCY = os.getenv("CURRENCY_SYMBOL", "₹")
GOLD = "#d4af37"
GREEN = "#2e9e5b"
RED = "#c0392b"

st.set_page_config(
    page_title="Portfolio Intelligence",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ===========================================================================
#  Styling - a thin CSS layer on top of .streamlit/config.toml
# ===========================================================================

st.markdown(
    f"""
    <style>
        .block-container {{ padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1400px; }}
        h1, h2, h3 {{ letter-spacing: -0.02em; font-weight: 600; }}

        /* Metric cards: charcoal tiles with a gold hairline */
        div[data-testid="stMetric"] {{
            background: #181818;
            border: 1px solid #2a2a2a;
            border-left: 3px solid {GOLD};
            border-radius: 10px;
            padding: 16px 18px;
        }}
        div[data-testid="stMetricLabel"] p {{
            font-size: 0.75rem; text-transform: uppercase;
            letter-spacing: 0.09em; color: #9a9a9a;
        }}
        div[data-testid="stMetricValue"] {{ font-size: 1.6rem; font-weight: 600; }}

        /* Gold primary buttons */
        div.stButton > button[kind="primary"],
        div.stFormSubmitButton > button {{
            background: {GOLD}; color: #121212; border: none;
            font-weight: 600; letter-spacing: 0.02em;
        }}
        div.stButton > button[kind="primary"]:hover,
        div.stFormSubmitButton > button:hover {{ background: #e8c65a; color: #121212; }}

        /* Tabs + sidebar accents */
        .stTabs [aria-selected="true"] {{ color: {GOLD} !important; }}
        section[data-testid="stSidebar"] {{ border-right: 1px solid #262626; }}
        hr {{ border-color: #262626; }}
        .brand {{ font-size: 1.05rem; font-weight: 700; color: {GOLD};
                  letter-spacing: 0.14em; text-transform: uppercase; }}
        .muted {{ color: #8a8a8a; font-size: 0.82rem; }}
    </style>
    """,
    unsafe_allow_html=True,
)


# ===========================================================================
#  Formatting helpers (pure presentation - no business logic)
# ===========================================================================

def money(value: float | None) -> str:
    """Currency formatting with thousands separators."""
    if value is None or pd.isna(value):
        return "-"
    return f"{CURRENCY}{value:,.2f}"


def pct(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:+.2f}%"


# ===========================================================================
#  Cached service reads
#  Caching lives in the UI tier on purpose: it is a rendering concern, and
#  services stay framework-agnostic. Every write clears these caches.
# ===========================================================================

@st.cache_data(ttl=60, show_spinner=False)
def load_snapshot(user_id: int) -> dict:
    return portfolio_service.get_portfolio_snapshot(user_id)


@st.cache_data(ttl=60, show_spinner=False)
def load_timeseries(user_id: int, days: int) -> pd.DataFrame:
    return analytics_service.get_portfolio_timeseries(user_id, days)


@st.cache_data(ttl=60, show_spinner=False)
def load_ledger(user_id: int) -> pd.DataFrame:
    return analytics_service.get_transaction_ledger(user_id)


@st.cache_data(ttl=60, show_spinner=False)
def load_allocation(user_id: int) -> pd.DataFrame:
    return analytics_service.get_asset_allocation(user_id)


@st.cache_data(ttl=60, show_spinner=False)
def load_sector_allocation(user_id: int) -> pd.DataFrame:
    return analytics_service.get_sector_allocation(user_id)


@st.cache_data(ttl=300, show_spinner=False)
def load_users() -> pd.DataFrame:
    return portfolio_service.list_users()


def invalidate_caches() -> None:
    """Called after every successful write so the UI never shows stale state."""
    st.cache_data.clear()
    portfolio_service.clear_price_cache()


# ===========================================================================
#  Sidebar - navigation, account switcher, connection status
# ===========================================================================

def render_sidebar() -> tuple[str, int | None]:
    with st.sidebar:
        st.markdown('<div class="brand">◆ Portfolio Intelligence</div>', unsafe_allow_html=True)
        st.caption("Real-time equity portfolio analytics")
        st.divider()

        # --- connection status: surfaced, not swallowed --------------------
        ok, message = _db_status()
        if ok:
            st.success(message, icon="✅")
        else:
            st.error(message, icon="🚫")
            st.stop()  # nothing below can work without a database

        # --- account switcher ---------------------------------------------
        users = load_users()
        if users.empty:
            st.warning("No accounts found. Load schema.sql to seed the demo user.")
            st.stop()

        labels = {
            int(r.user_id): f"{r.full_name} (@{r.username})"
            for r in users.itertuples()
        }
        user_id = st.selectbox(
            "Account",
            options=list(labels.keys()),
            format_func=lambda uid: labels[uid],
        )

        st.divider()
        page = st.radio(
            "Navigate",
            ["Dashboard", "Trading Desk", "Analytics", "Ledger", "Account"],
            label_visibility="collapsed",
        )

        st.divider()
        if st.button("↻ Refresh market data", use_container_width=True):
            invalidate_caches()
            st.rerun()
        st.markdown(
            f'<div class="muted">Quotes via Yahoo Finance, cached '
            f'{os.getenv("PRICE_CACHE_TTL", "60")}s.<br>'
            f'Updated {datetime.now():%d %b %Y, %H:%M:%S}</div>',
            unsafe_allow_html=True,
        )

    return page, int(user_id)


def _db_status() -> tuple[bool, str]:
    """
    Ask the service layer whether the backend is reachable.
    Wrapped so an import-time config error renders as a message, not a stack trace.
    """
    try:
        from db.database import healthcheck  # local import: status widget only

        return healthcheck()
    except Exception as exc:  # noqa: BLE001
        return False, f"Configuration error: {exc}"


# ===========================================================================
#  Shared components
# ===========================================================================

def render_metric_row(snapshot: dict) -> None:
    """TOP ROW - the four headline figures, all from one snapshot."""
    c1, c2, c3, c4 = st.columns(4)

    c1.metric(
        "Total Portfolio Value",
        money(snapshot["total_value"]),
        f"{money(snapshot['day_change'])} today",
        delta_color="normal",
    )
    c2.metric("Cash Balance", money(snapshot["cash"]))
    c3.metric(
        "Return on Investment",
        pct(snapshot["roi_pct"]),
        f"{money(snapshot['net_pnl'])} net P&L",
    )
    c4.metric(
        "Unrealised P&L",
        money(snapshot["unrealized_pnl"]),
        pct(snapshot["unrealized_pnl_pct"]),
    )

    if snapshot["stale_prices"]:
        st.warning(
            "Live quotes unavailable for: "
            + ", ".join(snapshot["stale_prices"])
            + ". These positions are valued at cost, so P&L for them reads as zero.",
            icon="⚠️",
        )


def render_trade_form(user_id: int, key: str = "trade") -> None:
    """Order-entry form. Submits to transaction_service and nothing else."""
    st.markdown("##### Place an order")

    known = portfolio_service.search_known_tickers(limit=12)
    if known:
        st.caption("Known symbols: " + " · ".join(known))

    with st.form(f"{key}_form", clear_on_submit=False):
        ticker = st.text_input(
            "Ticker",
            value=known[0] if known else "",
            placeholder="TCS.NS",
            help="Yahoo Finance symbol. NSE: TCS.NS · BSE: TCS.BO · US: AAPL. "
                 "Unknown symbols are registered automatically on first trade.",
        )

        col_a, col_b = st.columns(2)
        side = col_a.selectbox("Side", ["BUY", "SELL"])
        quantity = col_b.number_input("Quantity", min_value=0.0, step=1.0, value=1.0)

        use_live = st.checkbox("Use live market price", value=True)
        manual_price = st.number_input(
            "Execution price",
            min_value=0.0,
            step=0.05,
            value=0.0,
            disabled=use_live,
            help="Ignored when 'use live market price' is ticked.",
        )
        notes = st.text_input("Note (optional)", placeholder="e.g. SIP tranche 3")

        submitted = st.form_submit_button("Execute order", use_container_width=True)

    if not submitted:
        return

    # --- the entire UI-side error contract: catch TransactionError ---------
    try:
        with st.spinner("Routing order…"):
            receipt = transaction_service.execute_order(
                user_id=user_id,
                ticker=ticker,
                side=side,
                quantity=quantity,
                price=None if use_live else manual_price,
                notes=notes or None,
            )
    except TransactionError as exc:
        # Business-rule failure. The DB rolled back; nothing was written.
        st.error(str(exc), icon="🚫")
        return
    except Exception as exc:  # noqa: BLE001 - last-resort guard
        st.error(f"Unexpected error, order not placed: {exc}", icon="🚫")
        return

    invalidate_caches()
    st.success(receipt["message"], icon="✅")
    st.caption(
        f"Ticket #{receipt['txn_id']} · {receipt['side']} {receipt['ticker']} · "
        f"cash now {money(receipt['cash_after'])}"
    )
    st.rerun()


def render_holdings_table(holdings: pd.DataFrame) -> None:
    """Live positions, mark-to-market."""
    if holdings.empty:
        st.info("No open positions yet. Place your first order to get started.")
        return

    view = holdings[
        ["ticker", "quantity", "avg_cost", "current_price",
         "market_value", "unrealized_pnl", "unrealized_pnl_pct", "weight_pct"]
    ].rename(
        columns={
            "ticker": "Ticker", "quantity": "Qty", "avg_cost": "Avg cost",
            "current_price": "Price", "market_value": "Value",
            "unrealized_pnl": "P&L", "unrealized_pnl_pct": "P&L %",
            "weight_pct": "Weight %",
        }
    )
    st.dataframe(
        view,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Avg cost": st.column_config.NumberColumn(format="%.2f"),
            "Price": st.column_config.NumberColumn(format="%.2f"),
            "Value": st.column_config.NumberColumn(format="%.2f"),
            "P&L": st.column_config.NumberColumn(format="%.2f"),
            "P&L %": st.column_config.NumberColumn(format="%.2f%%"),
            "Weight %": st.column_config.ProgressColumn(
                format="%.1f%%", min_value=0, max_value=100
            ),
        },
    )


def render_ledger_table(ledger: pd.DataFrame, rows: int = 12) -> None:
    """Most recent trades from the immutable transaction ledger."""
    if ledger.empty:
        st.info("The ledger is empty.")
        return

    view = ledger.head(rows)[
        ["executed_at", "ticker", "txn_type", "quantity", "price_per_share", "net_amount"]
    ].rename(
        columns={
            "executed_at": "When", "ticker": "Ticker", "txn_type": "Side",
            "quantity": "Qty", "price_per_share": "Price", "net_amount": "Amount",
        }
    )
    st.dataframe(
        view,
        use_container_width=True,
        hide_index=True,
        column_config={
            "When": st.column_config.DatetimeColumn(format="DD MMM, HH:mm"),
            "Price": st.column_config.NumberColumn(format="%.2f"),
            "Amount": st.column_config.NumberColumn(format="%.2f"),
        },
    )


# ===========================================================================
#  PAGE 1 - Dashboard
# ===========================================================================

def page_dashboard(user_id: int) -> None:
    st.title("Portfolio Dashboard")

    snapshot = load_snapshot(user_id)

    # ---------------- TOP ROW: metrics ------------------------------------
    render_metric_row(snapshot)
    st.divider()

    # ---------------- MIDDLE ROW: charts ----------------------------------
    chart_col, alloc_col = st.columns([2, 1])

    with chart_col:
        st.markdown("##### Portfolio value over time")
        window = st.radio(
            "Window", options=["1M", "3M", "6M", "1Y"], index=1,
            horizontal=True, label_visibility="collapsed",
        )
        days = {"1M": 30, "3M": 90, "6M": 180, "1Y": 365}[window]

        ts = load_timeseries(user_id, days)
        if ts.empty:
            st.info("The growth curve appears once you have trade history.")
        else:
            if ts.attrs.get("price_basis") == "cost_basis":
                st.caption("⚠️ Price history unavailable — curve is drawn at cost basis.")
            st.line_chart(
                ts[["total_value", "market_value", "cash"]],
                height=300,
                color=[GOLD, "#6b8fb5", "#5a5a5a"],
            )

    with alloc_col:
        st.markdown("##### Asset allocation")
        alloc = load_allocation(user_id)
        if alloc.empty:
            st.info("No positions to allocate.")
        else:
            st.bar_chart(
                alloc.set_index("ticker")["weight_pct"],
                height=300, color=GOLD, horizontal=True,
            )

    st.divider()

    # ---------------- BOTTOM ROW: form (left) + tables (right) ------------
    left, right = st.columns([1, 2])

    with left:
        render_trade_form(user_id, key="dashboard")

    with right:
        tab_holdings, tab_ledger = st.tabs(["Live holdings", "Recent activity"])
        with tab_holdings:
            render_holdings_table(snapshot["holdings"])
        with tab_ledger:
            render_ledger_table(load_ledger(user_id))


# ===========================================================================
#  PAGE 2 - Trading Desk
# ===========================================================================

def page_trading_desk(user_id: int) -> None:
    st.title("Trading Desk")
    snapshot = load_snapshot(user_id)

    c1, c2, c3 = st.columns(3)
    c1.metric("Buying power", money(snapshot["cash"]))
    c2.metric("Open positions", snapshot["position_count"])
    c3.metric("Invested capital", money(snapshot["invested_value"]))
    st.divider()

    left, right = st.columns([1, 1.4])
    with left:
        render_trade_form(user_id, key="desk")

    with right:
        st.markdown("##### Quote lookup")
        symbol = st.text_input("Symbol", value="RELIANCE.NS", key="quote_lookup")
        if st.button("Get quote"):
            price = portfolio_service.get_live_price(symbol)
            if price is None:
                st.error(f"No quote found for '{symbol}'. Check the suffix (.NS / .BO).")
            else:
                profile = portfolio_service.fetch_company_profile(symbol)
                st.metric(profile["company_name"], money(price))
                st.caption(f"{profile['sector']} · {profile.get('exchange') or 'n/a'}")

        st.markdown("##### Current positions")
        render_holdings_table(snapshot["holdings"])


# ===========================================================================
#  PAGE 3 - Analytics (the BI surface)
# ===========================================================================

def page_analytics(user_id: int) -> None:
    st.title("Business Intelligence")

    days = st.slider("Analysis window (days)", 30, 730, 180, step=30)
    ts = load_timeseries(user_id, days)
    metrics = analytics_service.get_performance_metrics(user_id, days)
    stats = analytics_service.get_trade_statistics(user_id)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Period return", pct(metrics["period_return_pct"]))
    c2.metric("Max drawdown", pct(metrics["max_drawdown_pct"]))
    c3.metric("Annualised volatility", f"{metrics['volatility_pct']:.2f}%")
    c4.metric("Positive days", f"{metrics['positive_days_pct']:.1f}%")
    st.caption(
        f"Computed from {metrics['observations']} daily observations. "
        "Backward-looking statistics only — they describe the past, not expected returns."
    )
    st.divider()

    tab1, tab2, tab3, tab4 = st.tabs(
        ["Growth & drawdown", "Allocation", "Realised P&L", "Activity"]
    )

    with tab1:
        if ts.empty:
            st.info("No history to analyse yet.")
        else:
            st.markdown("##### Total value vs capital deployed")
            st.area_chart(ts[["total_value"]], height=260, color=GOLD)
            st.markdown("##### Underwater curve (drawdown %)")
            dd = analytics_service.get_drawdown_series(user_id, days)
            if not dd.empty:
                st.area_chart(dd, height=200, color=RED)
            st.markdown("##### Daily returns (%)")
            st.bar_chart(ts[["daily_return"]], height=200, color=GOLD)

    with tab2:
        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("##### By instrument")
            alloc = load_allocation(user_id)
            if alloc.empty:
                st.info("No open positions.")
            else:
                st.bar_chart(
                    alloc.set_index("ticker")["weight_pct"], height=280,
                    color=GOLD, horizontal=True,
                )
                st.dataframe(
                    alloc[["ticker", "sector", "market_value", "weight_pct",
                           "unrealized_pnl_pct"]],
                    use_container_width=True, hide_index=True,
                )
        with col_b:
            st.markdown("##### By sector")
            sectors = load_sector_allocation(user_id)
            if sectors.empty:
                st.info("No sector data.")
            else:
                st.bar_chart(
                    sectors.set_index("sector")["weight_pct"], height=280,
                    color="#6b8fb5", horizontal=True,
                )
                st.dataframe(sectors, use_container_width=True, hide_index=True)
                top = sectors.iloc[0]
                if top["weight_pct"] > 40:
                    st.warning(
                        f"Concentration risk: {top['weight_pct']:.0f}% of the "
                        f"portfolio sits in {top['sector']}.",
                        icon="⚠️",
                    )

    with tab3:
        realized = analytics_service.get_realized_pnl_by_stock(user_id)
        if realized.empty:
            st.info("No closed trades yet — realised P&L appears after your first sell.")
        else:
            st.bar_chart(
                realized.set_index("ticker")["realized_pnl"], height=280, color=GOLD
            )
            st.dataframe(realized, use_container_width=True, hide_index=True)
            k1, k2, k3 = st.columns(3)
            k1.metric("Total realised", money(realized["realized_pnl"].sum()))
            k2.metric("Win rate", f"{stats['win_rate_pct']:.0f}%")
            k3.metric("Fees paid", money(stats["total_fees"]))

    with tab4:
        monthly = analytics_service.get_monthly_activity(user_id)
        if monthly.empty:
            st.info("No trading activity recorded.")
        else:
            st.markdown("##### Monthly buy vs sell value")
            st.bar_chart(
                monthly.set_index("month")[["buy_value", "sell_value"]],
                height=280, color=[GOLD, "#6b8fb5"],
            )
            st.dataframe(monthly, use_container_width=True, hide_index=True)
            k1, k2, k3 = st.columns(3)
            k1.metric("Total trades", stats["total_trades"])
            k2.metric("Average ticket", money(stats["avg_ticket"]))
            k3.metric("Most traded", stats["most_traded"])


# ===========================================================================
#  PAGE 4 - Ledger
# ===========================================================================

def page_ledger(user_id: int) -> None:
    st.title("Transaction Ledger")
    st.caption("Append-only audit trail. Every figure in this app is derived from it.")

    ledger = load_ledger(user_id)
    if ledger.empty:
        st.info("No transactions recorded for this account.")
        return

    f1, f2, f3 = st.columns(3)
    side_filter = f1.multiselect("Side", ["BUY", "SELL"], default=["BUY", "SELL"])
    ticker_filter = f2.multiselect("Ticker", sorted(ledger["ticker"].unique()))
    search = f3.text_input("Search notes", placeholder="keyword")

    view = ledger[ledger["txn_type"].isin(side_filter)]
    if ticker_filter:
        view = view[view["ticker"].isin(ticker_filter)]
    if search:
        view = view[view["notes"].fillna("").str.contains(search, case=False)]

    st.dataframe(
        view[
            ["txn_id", "executed_at", "ticker", "company_name", "txn_type",
             "quantity", "price_per_share", "fees", "net_amount", "realized_pnl", "notes"]
        ],
        use_container_width=True,
        hide_index=True,
        column_config={
            "executed_at": st.column_config.DatetimeColumn("When", format="DD MMM YYYY, HH:mm"),
            "price_per_share": st.column_config.NumberColumn("Price", format="%.2f"),
            "net_amount": st.column_config.NumberColumn("Net", format="%.2f"),
            "realized_pnl": st.column_config.NumberColumn("Realised P&L", format="%.2f"),
        },
    )

    st.download_button(
        "⬇ Export filtered ledger (CSV)",
        data=view.to_csv(index=False).encode("utf-8"),
        file_name=f"ledger_user{user_id}_{datetime.now():%Y%m%d}.csv",
        mime="text/csv",
    )


# ===========================================================================
#  PAGE 5 - Account
# ===========================================================================

def page_account(user_id: int) -> None:
    st.title("Account")
    snapshot = load_snapshot(user_id)
    user = portfolio_service.get_user(user_id)

    c1, c2, c3 = st.columns(3)
    c1.metric("Cash balance", money(snapshot["cash"]))
    c2.metric("Lifetime deposits", money(snapshot["total_deposits"]))
    c3.metric("Net P&L", money(snapshot["net_pnl"]), pct(snapshot["roi_pct"]))

    if user:
        st.caption(
            f"{user['full_name']} · @{user['username']} · {user['email']} · "
            f"base currency {user['base_currency']}"
        )
    st.divider()

    left, right = st.columns(2)

    with left:
        st.markdown("##### Deposit funds")
        with st.form("deposit_form"):
            amount = st.number_input("Amount", min_value=0.0, step=1000.0, value=10000.0)
            if st.form_submit_button("Deposit", use_container_width=True):
                try:
                    result = transaction_service.deposit_cash(user_id, amount)
                    invalidate_caches()
                    st.success(result["message"], icon="✅")
                    st.rerun()
                except TransactionError as exc:
                    st.error(str(exc), icon="🚫")

    with right:
        st.markdown("##### Withdraw funds")
        with st.form("withdraw_form"):
            amount_w = st.number_input(
                "Amount ", min_value=0.0, step=1000.0, value=1000.0
            )
            if st.form_submit_button("Withdraw", use_container_width=True):
                try:
                    result = transaction_service.withdraw_cash(user_id, amount_w)
                    invalidate_caches()
                    st.success(result["message"], icon="✅")
                    st.rerun()
                except TransactionError as exc:
                    st.error(str(exc), icon="🚫")

    st.divider()
    st.markdown("##### How ROI is calculated")
    st.markdown(
        """
        `ROI % = (cash + market value of holdings − lifetime deposits) / lifetime deposits × 100`

        Deposits raise both the numerator and the denominator, so adding money
        to the account never shows up as a gain. Realised P&L is booked against
        the weighted-average cost basis at the moment of each sell, not FIFO.
        """
    )


# ===========================================================================
#  Router
# ===========================================================================

def main() -> None:
    page, user_id = render_sidebar()
    if user_id is None:
        st.stop()

    if page == "Dashboard":
        page_dashboard(user_id)
    elif page == "Trading Desk":
        page_trading_desk(user_id)
    elif page == "Analytics":
        page_analytics(user_id)
    elif page == "Ledger":
        page_ledger(user_id)
    elif page == "Account":
        page_account(user_id)


if __name__ == "__main__":
    main()
