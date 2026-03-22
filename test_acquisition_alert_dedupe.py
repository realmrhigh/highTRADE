#!/usr/bin/env python3
"""Regression tests for duplicate acquisition Slack alert suppression."""

from trading_db import get_sqlite_conn
import unittest
from unittest.mock import Mock

from broker_agent import AutonomousBroker, DB_PATH


def _cleanup_notification_log():
    conn = get_sqlite_conn(str(DB_PATH), timeout=5)
    try:
        conn.execute("DROP TABLE IF EXISTS notification_log")
        conn.commit()
    finally:
        conn.close()


def _decision():
    return {
        'conditional_id': 4242,
        'ticker': 'XYZ',
        'current_price': 12.34,
        'position_size': 1500.0,
        'position_size_pct': 0.05,
        'confidence': 0.77,
        'stop_loss': 11.4,
        'take_profit_1': 13.7,
        'take_profit_2': 15.1,
        'thesis': 'Short-dated catalyst with tight invalidation.',
        'entry_conditions': ['Volume expansion confirmed'],
        'watch_tag': 'breakout',
    }


class AcquisitionAlertDedupeTests(unittest.TestCase):
    def setUp(self):
        _cleanup_notification_log()

    def _make_broker(self, auto_execute=False, broker_mode='semi_auto'):
        broker = AutonomousBroker(auto_execute=auto_execute, broker_mode=broker_mode)
        broker.notification_engine.alerts.send_acquisition_alert = Mock(return_value=True)
        return broker

    def test_duplicate_trigger_alert_suppressed(self):
        broker = self._make_broker(auto_execute=False, broker_mode='semi_auto')
        decision = _decision()

        broker._notify_acquisition_triggered(decision, executed=False)
        broker._notify_acquisition_triggered(decision, executed=False)

        self.assertEqual(broker.notification_engine.alerts.send_acquisition_alert.call_count, 1)

    def test_trigger_and_executed_alerts_are_distinct(self):
        broker = self._make_broker(auto_execute=True, broker_mode='full_auto')
        decision = _decision()

        broker._notify_acquisition_triggered(decision, executed=False)
        broker._notify_acquisition_triggered(decision, executed=True)

        self.assertEqual(broker.notification_engine.alerts.send_acquisition_alert.call_count, 2)

    def test_notification_log_table_created_in_primary_db(self):
        broker = self._make_broker(auto_execute=False)
        broker._notify_acquisition_triggered(_decision(), executed=False)

        conn = get_sqlite_conn(str(DB_PATH), timeout=5)
        try:
            row = conn.execute(
                "SELECT event_type, ticker, conditional_id FROM notification_log WHERE conditional_id=?",
                (4242,)
            ).fetchone()
            self.assertEqual(row, ('acquisition_triggered', 'XYZ', 4242))
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()