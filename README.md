# Real-Time Stock Portfolio Management System

A 3-tier portfolio analytics platform: **Streamlit UI → Service Layer → MySQL**, with live
quotes from Yahoo Finance and a Pandas-based BI engine.

```
┌───────────────────────────────────────────────┐
│  app.py            PRESENTATION               │  Streamlit widgets only.
│  .streamlit/config.toml                       │  No SQL, no yfinance.
└───────────────────┬───────────────────────────┘
                    │  function calls
┌───────────────────▼───────────────────────────┐
│  services/transaction_service.py   WRITE PATH │  Order execution, ACID trades
│  services/portfolio_service.py     MARKET     │  Quotes, valuation, snapshot
│  services/analytics_service.py     BI         │  Pandas time-series, ROI, risk
└───────────────────┬───────────────────────────┘
                    │  SQLAlchemy Core (named binds)
┌───────────────────▼───────────────────────────┐
│  db/database.py    DATA ACCESS                │  Pool, session_scope, fetch_df
│  schema.sql        InnoDB / DECIMAL / FKs     │
└───────────────────────────────────────────────┘
```

## Setup

```bash
# 1. dependencies
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. database
mysql -u root -p < schema.sql

# 3. credentials
cp .env .env.local   # or edit .env directly; fill in DB_USER / DB_PASSWORD

# 4. run
streamlit run app.py
```

The sidebar shows a connection badge. If it is red, the message tells you whether the
credentials, the server, or the missing tables are the problem.

## File map

| File | Tier | Purpose |
|---|---|---|
| `requirements.txt` | — | Pinned dependency ranges |
| `schema.sql` | data | InnoDB tables, constraints, indexes, view, seed data |
| `.env` | config | DB credentials, pool sizing, fee rate, cache TTL |
| `db/database.py` | data access | Connection pool, `session_scope()`, `fetch_df()` |
| `services/transaction_service.py` | service | BUY/SELL with row locks, commit/rollback |
| `services/portfolio_service.py` | service | yfinance quotes, holdings, valuation snapshot |
| `services/analytics_service.py` | service | Pandas BI: growth curve, allocation, ROI, drawdown |
| `app.py` | UI | Dashboard, Trading Desk, Analytics, Ledger, Account |
| `.streamlit/config.toml` | UI | Charcoal `#121212` / gold `#d4af37` dark theme |

## How a trade stays consistent

`execute_order()` opens one transaction and, inside it:

1. locks the `Users` row (`SELECT ... FOR UPDATE`) — always first, so concurrent trades
   cannot deadlock by taking locks in opposite order;
2. locks the `Holdings` row;
3. moves cash, upserts the position, appends the ledger row.

Any exception rolls the whole thing back, so cash can never be debited without shares being
credited. Price discovery happens *before* the transaction opens, so a slow HTTP call never
holds database locks.

## Known limitations (by design, not oversight)

- **Cost basis is weighted-average, not FIFO.** Realised P&L uses the average cost at the
  moment of sale. Indian tax reporting expects FIFO — swap `_apply_sell` for a lot-tracking
  table if you need that.
- **Cash history is approximate.** `Users` stores a running `total_deposits`, not a dated
  cash-movement ledger, so the historical cash curve assumes deposits predate the chart
  window. Add a `CashFlows` table for deposit-accurate history.
- **yfinance is an unofficial scraper.** It rate-limits and occasionally returns nothing.
  Every quote path degrades to cost basis and flags the affected tickers rather than failing.
- **No authentication.** The account switcher trusts whoever opens the page. Do not expose
  this to a network without adding auth.
- **Single currency in totals.** USD and INR holdings are summed without FX conversion.
- **No short selling or margin** — sells are capped at the held quantity.
