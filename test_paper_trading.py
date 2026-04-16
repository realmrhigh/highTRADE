#!/usr/bin/env python3
"""Focused validation helpers for the current paper trading system."""

import sqlite3
from pathlib import Path

from paper_trading import PaperTradingEngine
from broker_agent import BrokerDecisionEngine


def test_position_sizing():
    """Test VIX-based position sizing"""
    engine = PaperTradingEngine()

    print("\nTEST 5: Position Sizing at Different VIX Levels")
    print("="*70)
    print(f"{'VIX':<6} {'Position Size':<20} {'Change vs Base':<20}")
    print("-"*70)

    base_size = engine.calculate_position_size_vix_adjusted(20.0)

    for vix in [10, 15, 20, 30, 40, 60, 80]:
        size = engine.calculate_position_size_vix_adjusted(vix)
        change_pct = ((size - base_size) / base_size) * 100
        print(f"{vix:<6} ${size:>15,.0f}  {change_pct:>15.1f}%")

    print()


def _create_test_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE trade_records (
        trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
        crisis_id INTEGER,
        asset_symbol TEXT,
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
        exit_reason TEXT,
        profit_loss_dollars REAL,
        profit_loss_percent REAL,
        holding_hours INTEGER,
        notes TEXT,
        status TEXT DEFAULT 'open',
        current_price REAL,
        stop_loss REAL,
        take_profit_1 REAL,
        take_profit_2 REAL,
        unrealized_pnl_dollars REAL,
        unrealized_pnl_percent REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    cur.execute('''
    CREATE TABLE defcon_history (
        defcon_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_date TEXT NOT NULL,
        event_time TEXT NOT NULL,
        defcon_level INTEGER,
        reason TEXT,
        contributing_signals TEXT,
        signal_score REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    conn.commit()
    conn.close()


class FakeAlpaca:
    def __init__(self, positions=None, account=None, configured=True):
        self._positions = positions or []
        self._account = account
        self.is_configured = configured

    def get_positions(self):
        return list(self._positions)

    def get_account(self):
        return self._account

    def place_order(self, symbol, qty, side):
        return {'ok': True, 'order': {'symbol': symbol, 'qty': qty, 'side': side}}


class FakeAlpacaWithSellResponses(FakeAlpaca):
    def __init__(self, sell_responses, positions=None, account=None, configured=True):
        super().__init__(positions=positions, account=account, configured=configured)
        self._sell_responses = list(sell_responses)

    def place_order(self, symbol, qty, side):
        if side == 'sell' and self._sell_responses:
            return self._sell_responses.pop(0)
        return super().place_order(symbol, qty, side)


def test_sync_imports_manual_alpaca_position(tmp_path):
    db_path = tmp_path / 'sync_import.db'
    _create_test_db(db_path)

    engine = PaperTradingEngine(db_path=db_path)
    engine.alpaca = FakeAlpaca(
        positions=[{
            'symbol': 'AAPL',
            'qty': '7',
            'avg_entry_price': '195.50',
            'market_value': '1368.50',
        }],
        account={
            'equity': '101368.50',
            'cash': '98000.00',
            'buying_power': '196000.00',
            'portfolio_value': '101368.50',
            'long_market_value': '1368.50',
            'last_equity': '100900.00',
        },
    )

    positions = engine.get_open_positions()
    assert len(positions) == 1
    assert positions[0]['asset_symbol'] == 'AAPL'
    assert positions[0]['shares'] == 7
    assert positions[0]['entry_price'] == 195.50

    perf = engine.get_portfolio_performance()
    assert perf['open_trades'] == 1
    assert perf['broker_equity'] == 101368.50
    assert perf['broker_cash'] == 98000.00


def test_sync_closes_local_position_missing_at_alpaca(tmp_path):
    db_path = tmp_path / 'sync_close.db'
    _create_test_db(db_path)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO trade_records (
            crisis_id, asset_symbol, entry_date, entry_time, entry_price,
            entry_signal_score, defcon_at_entry, shares, position_size_dollars,
            exit_reason, status, current_price
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (0, 'MSFT', '2026-03-05', '10:00:00', 400.0, 0, 5, 3, 1200.0, None, 'open', 410.0))
    conn.commit()
    conn.close()

    engine = PaperTradingEngine(db_path=db_path)
    engine.alpaca = FakeAlpaca(positions=[], account=None)

    positions = engine.get_open_positions()
    assert positions == []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT status, exit_reason, profit_loss_dollars FROM trade_records WHERE asset_symbol='MSFT'")
    row = dict(cur.fetchone())
    conn.close()

    assert row['status'] == 'closed'
    assert row['exit_reason'] == 'manual'
    assert row['profit_loss_dollars'] == 30.0


def test_sync_reimports_recently_closed_broker_position(tmp_path):
    db_path = tmp_path / 'sync_recent_close.db'
    _create_test_db(db_path)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO trade_records (
            crisis_id, asset_symbol, entry_date, entry_time, entry_price,
            entry_signal_score, defcon_at_entry, shares, position_size_dollars,
            exit_date, exit_time, exit_price, exit_reason, profit_loss_dollars,
            profit_loss_percent, holding_hours, notes, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (0, 'GOOGL', '2026-03-09', '09:00:00', 300.0, 0, 5, 10, 3000.0,
          '2026-03-09', '09:30:00', 303.0, 'manual', 30.0, 1.0, 0.5, 'locally closed', 'closed'))
    conn.commit()
    conn.close()

    engine = PaperTradingEngine(db_path=db_path)
    engine.alpaca = FakeAlpaca(
        positions=[{
            'symbol': 'GOOGL',
            'qty': '10',
            'avg_entry_price': '300.00',
            'market_value': '3010.00',
        }],
        account=None,
    )

    positions = engine.get_open_positions()
    googl_positions = [p for p in positions if p['asset_symbol'] == 'GOOGL']
    assert len(googl_positions) == 1
    assert googl_positions[0]['shares'] == 10


def test_manual_sell_does_not_close_locally_when_broker_rejects(tmp_path):
    db_path = tmp_path / 'manual_sell_reject.db'
    _create_test_db(db_path)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO trade_records (
            crisis_id, asset_symbol, entry_date, entry_time, entry_price,
            entry_signal_score, defcon_at_entry, shares, position_size_dollars,
            exit_reason, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (0, 'AAPL', '2026-03-09', '10:00:00', 200.0, 0, 5, 5, 1000.0, None, 'open'))
    conn.commit()
    conn.close()

    engine = PaperTradingEngine(db_path=db_path)
    engine.alpaca = FakeAlpacaWithSellResponses([
        {'ok': False, 'error': 'wash trade rejection'},
        {'ok': False, 'error': 'wash trade rejection again'},
    ])
    engine._get_current_price = lambda ticker: 201.0

    result = engine.manual_sell('AAPL')
    assert result['ok'] is False
    assert 'local position remains open' in result['message']

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT status, exit_date, exit_price FROM trade_records WHERE asset_symbol='AAPL'")
    row = dict(cur.fetchone())
    conn.close()

    assert row['status'] == 'open'
    assert row['exit_date'] is None
    assert row['exit_price'] is None


def test_exit_position_returns_false_when_broker_rejects(tmp_path):
    db_path = tmp_path / 'exit_position_reject.db'
    _create_test_db(db_path)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO trade_records (
            crisis_id, asset_symbol, entry_date, entry_time, entry_price,
            entry_signal_score, defcon_at_entry, shares, position_size_dollars,
            exit_reason, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (0, 'MSFT', '2026-03-09', '10:00:00', 400.0, 0, 5, 3, 1200.0, None, 'open'))
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()

    engine = PaperTradingEngine(db_path=db_path)
    engine.alpaca = FakeAlpacaWithSellResponses([
        {'ok': False, 'error': 'broker unavailable'},
        {'ok': False, 'error': 'broker unavailable retry'},
    ])

    ok = engine.exit_position(trade_id, 'manual', exit_price=410.0)
    assert ok is False

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT status, exit_date, exit_price FROM trade_records WHERE trade_id=?", (trade_id,))
    row = dict(cur.fetchone())
    conn.close()

    assert row['status'] == 'open'
    assert row['exit_date'] is None
    assert row['exit_price'] is None


def test_day_trader_reconciles_stale_closed_session(tmp_path):
    db_path = tmp_path / 'daytrade_reconcile.db'
    _create_test_db(db_path)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE day_trade_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            ticker TEXT,
            scan_time TEXT,
            scan_research TEXT,
            scan_confidence INTEGER,
            scan_sources INTEGER,
            gap_pct REAL,
            relative_volume REAL,
            stop_loss_pct REAL,
            take_profit_pct REAL,
            stretch_target_pct REAL,
            portfolio_risk_pct REAL,
            suggested_position_dollars REAL,
            stop_below REAL,
            first_target REAL,
            trailing_plan TEXT,
            edge_summary TEXT,
            alternatives_json TEXT,
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
        )
    ''')
    cur.execute('''
        INSERT INTO day_trade_sessions (
            date, ticker, entry_trade_id, entry_price, entry_time, shares,
            position_size_dollars, status, high_water_price
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', ('2026-04-06', 'AAPL', 77, 200.0, '09:35:00', 5, 1000.0, 'bought', 200.0))
    conn.commit()
    conn.close()

    class FakePT:
        def get_open_positions(self):
            return []

    from day_trader import DayTrader
    trader = DayTrader(db_path=db_path, paper_trading=FakePT(), alerts=None, realtime_monitor=None)
    reconciled = trader._reconcile_session_with_position_state('2026-04-06')
    assert reconciled is True

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, exit_reason, exit_price FROM day_trade_sessions WHERE date = ?",
        ('2026-04-06',)
    ).fetchone()
    conn.close()

    assert row['status'] == 'closed'
    assert row['exit_reason'] == 'manual'
    assert row['exit_price'] == 200.0


def test_sync_skips_fresh_trade_in_grace_window(tmp_path):
    db_path = tmp_path / 'sync_grace.db'
    _create_test_db(db_path)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO trade_records (
            crisis_id, asset_symbol, entry_date, entry_time, entry_price,
            entry_signal_score, defcon_at_entry, shares, position_size_dollars,
            exit_reason, status, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (0, 'SLNO', '2026-04-06', '09:39:58', 52.275001525878906, 0, 5, 12, 627.3, None, 'open', '[DAYTRADE] Grok pick'))
    conn.commit()
    conn.close()

    engine = PaperTradingEngine(db_path=db_path)
    engine.alpaca = FakeAlpaca(positions=[], account=None)

    positions = engine.get_open_positions()
    assert positions == []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status, exit_reason FROM trade_records WHERE asset_symbol='SLNO'").fetchone()
    conn.close()

    # Fresh trade should remain open because of the grace window; no ghost close.
    assert row['status'] == 'open'
    assert row['exit_reason'] is None


def test_defcon_crisis_basket_disabled():
    """DEFCON-triggered basket buys should be disabled in favor of dynamic acquisition flow."""
    broker = BrokerDecisionEngine()

    decision = broker.analyze_market_for_trades(
        defcon_level=2,
        signal_score=78.0,
        crisis_description='Tariff escalation driving a market selloff and recovery setup',
        market_data={'vix': 28.0},
    )

    assert decision is None


if __name__ == '__main__':
    print("\n" + "="*70)
    print("PAPER TRADING ENGINE - VALIDATION SUITE")
    print("="*70)

    test_position_sizing()
    test_defcon_crisis_basket_disabled()

    print("="*70)
    print("All tests completed successfully!")
    print("="*70)
