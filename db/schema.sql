-- =====================================================================
-- NSE Investment Copilot — Phase 1 schema (PostgreSQL 15+: uses UNIQUE NULLS NOT DISTINCT)
-- Idempotent: safe to run repeatedly (`nse-agent init-db`).
-- Money values are KES unless a currency column says otherwise.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sectors (
    sector_code  TEXT PRIMARY KEY,           -- e.g. 'BANKING'
    name         TEXT NOT NULL UNIQUE        -- e.g. 'Banking'
);

CREATE TABLE IF NOT EXISTS companies (
    ticker              TEXT PRIMARY KEY,                 -- NSE trading code, e.g. 'SCOM'
    name                TEXT NOT NULL,
    sector_code         TEXT REFERENCES sectors(sector_code),
    market_segment      TEXT CHECK (market_segment IN ('MIMS','AIMS','GEMS','REIT','ETF','PREF')),
    security_type       TEXT NOT NULL DEFAULT 'equity'
                        CHECK (security_type IN ('equity','preference','reit','etf')),
    isin                TEXT UNIQUE,
    shares_outstanding  BIGINT CHECK (shares_outstanding IS NULL OR shares_outstanding > 0),
    website             TEXT,
    -- Phrases used to link news articles to this company. ALL-CAPS aliases
    -- are matched case-sensitively (so 'KCB' doesn't match 'kcb' in a URL),
    -- everything else case-insensitively, always on word boundaries.
    aliases             TEXT[] NOT NULL DEFAULT '{}',
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,    -- FALSE = suspended/delisted
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_companies_sector ON companies(sector_code);

-- ---------------------------------------------------------------------
-- Market data
-- ---------------------------------------------------------------------
-- One row per security per trading day. Sources differ in what they give
-- (a snapshot gives close+volume only), so OHLC columns are nullable.
CREATE TABLE IF NOT EXISTS daily_prices (
    ticker       TEXT NOT NULL REFERENCES companies(ticker) ON UPDATE CASCADE,
    trade_date   DATE NOT NULL,
    open         NUMERIC(14,4) CHECK (open  IS NULL OR open  >= 0),
    high         NUMERIC(14,4) CHECK (high  IS NULL OR high  >= 0),
    low          NUMERIC(14,4) CHECK (low   IS NULL OR low   >= 0),
    close        NUMERIC(14,4) NOT NULL CHECK (close >= 0),
    prev_close   NUMERIC(14,4) CHECK (prev_close IS NULL OR prev_close >= 0),
    volume       BIGINT CHECK (volume IS NULL OR volume >= 0),
    turnover     NUMERIC(20,2),                       -- KES value traded
    vwap         NUMERIC(14,4),
    source       TEXT NOT NULL,                       -- which adapter wrote it
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, trade_date),
    CHECK (high IS NULL OR low IS NULL OR high >= low)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON daily_prices(trade_date);

CREATE TABLE IF NOT EXISTS market_indices (
    index_code   TEXT PRIMARY KEY,                    -- 'NASI', 'NSE20', 'NSE25', 'NSE10'
    name         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_values (
    index_code   TEXT NOT NULL REFERENCES market_indices(index_code),
    trade_date   DATE NOT NULL,
    value        NUMERIC(14,4) NOT NULL,
    source       TEXT NOT NULL,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (index_code, trade_date)
);

-- ---------------------------------------------------------------------
-- Corporate actions & fundamentals
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corporate_actions (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL REFERENCES companies(ticker) ON UPDATE CASCADE,
    action_type         TEXT NOT NULL CHECK (action_type IN
                        ('interim_dividend','final_dividend','special_dividend',
                         'bonus_issue','rights_issue','stock_split','suspension','other')),
    announced_date      DATE,
    book_closure_date   DATE,
    payment_date        DATE,
    amount_per_share    NUMERIC(14,4),               -- cash dividends (KES)
    ratio_new           INTEGER,                     -- bonus/split/rights: ratio_new for ratio_old
    ratio_old           INTEGER,
    financial_year      INTEGER,
    notes               TEXT,
    source_url          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (ticker, action_type, book_closure_date, financial_year)
);
CREATE INDEX IF NOT EXISTS idx_actions_ticker ON corporate_actions(ticker, book_closure_date DESC);

-- Headline numbers from annual/interim reports. Phase 1 fills this by CSV
-- import; a later phase extracts it from the PDFs.
CREATE TABLE IF NOT EXISTS financial_statements (
    ticker              TEXT NOT NULL REFERENCES companies(ticker) ON UPDATE CASCADE,
    period_end          DATE NOT NULL,
    period_type         TEXT NOT NULL CHECK (period_type IN ('FY','H1','Q1','Q3')),
    currency            TEXT NOT NULL DEFAULT 'KES',
    revenue             NUMERIC(20,2),
    operating_profit    NUMERIC(20,2),
    profit_before_tax   NUMERIC(20,2),
    net_income          NUMERIC(20,2),               -- attributable to shareholders
    total_assets        NUMERIC(20,2),
    total_liabilities   NUMERIC(20,2),
    total_equity        NUMERIC(20,2),
    operating_cash_flow NUMERIC(20,2),
    eps                 NUMERIC(14,4),
    dps                 NUMERIC(14,4),               -- total declared for the period
    shares_outstanding  BIGINT,
    source_url          TEXT,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, period_end, period_type)
);

-- ---------------------------------------------------------------------
-- News
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS news_sources (
    source_code  TEXT PRIMARY KEY,                   -- 'capitalfm_business'
    name         TEXT NOT NULL,
    feed_url     TEXT NOT NULL,
    scope        TEXT NOT NULL CHECK (scope IN ('local','regional','global','company')),
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    last_ok_at   TIMESTAMPTZ,
    last_error   TEXT
);

CREATE TABLE IF NOT EXISTS news_articles (
    id            BIGSERIAL PRIMARY KEY,
    source_code   TEXT NOT NULL REFERENCES news_sources(source_code),
    url           TEXT NOT NULL,
    url_hash      TEXT NOT NULL UNIQUE,              -- sha256 of normalised URL (dedupe)
    title         TEXT NOT NULL,
    summary       TEXT,
    content       TEXT,                              -- plain text, HTML stripped
    author        TEXT,
    categories    TEXT[] NOT NULL DEFAULT '{}',      -- as given by the feed
    topics        TEXT[] NOT NULL DEFAULT '{}',      -- our macro topics: interest_rates, fx, ...
    published_at  TIMESTAMPTZ,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_topics ON news_articles USING GIN(topics);

CREATE TABLE IF NOT EXISTS news_ticker_links (
    article_id    BIGINT NOT NULL REFERENCES news_articles(id) ON DELETE CASCADE,
    ticker        TEXT NOT NULL REFERENCES companies(ticker) ON UPDATE CASCADE,
    matched_alias TEXT NOT NULL,
    in_title      BOOLEAN NOT NULL,
    mentions      INTEGER NOT NULL CHECK (mentions > 0),
    relevance     NUMERIC(3,2) NOT NULL CHECK (relevance BETWEEN 0 AND 1),
    PRIMARY KEY (article_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_newslinks_ticker ON news_ticker_links(ticker);

-- ---------------------------------------------------------------------
-- Macro (CBK rate, T-bill yields, USD/KES, inflation, oil, ...)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS macro_series (
    series_code  TEXT PRIMARY KEY,                   -- 'CBK_CBR', 'TBILL_91', 'USDKES'
    name         TEXT NOT NULL,
    unit         TEXT NOT NULL,                      -- 'percent', 'KES', 'USD/bbl'
    frequency    TEXT NOT NULL CHECK (frequency IN ('daily','weekly','monthly','quarterly','event')),
    source       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS macro_observations (
    series_code  TEXT NOT NULL REFERENCES macro_series(series_code),
    obs_date     DATE NOT NULL,
    value        NUMERIC(18,6) NOT NULL,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (series_code, obs_date)
);

-- ---------------------------------------------------------------------
-- Pipeline bookkeeping
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id            BIGSERIAL PRIMARY KEY,
    job           TEXT NOT NULL,                     -- 'prices', 'news', 'import-prices', ...
    source        TEXT NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running','ok','partial','failed')),
    rows_seen     INTEGER NOT NULL DEFAULT 0,
    rows_written  INTEGER NOT NULL DEFAULT 0,
    warnings      TEXT[] NOT NULL DEFAULT '{}',
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_job ON ingestion_runs(job, started_at DESC);

-- ---------------------------------------------------------------------
-- Users & portfolios (tables now so Phase 2 has somewhere to write)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS investor_profiles (
    id                  BIGSERIAL PRIMARY KEY,
    display_name        TEXT NOT NULL,
    risk_tolerance      TEXT CHECK (risk_tolerance IN ('conservative','moderate','aggressive')),
    horizon_years       INTEGER CHECK (horizon_years IS NULL OR horizon_years > 0),
    monthly_investable  NUMERIC(14,2),
    has_emergency_fund  BOOLEAN,
    goals               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS transactions (
    id            BIGSERIAL PRIMARY KEY,
    profile_id    BIGINT NOT NULL REFERENCES investor_profiles(id) ON DELETE CASCADE,
    ticker        TEXT NOT NULL REFERENCES companies(ticker) ON UPDATE CASCADE,
    side          TEXT NOT NULL CHECK (side IN ('buy','sell','dividend','bonus')),
    trade_date    DATE NOT NULL,
    quantity      NUMERIC(18,4) NOT NULL CHECK (quantity >= 0),
    price         NUMERIC(14,4) NOT NULL CHECK (price >= 0),
    fees          NUMERIC(14,2) NOT NULL DEFAULT 0,
    notes         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tx_profile ON transactions(profile_id, trade_date);

-- ---------------------------------------------------------------------
-- Phase 2: financial profile, plan, non-equity holdings, alerts
-- ALTER ... IF NOT EXISTS so databases created in Phase 1 upgrade in place.
-- ---------------------------------------------------------------------
ALTER TABLE investor_profiles
    ADD COLUMN IF NOT EXISTS monthly_income          NUMERIC(14,2) CHECK (monthly_income IS NULL OR monthly_income >= 0),
    ADD COLUMN IF NOT EXISTS monthly_expenses        NUMERIC(14,2) CHECK (monthly_expenses IS NULL OR monthly_expenses >= 0),
    ADD COLUMN IF NOT EXISTS high_interest_debt      NUMERIC(14,2) NOT NULL DEFAULT 0,  -- mobile loans, cards, >~15% p.a.
    ADD COLUMN IF NOT EXISTS emergency_months_target INTEGER NOT NULL DEFAULT 6,
    ADD COLUMN IF NOT EXISTS risk_score              INTEGER CHECK (risk_score IS NULL OR risk_score BETWEEN 0 AND 100),
    ADD COLUMN IF NOT EXISTS questionnaire           JSONB,   -- raw answers, so the score is auditable
    ADD COLUMN IF NOT EXISTS max_stock_pct           NUMERIC(5,2) NOT NULL DEFAULT 20,  -- of the equity sleeve
    ADD COLUMN IF NOT EXISTS max_sector_pct          NUMERIC(5,2) NOT NULL DEFAULT 40,
    ADD COLUMN IF NOT EXISTS updated_at              TIMESTAMPTZ NOT NULL DEFAULT now();

-- Withholding tax on dividends (5% for Kenyan residents), kept apart from
-- trading fees so income can be reported gross and net.
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS tax NUMERIC(14,2) NOT NULL DEFAULT 0;

-- Money outside NSE shares: emergency cash, MMFs, T-bills/bonds, SACCO.
-- Needed to judge allocation across the whole portfolio, not just stocks.
-- The emergency fund is the sum of 'emergency_cash' rows (single source of truth).
CREATE TABLE IF NOT EXISTS other_holdings (
    id                  BIGSERIAL PRIMARY KEY,
    profile_id          BIGINT NOT NULL REFERENCES investor_profiles(id) ON DELETE CASCADE,
    asset_class         TEXT NOT NULL CHECK (asset_class IN
                        ('emergency_cash','money_market','government_securities','sacco','other')),
    name                TEXT NOT NULL,                     -- e.g. 'CIC MMF', '364-day T-bill'
    amount              NUMERIC(16,2) NOT NULL CHECK (amount >= 0),
    expected_yield_pct  NUMERIC(6,3),
    maturity_date       DATE,
    as_of               DATE NOT NULL DEFAULT CURRENT_DATE,
    UNIQUE (profile_id, asset_class, name)
);

-- The latest plan produced by `nse-agent plan`, used for drift alerts.
CREATE TABLE IF NOT EXISTS allocation_targets (
    profile_id   BIGINT NOT NULL REFERENCES investor_profiles(id) ON DELETE CASCADE,
    asset_class  TEXT NOT NULL CHECK (asset_class IN
                 ('emergency_cash','money_market','government_securities','nse_equities')),
    target_pct   NUMERIC(5,2) NOT NULL CHECK (target_pct BETWEEN 0 AND 100),
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_id, asset_class)
);

CREATE TABLE IF NOT EXISTS watchlist (
    profile_id  BIGINT NOT NULL REFERENCES investor_profiles(id) ON DELETE CASCADE,
    ticker      TEXT NOT NULL REFERENCES companies(ticker) ON UPDATE CASCADE,
    note        TEXT,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_id, ticker)
);

CREATE TABLE IF NOT EXISTS price_alert_rules (
    id          BIGSERIAL PRIMARY KEY,
    profile_id  BIGINT NOT NULL REFERENCES investor_profiles(id) ON DELETE CASCADE,
    ticker      TEXT NOT NULL REFERENCES companies(ticker) ON UPDATE CASCADE,
    condition   TEXT NOT NULL CHECK (condition IN ('above','below','move_pct')),
    threshold   NUMERIC(14,4) NOT NULL CHECK (threshold > 0),
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (profile_id, ticker, condition, threshold)
);

CREATE TABLE IF NOT EXISTS alerts (
    id               BIGSERIAL PRIMARY KEY,
    profile_id       BIGINT NOT NULL REFERENCES investor_profiles(id) ON DELETE CASCADE,
    alert_type       TEXT NOT NULL,          -- price_rule, big_move, 52w_high, book_closure, ...
    severity         TEXT NOT NULL CHECK (severity IN ('info','warning','action')),
    ticker           TEXT REFERENCES companies(ticker) ON UPDATE CASCADE,
    message          TEXT NOT NULL,
    details          JSONB NOT NULL DEFAULT '{}',
    dedupe_key       TEXT NOT NULL,          -- same event is only raised once
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_at  TIMESTAMPTZ,
    UNIQUE (profile_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts(profile_id, created_at DESC) WHERE acknowledged_at IS NULL;

-- ---------------------------------------------------------------------
-- Views the agent's tools will query
-- ---------------------------------------------------------------------

-- Latest close per ticker plus change vs previous close and 52-week range.
CREATE OR REPLACE VIEW v_latest_prices AS
WITH ranked AS (
    SELECT p.*,
           row_number() OVER (PARTITION BY ticker ORDER BY trade_date DESC) AS rn,
           lag(close)   OVER (PARTITION BY ticker ORDER BY trade_date)      AS lag_close
    FROM daily_prices p
),
yr AS (
    SELECT ticker, max(close) AS high_52w, min(close) AS low_52w,
           avg(volume)::BIGINT AS avg_volume_52w,
           count(*) AS trading_days_52w
    FROM daily_prices
    WHERE trade_date > (SELECT max(trade_date) FROM daily_prices) - INTERVAL '365 days'
    GROUP BY ticker
)
SELECT c.ticker, c.name, c.sector_code,
       r.trade_date, r.close, r.volume,
       COALESCE(r.prev_close, r.lag_close)                             AS prev_close,
       r.close - COALESCE(r.prev_close, r.lag_close)                   AS change,
       ROUND(100 * (r.close - COALESCE(r.prev_close, r.lag_close))
             / NULLIF(COALESCE(r.prev_close, r.lag_close), 0), 2)      AS change_pct,
       yr.high_52w, yr.low_52w, yr.avg_volume_52w, yr.trading_days_52w,
       c.shares_outstanding,
       r.close * c.shares_outstanding                                  AS market_cap
FROM companies c
JOIN ranked r ON r.ticker = c.ticker AND r.rn = 1
LEFT JOIN yr   ON yr.ticker = c.ticker;

-- Valuation snapshot: trailing P/E and dividend yield from the latest FY.
CREATE OR REPLACE VIEW v_valuation AS
WITH fy AS (
    SELECT DISTINCT ON (ticker) *
    FROM financial_statements
    WHERE period_type = 'FY'
    ORDER BY ticker, period_end DESC
)
SELECT lp.ticker, lp.name, lp.sector_code, lp.trade_date, lp.close,
       fy.period_end AS fy_period_end, fy.eps, fy.dps,
       ROUND(lp.close / NULLIF(fy.eps, 0), 2)                          AS pe_ratio,
       ROUND(100 * fy.dps / NULLIF(lp.close, 0), 2)                    AS dividend_yield_pct,
       ROUND(100 * fy.net_income / NULLIF(fy.total_equity, 0), 2)      AS roe_pct,
       ROUND(fy.dps / NULLIF(fy.eps, 0), 2)                            AS payout_ratio
FROM v_latest_prices lp
LEFT JOIN fy ON fy.ticker = lp.ticker;

-- Upcoming dividend book closures (what a user may want alerts for).
CREATE OR REPLACE VIEW v_upcoming_dividends AS
SELECT ca.ticker, c.name, ca.action_type, ca.amount_per_share,
       ca.book_closure_date, ca.payment_date,
       ROUND(100 * ca.amount_per_share / NULLIF(lp.close, 0), 2) AS yield_on_price_pct
FROM corporate_actions ca
JOIN companies c USING (ticker)
LEFT JOIN v_latest_prices lp USING (ticker)
WHERE ca.action_type LIKE '%dividend'
  AND ca.book_closure_date >= CURRENT_DATE;

-- Recent news per ticker, most relevant first.
CREATE OR REPLACE VIEW v_ticker_news AS
SELECT l.ticker, a.id AS article_id, a.title, a.url, a.source_code,
       a.published_at, l.relevance, l.in_title, a.topics
FROM news_ticker_links l
JOIN news_articles a ON a.id = l.article_id;

-- Data freshness per job: the first thing to check when answers look off.
CREATE OR REPLACE VIEW v_pipeline_health AS
SELECT DISTINCT ON (job, source)
       job, source, status, started_at, finished_at,
       rows_seen, rows_written, warnings, error
FROM ingestion_runs
ORDER BY job, source, started_at DESC;
