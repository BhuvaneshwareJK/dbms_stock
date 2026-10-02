"""
services/transaction_service.py
===============================
TIER 2 - SERVICE LAYER (order execution / write path).

This is the only module in the system allowed to mutate cash, holdings or the
ledger. Everything it writes happens inside ONE database transaction, so the
three side effects of a trade

    (1) cash balance moves
    (2) Holdings row is upserted
    (3) an immutable Transactions row is appended

either all land or none of them do. A crash, a duplicate click, or a failed
constraint mid-way leaves the account exactly as it was.

Concurrency model
-----------------
Streamlit users can double-submit a form, and several sessions may share an
account. We therefore take pessimistic row locks in a FIXED ORDER inside each
trade:

        SELECT ... FROM Users    WHERE user_id  = :uid FOR UPDATE
        SELECT ... FROM Holdings WHERE ...            FOR UPDATE

Locking Users first in every code path means two concurrent trades can never
deadlock by grabbing the same two rows in opposite order. InnoDB holds these
locks until COMMIT/ROLLBACK.

Money arithmetic
----------------
All amounts are `decimal.Decimal`, quantised on the way into the database to
match the column scale (2 dp for currency, 4 dp for quantity/price). Floats are
never used for money here - 0.1 + 0.2 != 0.3 is not an acceptable property for
a ledger.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from db.database import session_scope
from services import portfolio_service

logger = logging.getLogger(__name__)

# Quantisers matching the DECIMAL scales declared in schema.sql
MONEY = Decimal("0.01")   # DECIMAL(18,2)
UNITS = Decimal("0.0001")  # DECIMAL(18,4)


# ===========================================================================
#  Exception hierarchy - the UI catches TransactionError and shows .args[0]
# ===========================================================================

class TransactionError(Exception):
    """Base class for every business-rule failure in the trading path."""


class InvalidOrderError(TransactionError):
    """Malformed order: bad quantity, unknown side, blank ticker."""


class UnknownUserError(TransactionError):
    """The user_id does not exist."""


class InsufficientFundsError(TransactionError):
    """Order value exceeds settled cash."""


class InsufficientSharesError(TransactionError):
    """Sell quantity exceeds the open position (no short selling)."""


class PriceUnavailableError(TransactionError):
    """No manual price given and the live quote could not be fetched."""


# ===========================================================================
#  Helpers
# ===========================================================================

def _to_decimal(value: Any, quant: Decimal, field: str) -> Decimal:
    """
    Convert user input to Decimal safely.

    `str(value)` first: Decimal(0.1) inherits the float's representation error,
    whereas Decimal("0.1") is exact.
    """
    try:
        return Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidOrderError(f"'{field}' is not a valid number: {value!r}") from exc


def _fee_rate() -> Decimal:
    """Brokerage rate from the environment (e.g. 0.0005 = 0.05% per trade)."""
    import os

    try:
        return Decimal(str(os.getenv("BROKERAGE_FEE_PCT", "0") or "0"))
    except InvalidOperation:
        return Decimal("0")


def _get_or_create_stock(session: Session, ticker: str) -> int:
    """
    Resolve a ticker to a stock_id inside the caller's transaction, creating
    the reference row (with yfinance metadata) the first time it is traded.

    Uses INSERT ... ON DUPLICATE KEY UPDATE so two simultaneous first-trades on
    the same new symbol cannot produce a duplicate-key crash.
    """
    symbol = ticker.strip().upper()
    if not symbol:
        raise InvalidOrderError("Ticker symbol cannot be empty.")

    row = session.execute(
        text("SELECT stock_id FROM Stocks WHERE ticker = :t"), {"t": symbol}
    ).first()
    if row:
        return int(row[0])

    profile = portfolio_service.fetch_company_profile(symbol)
    session.execute(
        text(
            """
            INSERT INTO Stocks (ticker, company_name, sector, industry, exchange, currency)
            VALUES (:ticker, :company_name, :sector, :industry, :exchange, :currency)
            ON DUPLICATE KEY UPDATE company_name = VALUES(company_name)
            """
        ),
        profile,
    )
    new_row = session.execute(
        text("SELECT stock_id FROM Stocks WHERE ticker = :t"), {"t": symbol}
    ).first()
    if not new_row:  # pragma: no cover - should be unreachable
        raise TransactionError(f"Failed to register instrument '{symbol}'.")
    logger.info("Registered new instrument %s (%s)", symbol, profile["company_name"])
    return int(new_row[0])


def _lock_user(session: Session, user_id: int) -> Decimal:
    """Take an exclusive row lock on the account and return its cash balance."""
    row = session.execute(
        text("SELECT cash_balance FROM Users WHERE user_id = :uid FOR UPDATE"),
        {"uid": user_id},
    ).first()
    if row is None:
        raise UnknownUserError(f"No account found with user_id={user_id}.")
    return Decimal(str(row[0]))


def _lock_holding(session: Session, user_id: int, stock_id: int):
    """Lock the position row (if any) and return (quantity, avg_cost)."""
    row = session.execute(
        text(
            """
            SELECT quantity, avg_cost FROM Holdings
            WHERE user_id = :uid AND stock_id = :sid
            FOR UPDATE
            """
        ),
        {"uid": user_id, "sid": stock_id},
    ).first()
    if row is None:
        return Decimal("0"), Decimal("0")
    return Decimal(str(row[0])), Decimal(str(row[1]))


def _insert_ledger_row(
    session: Session,
    *,
    user_id: int,
    stock_id: int,
    txn_type: str,
    quantity: Decimal,
    price: Decimal,
    fees: Decimal,
    gross: Decimal,
    net: Decimal,
    realized: Optional[Decimal],
    notes: Optional[str],
) -> int:
    """Append the immutable audit record and return its txn_id."""
    session.execute(
        text(
            """
            INSERT INTO Transactions
                (user_id, stock_id, txn_type, quantity, price_per_share,
                 fees, gross_amount, net_amount, realized_pnl, executed_at, notes)
            VALUES
                (:user_id, :stock_id, :txn_type, :quantity, :price,
                 :fees, :gross, :net, :realized, :executed_at, :notes)
            """
        ),
        {
            "user_id": user_id,
            "stock_id": stock_id,
            "txn_type": txn_type,
            "quantity": quantity,
            "price": price,
            "fees": fees,
            "gross": gross,
            "net": net,
            "realized": realized,
            "executed_at": datetime.now(),
            "notes": notes,
        },
    )
    return int(session.execute(text("SELECT LAST_INSERT_ID()")).scalar() or 0)


# ===========================================================================
#  Public API
# ===========================================================================

def execute_order(
    user_id: int,
    ticker: str,
    side: str,
    quantity: float | str | Decimal,
    price: Optional[float | str | Decimal] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute a BUY or SELL and return a receipt dict.

    Parameters
    ----------
    user_id   : account placing the order
    ticker    : Yahoo-style symbol ('TCS.NS', 'AAPL')
    side      : 'BUY' or 'SELL' (case-insensitive)
    quantity  : shares, must be > 0
    price     : execution price. If None, the live quote is used and the order
                fails cleanly (PriceUnavailableError) when no quote exists.
    notes     : free-text tag stored on the ledger row

    Raises any subclass of TransactionError on a business-rule violation; the
    database is untouched in that case.
    """
    side = str(side).strip().upper()
    if side not in ("BUY", "SELL"):
        raise InvalidOrderError(f"Order side must be BUY or SELL, got {side!r}.")

    symbol = str(ticker).strip().upper()
    qty = _to_decimal(quantity, UNITS, "quantity")
    if qty <= 0:
        raise InvalidOrderError("Quantity must be greater than zero.")

    # ---- price discovery happens BEFORE the transaction opens -------------
    # A network call inside an open transaction would hold row locks for the
    # entire HTTP round-trip, blocking other traders for seconds.
    if price is None:
        live = portfolio_service.get_live_price(symbol)
        if live is None:
            raise PriceUnavailableError(
                f"Live price unavailable for '{symbol}'. "
                "Enter the execution price manually to proceed."
            )
        exec_price = _to_decimal(live, UNITS, "price")
    else:
        exec_price = _to_decimal(price, UNITS, "price")
    if exec_price <= 0:
        raise InvalidOrderError("Execution price must be greater than zero.")

    gross = (qty * exec_price).quantize(MONEY, rounding=ROUND_HALF_UP)
    fees = (gross * _fee_rate()).quantize(MONEY, rounding=ROUND_HALF_UP)

    try:
        # --- ATOMIC BLOCK -------------------------------------------------
        with session_scope() as session:
            stock_id = _get_or_create_stock(session, symbol)
            cash = _lock_user(session, user_id)              # lock order: Users
            held_qty, avg_cost = _lock_holding(session, user_id, stock_id)  # then Holdings

            if side == "BUY":
                receipt = _apply_buy(
                    session,
                    user_id=user_id,
                    stock_id=stock_id,
                    cash=cash,
                    qty=qty,
                    price=exec_price,
                    gross=gross,
                    fees=fees,
                    notes=notes,
                )
            else:
                receipt = _apply_sell(
                    session,
                    user_id=user_id,
                    stock_id=stock_id,
                    cash=cash,
                    held_qty=held_qty,
                    avg_cost=avg_cost,
                    qty=qty,
                    price=exec_price,
                    gross=gross,
                    fees=fees,
                    notes=notes,
                )
        # --- COMMIT happened on clean exit of the with-block ---------------
    except TransactionError:
        raise  # business rule - already has a user-facing message
    except SQLAlchemyError as exc:
        # Constraint violations (e.g. the CHECK on cash_balance) land here.
        logger.exception("Database error while executing order")
        raise TransactionError(
            f"The order was rolled back due to a database error: {exc.__class__.__name__}"
        ) from exc

    receipt.update({"ticker": symbol, "side": side})
    logger.info(
        "%s %s x%s @ %s for user %s (txn %s)",
        side, symbol, qty, exec_price, user_id, receipt.get("txn_id"),
    )
    return receipt


