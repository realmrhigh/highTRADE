CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    notes TEXT
);
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE market_crises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            event_type TEXT NOT NULL,
            trigger_description TEXT,
            drawdown_percent REAL,
            recovery_days INTEGER,
            signals JSON,
            resolution_catalyst TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
CREATE TABLE market_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            signal_type TEXT NOT NULL,
            confidence REAL,
            context JSON,
            defcon_level INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
CREATE TABLE signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER,
            crisis_id INTEGER,
            lead_time_days INTEGER,
            accuracy REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (signal_id) REFERENCES market_signals(id),
            FOREIGN KEY (crisis_id) REFERENCES market_crises(id)
        );
CREATE INDEX idx_crises_date
        ON market_crises(date DESC)
    ;
CREATE INDEX idx_crises_event_type
        ON market_crises(event_type)
    ;
CREATE INDEX idx_signals_timestamp
        ON market_signals(timestamp DESC)
    ;
CREATE INDEX idx_signals_type
        ON market_signals(signal_type)
    ;
CREATE INDEX idx_signals_defcon
        ON market_signals(defcon_level)
    ;
CREATE INDEX idx_crises_date_type
        ON market_crises(date DESC, event_type)
    ;
CREATE TABLE signals (
        signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
        crisis_id INTEGER NOT NULL,
        signal_type TEXT CHECK(signal_type IN (
            'bond_yield_spike',
            'official_denial',
            'tone_shift',
            'elite_pushback',
            'rally_despite_news',
            'retaliation_pause',
            'vix_divergence',
            'put_call_spike',
            'market_breadth_extreme',
            'policy_signal'
        )),
        signal_weight REAL NOT NULL,
        detected_date TEXT,
        detected_time TEXT,
        value TEXT,
        description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (crisis_id) REFERENCES crisis_events(crisis_id)
    );
CREATE TABLE market_data (
        data_id INTEGER PRIMARY KEY AUTOINCREMENT,
        crisis_id INTEGER NOT NULL,
        data_date TEXT NOT NULL,
        sp500_price REAL,
        sp500_change_percent REAL,
        nasdaq_price REAL,
        nasdaq_change_percent REAL,
        vix_close REAL,
        bond_10yr_yield REAL,
        bond_yield_change_bps INTEGER,
        put_call_ratio REAL,
        market_breadth_percent REAL,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (crisis_id) REFERENCES crisis_events(crisis_id),
        UNIQUE(crisis_id, data_date)
    );
CREATE TABLE signal_monitoring (
        monitor_id INTEGER PRIMARY KEY AUTOINCREMENT,
        monitoring_date TEXT NOT NULL,
        monitoring_time TEXT NOT NULL,
        bond_10yr_yield REAL,
        bond_yield_5day_change_bps INTEGER,
        vix_close REAL,
        vix_5day_high REAL,
        news_sentiment_score REAL CHECK(news_sentiment_score >= -1 AND news_sentiment_score <= 1),
        official_denials_count INTEGER DEFAULT 0,
        defcon_level INTEGER CHECK(defcon_level IN (5, 4, 3, 2, 1)),
        signal_score REAL CHECK(signal_score >= 0 AND signal_score <= 100),
        key_articles TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, news_score REAL DEFAULT 0, composite_signal_score REAL, macro_defcon_modifier REAL DEFAULT 0,
        UNIQUE(monitoring_date, monitoring_time)
    );
CREATE TABLE defcon_history (
        defcon_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_date TEXT NOT NULL,
        event_time TEXT NOT NULL,
        defcon_level INTEGER CHECK(defcon_level IN (5, 4, 3, 2, 1)) NOT NULL,
        reason TEXT,
        contributing_signals TEXT,
        signal_score REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    , claude_influenced BOOLEAN DEFAULT 0, claude_analysis_id INTEGER);
