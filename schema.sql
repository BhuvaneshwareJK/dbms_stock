-- ===========================================================================
--  Real-Time Stock Portfolio Management System
--  Database schema (MySQL 8.0+)
--
--  Design notes
--  ------------
--  * Every table uses the InnoDB engine, which is what actually gives us the
--    ACID guarantees the service layer relies on (row-level locking,
--    transactional DDL-safe DML, crash recovery via the redo log).
--  * All monetary columns are DECIMAL, never FLOAT/DOUBLE. Binary floating
--    point cannot represent 0.10 exactly and will silently corrupt balances.
--  * CHECK constraints require MySQL 8.0.16 or newer. On older servers they
--    are parsed and ignored, so the service layer re-validates everything in
--    Python as well (defence in depth).
--  * Run with:  mysql -u root -p < schema.sql
-- ===========================================================================


-- Drop in reverse dependency order so foreign keys never block us.
DROP VIEW  IF EXISTS v_user_positions;
DROP TABLE IF EXISTS Transactions;
DROP TABLE IF EXISTS Holdings;
DROP TABLE IF EXISTS Stocks;
DROP TABLE IF EXISTS Users;


-- ---------------------------------------------------------------------------
-- 1. Users : account holders and their settled cash balance
-- ---------------------------------------------------------------------------
CREATE TABLE Users (
    user_id         INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    username        VARCHAR(50)     NOT NULL,
    full_name       VARCHAR(120)    NOT NULL,
    email           VARCHAR(120)    NOT NULL,
    -- Settled buying power. Debited on BUY, credited on SELL.
    cash_balance    DECIMAL(18, 2)  NOT NULL DEFAULT 0.00,
    -- Lifetime capital contributed by the user. This is the denominator of the
    -- ROI calculation, so it must only change on deposits/withdrawals, never
    -- on trades.
    total_deposits  DECIMAL(18, 2)  NOT NULL DEFAULT 0.00,
    base_currency   CHAR(3)         NOT NULL DEFAULT 'INR',
    created_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP
                                    ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (user_id),
    UNIQUE KEY uq_users_username (username),
    UNIQUE KEY uq_users_email    (email),

    -- An account can never go overdrawn; the BUY path checks this too.
    CONSTRAINT chk_users_cash_non_negative     CHECK (cash_balance   >= 0),
    CONSTRAINT chk_users_deposits_non_negative CHECK (total_deposits >= 0)
) ENGINE = InnoDB;


-- ---------------------------------------------------------------------------
-- 2. Stocks : the instrument master / reference-data dimension table
--    Ticker format follows Yahoo Finance conventions:
--      RELIANCE.NS  -> NSE India
--      TCS.BO       -> BSE India
--      AAPL         -> NASDAQ
-- ---------------------------------------------------------------------------
CREATE TABLE Stocks (
    stock_id      INT UNSIGNED   NOT NULL AUTO_INCREMENT,
    ticker        VARCHAR(20)    NOT NULL,
    company_name  VARCHAR(150)   NOT NULL,
    sector        VARCHAR(80)    NULL DEFAULT 'Unclassified',
    industry      VARCHAR(120)   NULL,
    exchange      VARCHAR(40)    NULL,
    currency      CHAR(3)        NOT NULL DEFAULT 'INR',
    is_active     TINYINT(1)     NOT NULL DEFAULT 1,
    created_at    TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (stock_id),
    UNIQUE KEY uq_stocks_ticker (ticker),
    KEY idx_stocks_sector (sector)
) ENGINE = InnoDB;


-- ---------------------------------------------------------------------------
-- 3. Holdings : current position per (user, stock). A materialised aggregate
--    of Transactions, kept in sync inside the same DB transaction as the
--    trade itself so the two can never disagree.
-- ---------------------------------------------------------------------------
CREATE TABLE Holdings (
    holding_id      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id         INT UNSIGNED    NOT NULL,
    stock_id        INT UNSIGNED    NOT NULL,
    quantity        DECIMAL(18, 4)  NOT NULL DEFAULT 0,
    -- Weighted-average cost basis per share, recomputed on every BUY.
    avg_cost        DECIMAL(18, 4)  NOT NULL DEFAULT 0,
    -- Cumulative booked profit/loss from SELLs on this position.
    realized_pnl    DECIMAL(18, 2)  NOT NULL DEFAULT 0.00,
    first_bought_at DATETIME        NULL,
    updated_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP
                                    ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (holding_id),
    -- The UNIQUE key is what makes the INSERT ... ON DUPLICATE KEY UPDATE
    -- upsert in transaction_service atomic.
    UNIQUE KEY uq_holdings_user_stock (user_id, stock_id),
    KEY idx_holdings_user (user_id),

    CONSTRAINT fk_holdings_user
        FOREIGN KEY (user_id)  REFERENCES Users (user_id)   ON DELETE CASCADE,
    CONSTRAINT fk_holdings_stock
        FOREIGN KEY (stock_id) REFERENCES Stocks (stock_id) ON DELETE RESTRICT,

    -- You cannot hold a negative number of shares (no short selling here).
    CONSTRAINT chk_holdings_qty_non_negative CHECK (quantity >= 0)
) ENGINE = InnoDB;