def _apply_buy(
    session: Session,
    *,
    user_id: int,
    stock_id: int,
    cash: Decimal,
    qty: Decimal,
    price: Decimal,
    gross: Decimal,
    fees: Decimal,
    notes: Optional[str],
) -> Dict[str, Any]:
    """Debit cash, upsert the position with a new weighted-average cost."""
    total_cost = gross + fees
    if total_cost > cash:
        raise InsufficientFundsError(
            f"Insufficient funds: this order costs {total_cost:,.2f} "
            f"but only {cash:,.2f} is available."
        )

    session.execute(
        text("UPDATE Users SET cash_balance = cash_balance - :amt WHERE user_id = :uid"),
        {"amt": total_cost, "uid": user_id},
    )

    # NOTE on the upsert below: MySQL evaluates ON DUPLICATE KEY UPDATE
    # assignments left to right, so `avg_cost` MUST be recomputed BEFORE
    # `quantity` is incremented - it reads the pre-update quantity.
    session.execute(
        text(
            """
            INSERT INTO Holdings (user_id, stock_id, quantity, avg_cost, first_bought_at)
            VALUES (:uid, :sid, :qty, :price, NOW())
            ON DUPLICATE KEY UPDATE
                avg_cost = ((Holdings.quantity * Holdings.avg_cost) + :gross)
                           / (Holdings.quantity + :qty),
                quantity = Holdings.quantity + :qty,
                first_bought_at = COALESCE(Holdings.first_bought_at, NOW())
            """
        ),
        {"uid": user_id, "sid": stock_id, "qty": qty, "price": price, "gross": gross},
    )

    txn_id = _insert_ledger_row(
        session,
        user_id=user_id, stock_id=stock_id, txn_type="BUY",
        quantity=qty, price=price, fees=fees,
        gross=gross, net=total_cost, realized=None, notes=notes,
    )

    return {
        "txn_id": txn_id,
        "quantity": float(qty),
        "price": float(price),
        "gross_amount": float(gross),
        "fees": float(fees),
        "net_amount": float(total_cost),
        "cash_after": float(cash - total_cost),
        "realized_pnl": None,
        "message": f"Bought {qty:g} shares at {price:,.2f} (total {total_cost:,.2f}).",
    }