CREATE TABLE signal_rules (
        rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_type TEXT UNIQUE NOT NULL,
        base_weight REAL NOT NULL,
        description TEXT,
        defcon_4_threshold REAL,
        defcon_3_threshold REAL,
        defcon_2_threshold REAL,
        defcon_1_threshold REAL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
CREATE TABLE news_signals (
        news_signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        news_score REAL,
        dominant_crisis_type TEXT,
        crisis_description TEXT,
        breaking_news_override BOOLEAN,
        recommended_defcon INTEGER,
        article_count INTEGER,
        breaking_count INTEGER,
        avg_confidence REAL,
        sentiment_summary TEXT,
        articles_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    , sentiment_net_score REAL, signal_concentration REAL, crisis_distribution_json TEXT, score_components_json TEXT, keyword_hits_json TEXT, baseline_deviation REAL, articles_full_json TEXT, gemini_flash_json TEXT, congressional_signal_score REAL DEFAULT 50, macro_score REAL DEFAULT 50, macro_defcon_modifier REAL DEFAULT 0);
CREATE TABLE claude_analysis (
                analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_signal_id INTEGER,
                enhanced_confidence REAL,
                sentiment_override TEXT,
                risk_factors TEXT,
                opportunity_score REAL,
                reasoning TEXT,
                sources_verified INTEGER,
                narrative_coherence REAL,
                recommended_action TEXT,
                confidence_adjustment REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (news_signal_id) REFERENCES news_signals(news_signal_id)
            );
CREATE INDEX idx_claude_news_signal 
            ON claude_analysis(news_signal_id)
        ;
CREATE INDEX idx_claude_timestamp 
            ON claude_analysis(created_at DESC)
        ;
CREATE TABLE IF NOT EXISTS "trade_records" (
        trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
        crisis_id INTEGER,
        entry_date TEXT NOT NULL,
        entry_time TEXT,
        entry_price REAL NOT NULL,
        entry_signal_score REAL,
        defcon_at_entry INTEGER,
        shares INTEGER,
        position_size_dollars REAL,
        exit_date TEXT,
        exit_time TEXT,
        exit_price REAL,
        exit_reason TEXT CHECK(exit_reason IN ('profit_target', 'stop_loss', 'manual', 'invalidation')),
        profit_loss_dollars REAL,
        profit_loss_percent REAL,
        holding_hours INTEGER,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        asset_symbol TEXT,
        status TEXT DEFAULT 'closed', current_price REAL, unrealized_pnl_dollars REAL, unrealized_pnl_percent REAL, last_price_updated TIMESTAMP, stop_loss REAL, take_profit_1 REAL, take_profit_2 REAL, catalyst_event TEXT, catalyst_window_end TEXT, catalyst_spike_pct REAL, catalyst_failure_pct REAL, peak_price REAL,
        FOREIGN KEY (crisis_id) REFERENCES crisis_events(crisis_id)
    );
CREATE TABLE IF NOT EXISTS "crisis_events" (
        crisis_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        trigger TEXT,
        start_date TEXT NOT NULL,
        crisis_bottom_date TEXT,
        recovery_date TEXT,
        resolution_announcement_date TEXT,
        market_drop_percent REAL,
        recovery_percent REAL,
        recovery_days INTEGER,
        severity TEXT CHECK(severity IN ('minor', 'moderate', 'severe')),
        category TEXT CHECK(category IN ('trade', 'policy', 'geopolitical', 'financial', 'epidemic', 'signal')),
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
CREATE TABLE gemini_analysis (
    analysis_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    news_signal_id       INTEGER,
    model_used           TEXT,
    trigger_type         TEXT,
    narrative_coherence  REAL,
    hidden_risks         TEXT,
    contrarian_signals   TEXT,
    market_context       TEXT,
    confidence_in_signal REAL,
    recommended_action   TEXT,
    reasoning            TEXT,
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP, data_gaps_json TEXT,
    FOREIGN KEY (news_signal_id) REFERENCES news_signals (news_signal_id)
);
CREATE INDEX idx_gemini_analysis_news_signal_id
    ON gemini_analysis (news_signal_id);
CREATE INDEX idx_gemini_analysis_created_at
    ON gemini_analysis (created_at DESC);
CREATE TABLE congressional_trades (
            trade_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source            TEXT NOT NULL,           -- 'house' | 'senate' | 'sec_form4'
            politician        TEXT NOT NULL,
            party             TEXT,                    -- 'D' | 'R' | 'I'
            ticker            TEXT NOT NULL,
            direction         TEXT,                    -- 'buy' | 'sell' | 'unknown'
            amount            REAL,                    -- estimated USD midpoint
            disclosure_date   TEXT,                   -- YYYY-MM-DD
            transaction_date  TEXT,                   -- YYYY-MM-DD (when trade occurred)
            asset_description TEXT,
            district          TEXT,                    -- state or district
            committee_hint    TEXT,                   -- relevant committee if known
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(politician, ticker, direction, transaction_date)
        );
CREATE INDEX idx_cong_trades_ticker ON congressional_trades(ticker);
CREATE INDEX idx_cong_trades_date ON congressional_trades(disclosure_date);
CREATE INDEX idx_cong_trades_politician ON congressional_trades(politician);
CREATE TABLE congressional_cluster_signals (
            cluster_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker             TEXT NOT NULL,
            buy_count          INTEGER,
            politicians_json   TEXT,         -- JSON array of politician names
            total_amount       REAL,         -- sum of estimated trade amounts
            bipartisan         BOOLEAN,      -- True if both parties buying
            committee_relevance TEXT,        -- JSON array of relevant committees
            signal_strength    REAL,         -- 0-100 score
            window_days        INTEGER,      -- rolling window used
            created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
CREATE INDEX idx_cluster_ticker ON congressional_cluster_signals(ticker);
CREATE INDEX idx_cluster_created ON congressional_cluster_signals(created_at);
CREATE TABLE macro_indicators (
            indicator_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            yield_curve_spread REAL,         -- T10Y2Y spread in %
            fed_funds_rate     REAL,         -- FEDFUNDS %
            unemployment_rate  REAL,         -- UNRATE %
            m2_yoy_change      REAL,         -- M2 YoY % change
            hy_oas_bps         REAL,         -- HY OAS in basis points
            consumer_sentiment REAL,         -- UMCSENT index
            rate_10y           REAL,         -- DGS10 %
            rate_2y            REAL,         -- DGS2 %
            macro_score        REAL,         -- 0-100 composite score
            defcon_modifier    REAL,         -- DEFCON level adjustment
            bearish_signals    INTEGER,
            bullish_signals    INTEGER,
            signals_json       TEXT,         -- JSON array of signal objects
            created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
CREATE INDEX idx_macro_created ON macro_indicators(created_at);
CREATE TABLE daily_briefings (
            briefing_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL,
            model_key       TEXT NOT NULL,
            model_id        TEXT,
            market_regime   TEXT,
            regime_confidence REAL,
            headline_summary TEXT,
            key_themes_json TEXT,
            biggest_risk    TEXT,
            biggest_opportunity TEXT,
            signal_quality  TEXT,
            macro_alignment TEXT,
            congressional_alpha TEXT,
            portfolio_assessment TEXT,
            watchlist_json  TEXT,
            entry_conditions TEXT,
            defcon_forecast TEXT,
            reasoning_chain TEXT,
            model_confidence REAL,
            input_tokens    INTEGER,
            output_tokens   INTEGER,
            full_response_json TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP, data_gaps_json TEXT, trading_stance TEXT DEFAULT "NORMAL",
            UNIQUE(date, model_key)
        );
CREATE INDEX idx_briefing_date ON daily_briefings(date);
CREATE TABLE portfolio_snapshots (
        snapshot_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_time     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        snapshot_date     TEXT,
        total_value       REAL,
        cash_balance      REAL,
        deployed_capital  REAL,
        unrealized_pnl    REAL,
        realized_pnl      REAL,
        total_return_pct  REAL,
        open_positions_json TEXT
    );
CREATE INDEX idx_snapshot_date ON portfolio_snapshots(snapshot_date);
CREATE TABLE acquisition_watchlist (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                date_added          TEXT NOT NULL,
                ticker              TEXT NOT NULL,
                source              TEXT DEFAULT 'daily_briefing',
                market_regime       TEXT,
                model_confidence    REAL,
                entry_conditions    TEXT,
                biggest_risk        TEXT,
                biggest_opportunity TEXT,
                status              TEXT DEFAULT 'pending',
                notes               TEXT,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date_added, ticker)
            );
CREATE INDEX idx_acq_date ON acquisition_watchlist(date_added);
CREATE INDEX idx_acq_status ON acquisition_watchlist(status);
CREATE TABLE stock_research_library (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker              TEXT NOT NULL,
            research_date       TEXT NOT NULL,
            -- Price & technicals
            current_price       REAL,
            price_1w_chg_pct    REAL,
            price_1m_chg_pct    REAL,
            price_52w_high      REAL,
            price_52w_low       REAL,
            avg_volume_20d      INTEGER,
            -- Fundamentals
            market_cap          REAL,
            pe_ratio            REAL,
            forward_pe          REAL,
            peg_ratio           REAL,
            price_to_book       REAL,
            profit_margin       REAL,
            revenue_growth_yoy  REAL,
            earnings_growth_yoy REAL,
            debt_to_equity      REAL,
            free_cash_flow      REAL,
            -- Analyst coverage
            analyst_target_mean REAL,
            analyst_target_high REAL,
            analyst_target_low  REAL,
            analyst_buy_count   INTEGER,
            analyst_hold_count  INTEGER,
            analyst_sell_count  INTEGER,
            -- Earnings
            next_earnings_date  TEXT,
            last_eps_surprise_pct REAL,
            -- SEC filings
            latest_filing_type  TEXT,
            latest_filing_date  TEXT,
            sec_recent_8k_summary TEXT,
            -- Internal signals
            news_mention_count  INTEGER DEFAULT 0,
            news_sentiment_avg  REAL,
            congressional_signal_strength REAL,
            congressional_buy_count INTEGER DEFAULT 0,
            -- Macro context (snapshot)
            macro_score         REAL,
            market_regime       TEXT,
            -- Raw blobs for analyst
            yfinance_info_json  TEXT,
            sec_filings_json    TEXT,
            news_signals_json   TEXT,
            -- Status
            status              TEXT DEFAULT 'library_ready',
            error_notes         TEXT,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP, recommendation_key TEXT, recommendation_mean REAL, analyst_count INTEGER, vix_level REAL, short_pct_float REAL, shares_short INTEGER, short_ratio REAL, short_date TEXT, options_atm_iv_call REAL, options_atm_iv_put REAL, options_put_call_ratio REAL, options_total_call_oi INTEGER, options_total_put_oi INTEGER, options_nearest_expiry TEXT, pre_market_price REAL, pre_market_chg_pct REAL, insider_buys_90d INTEGER DEFAULT 0, insider_sells_90d INTEGER DEFAULT 0, insider_net_sentiment TEXT, insider_last_date TEXT, insider_txns_json TEXT, news_zero_reason TEXT,
            UNIQUE(ticker, research_date)
        );
CREATE INDEX idx_lib_ticker ON stock_research_library(ticker);
CREATE INDEX idx_lib_status ON stock_research_library(status);
CREATE INDEX idx_lib_date   ON stock_research_library(research_date);
CREATE TABLE conditional_tracking (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker                  TEXT NOT NULL,
            date_created            TEXT NOT NULL,
            entry_price_target      REAL,
            entry_price_rationale   TEXT,
            stop_loss               REAL,
            stop_loss_rationale     TEXT,
            take_profit_1           REAL,
            take_profit_2           REAL,
            take_profit_rationale   TEXT,
            position_size_pct       REAL,
            position_size_rationale TEXT,
            time_horizon_days       INTEGER,
            entry_conditions_json   TEXT,
            invalidation_conditions_json TEXT,
            thesis_summary          TEXT,
            key_risks_json          TEXT,
            macro_alignment         TEXT,
            reasoning_chain         TEXT,
            research_confidence     REAL,
            -- Lifecycle
            status                  TEXT DEFAULT 'active',
            -- active → broker is watching this
            -- triggered → broker entered the position
            -- invalidated → thesis failed, archived
            -- flagged → Flash verifier raised concerns, needs analyst review
            -- expired → time horizon passed without trigger
            last_verified           TEXT,
            verification_count      INTEGER DEFAULT 0,
            verification_notes      TEXT,
            created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        , watch_tag TEXT, watch_tag_rationale TEXT, data_gaps_json TEXT, attention_score REAL, attention_updated_at TEXT, invalidation_count INTEGER DEFAULT 0, flag_count INTEGER DEFAULT 0, priority TEXT DEFAULT 'normal', catalyst_event TEXT, catalyst_window_hours INTEGER, catalyst_spike_pct REAL, catalyst_failure_pct REAL, gate_vetoed_until TEXT);
CREATE INDEX idx_cond_ticker ON conditional_tracking(ticker);
CREATE INDEX idx_cond_status ON conditional_tracking(status);
CREATE INDEX idx_cond_date   ON conditional_tracking(date_created);
CREATE INDEX idx_signal_monitoring_date     ON signal_monitoring(monitoring_date DESC);
CREATE INDEX idx_news_signals_timestamp     ON news_signals(timestamp DESC);
CREATE INDEX idx_news_signals_id            ON news_signals(news_signal_id);
CREATE INDEX idx_trade_records_status       ON trade_records(status, exit_date);
CREATE INDEX idx_trade_records_entry_date   ON trade_records(entry_date);
CREATE VIEW v_daily_signal_summary AS
SELECT
    monitoring_date                             AS date,
    COUNT(*)                                    AS cycles,
    ROUND(AVG(signal_score), 1)                AS avg_signal_score,
    ROUND(MAX(signal_score), 1)                AS peak_signal_score,
    ROUND(MIN(defcon_level), 0)                AS lowest_defcon,
    ROUND(AVG(defcon_level), 1)                AS avg_defcon,
    ROUND(AVG(news_score), 1)                  AS avg_news_score,
    ROUND(MAX(news_score), 1)                  AS peak_news_score,
    ROUND(AVG(vix_close), 2)                   AS avg_vix,
    ROUND(AVG(bond_10yr_yield), 3)             AS avg_yield,
    MAX(created_at)                             AS last_cycle_at
FROM signal_monitoring
GROUP BY monitoring_date
/* v_daily_signal_summary(date,cycles,avg_signal_score,peak_signal_score,lowest_defcon,avg_defcon,avg_news_score,peak_news_score,avg_vix,avg_yield,last_cycle_at) */;
CREATE VIEW v_active_conditionals AS
SELECT
    id,
    ticker,
    date_created,
    entry_price_target,
    stop_loss,
    take_profit_1,
    take_profit_2,
    position_size_pct,
    time_horizon_days,
    watch_tag,
    watch_tag_rationale,
    thesis_summary,
    research_confidence,
    entry_conditions_json,
    invalidation_conditions_json,
    CAST(julianday('now') - julianday(date_created) AS INTEGER) AS days_active
FROM conditional_tracking
WHERE status = 'active'
ORDER BY date_created ASC
/* v_active_conditionals(id,ticker,date_created,entry_price_target,stop_loss,take_profit_1,take_profit_2,position_size_pct,time_horizon_days,watch_tag,watch_tag_rationale,thesis_summary,research_confidence,entry_conditions_json,invalidation_conditions_json,days_active) */;
CREATE VIEW v_active_positions AS
SELECT
    t.trade_id,
    t.asset_symbol,
    t.entry_date,
    t.entry_price,
    t.shares,
    t.position_size_dollars,
    t.defcon_at_entry,
    t.current_price,
    t.unrealized_pnl_dollars,
    t.unrealized_pnl_percent,
    t.last_price_updated,
    -- Join to most recent conditional for this ticker (any status)
    ct.watch_tag,
    ct.stop_loss,
    ct.take_profit_1,
    ct.take_profit_2,
    ct.thesis_summary,
    ct.time_horizon_days
FROM trade_records t
LEFT JOIN conditional_tracking ct ON ct.id = (
    SELECT id FROM conditional_tracking
    WHERE ticker = t.asset_symbol
    ORDER BY created_at DESC LIMIT 1
)
WHERE t.status = 'open'
/* v_active_positions(trade_id,asset_symbol,entry_date,entry_price,shares,position_size_dollars,defcon_at_entry,current_price,unrealized_pnl_dollars,unrealized_pnl_percent,last_price_updated,watch_tag,stop_loss,take_profit_1,take_profit_2,thesis_summary,time_horizon_days) */;
CREATE TABLE health_checks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date        TEXT NOT NULL,
                status          TEXT NOT NULL,
                summary         TEXT,
                apis_ok_json    TEXT,
                apis_down_json  TEXT,
                signal_healthy  INTEGER,
                signal_message  TEXT,
                recurring_gaps_json TEXT,
                new_gaps_json   TEXT,
                new_models_json TEXT,
                gap_counts_json TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
CREATE TABLE grok_analysis (
    analysis_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    news_signal_id       INTEGER,
    model_used           TEXT,
    x_sentiment_score    REAL,
    trending_topics      TEXT,
    hidden_narratives    TEXT,
    second_opinion_action TEXT,
    reasoning            TEXT,
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (news_signal_id) REFERENCES news_signals (news_signal_id)
);
CREATE INDEX idx_grok_analysis_news_signal_id ON grok_analysis (news_signal_id);
CREATE TABLE IF NOT EXISTS "grok_hound_candidates" (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL UNIQUE,
    alpha_score         INTEGER,
    why_next            TEXT,
    signals             TEXT,
    risks               TEXT,
    action_suggestion   TEXT,
    status              TEXT DEFAULT 'pending',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE gemini_call_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            model_id     TEXT NOT NULL,
            model_key    TEXT NOT NULL,
            caller       TEXT DEFAULT 'unknown',
            tokens_in    INTEGER DEFAULT 0,
            tokens_out   INTEGER DEFAULT 0,
            downgraded   INTEGER DEFAULT 0,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        , auth_method TEXT DEFAULT 'unknown');
CREATE INDEX idx_gcl_model_time ON gemini_call_log(model_id, created_at);
CREATE TABLE exit_analyst_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id    INTEGER NOT NULL,
            ticker      TEXT NOT NULL,
            ran_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            stop_loss   REAL,
            take_profit_1 REAL,
            take_profit_2 REAL,
            rationale   TEXT,
            tokens_in   INTEGER DEFAULT 0,
            tokens_out  INTEGER DEFAULT 0
        , data_gaps_json TEXT);
CREATE TABLE stream_health (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   TEXT NOT NULL,
                    status      TEXT,
                    ticks       INTEGER,
                    tps         REAL,
                    tickers     INTEGER,
                    entries     INTEGER,
                    exits       INTEGER,
                    peaks       INTEGER,
                    errors      INTEGER,
                    feed        TEXT,
                    details_json TEXT
                );
CREATE TABLE day_trade_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                ticker TEXT,
                scan_time TEXT,
                scan_research TEXT,
                scan_confidence INTEGER,
                scan_sources INTEGER,
                stop_loss_pct REAL,
                take_profit_pct REAL,
                stretch_target_pct REAL,
                tp1_hit_time TEXT,
                high_water_price REAL,
                position_size_dollars REAL,
                cash_available_at_scan REAL,
                entry_trade_id INTEGER,
                entry_price REAL,
                entry_time TEXT,
                shares INTEGER,
                exit_price REAL,
                exit_time TEXT,
                exit_reason TEXT,
                pnl_dollars REAL,
                pnl_percent REAL,
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            , gap_pct REAL, relative_volume REAL, portfolio_risk_pct REAL, suggested_position_dollars REAL, stop_below REAL, first_target REAL, trailing_plan TEXT, edge_summary TEXT, alternatives_json TEXT);
CREATE TABLE notification_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    ticker TEXT,
                    conditional_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