-- ---------------------------------------------------------------------------
-- 4. Transactions : the immutable, append-only ledger.
--    This is the single source of truth for the analytics tier - every
--    time-series in analytics_service is rebuilt from these rows.
-- ---------------------------------------------------------------------------
CREATE TABLE Transactions (
    txn_id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id         INT UNSIGNED    NOT NULL,
    stock_id        INT UNSIGNED    NOT NULL,
    txn_type        ENUM('BUY', 'SELL') NOT NULL,
    quantity        DECIMAL(18, 4)  NOT NULL,
    price_per_share DECIMAL(18, 4)  NOT NULL,
    fees            DECIMAL(18, 2)  NOT NULL DEFAULT 0.00,
    -- gross = quantity * price ; net = gross +/- fees (the actual cash moved)
    gross_amount    DECIMAL(18, 2)  NOT NULL,
    net_amount      DECIMAL(18, 2)  NOT NULL,
    -- Populated on SELL only: (sale price - avg cost) * qty - fees
    realized_pnl    DECIMAL(18, 2)  NULL,
    executed_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notes           VARCHAR(255)    NULL,

    PRIMARY KEY (txn_id),
    -- Composite index: every analytics query filters by user and orders by time.
    KEY idx_txn_user_time  (user_id, executed_at),
    KEY idx_txn_stock_time (stock_id, executed_at),

    CONSTRAINT fk_txn_user
        FOREIGN KEY (user_id)  REFERENCES Users (user_id)   ON DELETE CASCADE,
    CONSTRAINT fk_txn_stock
        FOREIGN KEY (stock_id) REFERENCES Stocks (stock_id) ON DELETE RESTRICT,

    CONSTRAINT chk_txn_qty_positive   CHECK (quantity        >  0),
    CONSTRAINT chk_txn_price_positive CHECK (price_per_share >= 0)
) ENGINE = InnoDB;


-- ---------------------------------------------------------------------------
-- 5. Reporting view: flattens Holdings + Stocks so read-only BI queries never
--    have to re-write the join.
-- ---------------------------------------------------------------------------
CREATE VIEW v_user_positions AS
SELECT
    h.user_id,
    u.username,
    s.stock_id,
    s.ticker,
    s.company_name,
    s.sector,
    s.currency,
    h.quantity,
    h.avg_cost,
    (h.quantity * h.avg_cost) AS invested_value,
    h.realized_pnl,
    h.first_bought_at,
    h.updated_at
FROM Holdings h
JOIN Users  u ON u.user_id  = h.user_id
JOIN Stocks s ON s.stock_id = h.stock_id
WHERE h.quantity > 0;


-- ===========================================================================
--  SEED DATA - a demo user plus a reference-data starter set.
--  The application creates any missing ticker on first trade, so this list
--  only exists to make the dashboard useful on a cold start.
-- ===========================================================================

INSERT INTO Users (username, full_name, email, cash_balance, total_deposits, base_currency)
VALUES
    ('nirmal', 'Nirmal M',   'nirmal@example.com', 500000.00, 500000.00, 'INR'),
    ('demo',   'Demo Trader', 'demo@example.com',  250000.00, 250000.00, 'INR');

INSERT INTO Stocks (ticker, company_name, sector, industry, exchange, currency) VALUES
    ('RELIANCE.NS', 'Reliance Industries Ltd',       'Energy',             'Oil & Gas Refining',   'NSE',    'INR'),
    ('TCS.NS',      'Tata Consultancy Services Ltd', 'Technology',         'IT Services',          'NSE',    'INR'),
    ('INFY.NS',     'Infosys Ltd',                   'Technology',         'IT Services',          'NSE',    'INR'),
    ('HDFCBANK.NS', 'HDFC Bank Ltd',                 'Financial Services', 'Banks - Regional',     'NSE',    'INR'),
    ('ICICIBANK.NS','ICICI Bank Ltd',                'Financial Services', 'Banks - Regional',     'NSE',    'INR'),
    ('TATAMOTORS.NS','Tata Motors Ltd',              'Consumer Cyclical',  'Auto Manufacturers',   'NSE',    'INR'),
    ('SUNPHARMA.NS','Sun Pharmaceutical Industries', 'Healthcare',         'Drug Manufacturers',   'NSE',    'INR'),
    ('ITC.NS',      'ITC Ltd',                       'Consumer Defensive', 'Tobacco',              'NSE',    'INR'),
    ('AAPL',        'Apple Inc.',                    'Technology',         'Consumer Electronics', 'NASDAQ', 'USD'),
    ('MSFT',        'Microsoft Corporation',         'Technology',         'Software - Infra',     'NASDAQ', 'USD');

-- Sanity check after loading:
--   SELECT COUNT(*) FROM Stocks;   -- expect 10
--   SHOW TABLE STATUS WHERE Engine <> 'InnoDB';  -- expect 0 rows