def _apply_sell(
    session: Session,
    *,
    user_id: int,
    stock_id: int,
    cash: Decimal,
    held_qty: Decimal,
    avg_cost: Decimal,
    qty: Decimal,
    price: Decimal,
    gross: Decimal,
    fees: Decimal,
    notes: Optional[str],
) -> Dict[str, Any]:
    """
    Credit net proceeds, reduce the position, book realised P&L.

    Cost basis is weighted-average (not FIFO), so avg_cost is UNCHANGED by a
    sell - only the quantity falls. Realised P&L on the sold slice is
    (sale price - avg cost) * qty - fees.
    """
    if qty > held_qty:
        raise InsufficientSharesError(
            f"Insufficient shares: you hold {held_qty:g} but tried to sell {qty:g}. "
            "Short selling is not supported."
        )

    proceeds = gross - fees
    realized = ((price - avg_cost) * qty - fees).quantize(MONEY, rounding=ROUND_HALF_UP)

    session.execute(
        text("UPDATE Users SET cash_balance = cash_balance + :amt WHERE user_id = :uid"),
        {"amt": proceeds, "uid": user_id},
    )
    session.execute(
        text(
            """
            UPDATE Holdings
            SET quantity     = quantity - :qty,
                realized_pnl = realized_pnl + :realized
            WHERE user_id = :uid AND stock_id = :sid
            """
        ),
        {"qty": qty, "realized": realized, "uid": user_id, "sid": stock_id},
    )

    txn_id = _insert_ledger_row(
        session,
        user_id=user_id, stock_id=stock_id, txn_type="SELL",
        quantity=qty, price=price, fees=fees,
        gross=gross, net=proceeds, realized=realized, notes=notes,
    )

    return {
        "txn_id": txn_id,
        "quantity": float(qty),
        "price": float(price),
        "gross_amount": float(gross),
        "fees": float(fees),
        "net_amount": float(proceeds),
        "cash_after": float(cash + proceeds),
        "realized_pnl": float(realized),
        "message": (
            f"Sold {qty:g} shares at {price:,.2f} "
            f"(proceeds {proceeds:,.2f}, realised P&L {realized:,.2f})."
        ),
    }


# ===========================================================================
#  Cash management
# ===========================================================================

def deposit_cash(user_id: int, amount: float | str | Decimal) -> Dict[str, Any]:
    """
    Add capital to the account.

    Both `cash_balance` and `total_deposits` move, which keeps ROI honest: a
    deposit increases the portfolio's value AND its cost base, so it does not
    masquerade as a gain.
    """
    amt = _to_decimal(amount, MONEY, "amount")
    if amt <= 0:
        raise InvalidOrderError("Deposit amount must be greater than zero.")

    with session_scope() as session:
        cash = _lock_user(session, user_id)
        session.execute(
            text(
                """
                UPDATE Users
                SET cash_balance   = cash_balance   + :amt,
                    total_deposits = total_deposits + :amt
                WHERE user_id = :uid
                """
            ),
            {"amt": amt, "uid": user_id},
        )
    return {"cash_after": float(cash + amt), "message": f"Deposited {amt:,.2f}."}


def withdraw_cash(user_id: int, amount: float | str | Decimal) -> Dict[str, Any]:
    """Withdraw settled cash, reducing both the balance and the ROI cost base."""
    amt = _to_decimal(amount, MONEY, "amount")
    if amt <= 0:
        raise InvalidOrderError("Withdrawal amount must be greater than zero.")

    with session_scope() as session:
        cash = _lock_user(session, user_id)
        if amt > cash:
            raise InsufficientFundsError(
                f"Cannot withdraw {amt:,.2f}; only {cash:,.2f} is available."
            )
        session.execute(
            text(
                """
                UPDATE Users
                SET cash_balance   = cash_balance - :amt,
                    total_deposits = GREATEST(total_deposits - :amt, 0)
                WHERE user_id = :uid
                """
            ),
            {"amt": amt, "uid": user_id},
        )
    return {"cash_after": float(cash - amt), "message": f"Withdrew {amt:,.2f}."}
