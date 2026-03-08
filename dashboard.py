#!/usr/bin/env python3
"""
HighTrade Dashboard Generator
Queries SQLite and produces a rich, self-contained HTML dashboard.
Run:  python dashboard.py [--open]
"""

import sqlite3
import json
import sys
import os
import webbrowser
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from paper_trading import PaperTradingEngine

_ET = ZoneInfo('America/New_York')       # trading schedule — always Eastern
_LOCAL_TZ = None                          # display timezone — auto-detected below

# Detect system timezone via macOS /etc/localtime symlink
try:
    _p = Path('/etc/localtime')
    if _p.is_symlink():
        _link = str(_p.resolve())
        _idx = _link.find('zoneinfo/')
        if _idx >= 0:
            _LOCAL_TZ = ZoneInfo(_link[_idx + 9:])
except Exception:
    pass
if _LOCAL_TZ is None:
    _LOCAL_TZ = _ET  # fallback to ET if detection fails

def _et_now() -> datetime:
    """Current time in ET — used for trading schedule logic."""
    return datetime.now(_ET)

def _local_now() -> datetime:
    """Current time in local timezone — used for dashboard display."""
    return datetime.now(_LOCAL_TZ)

def _utc_to_local(utc_str: str) -> str:
    """Convert a UTC datetime string to local-tz 'YYYY-MM-DD HH:MM:SS TZ' string."""
    try:
        dt_utc = datetime.fromisoformat(utc_str.replace('T', ' ')[:19]).replace(
            tzinfo=ZoneInfo('UTC'))
        dt_local = dt_utc.astimezone(_LOCAL_TZ)
        return dt_local.strftime('%Y-%m-%d %H:%M:%S ') + dt_local.tzname()
    except Exception:
        return utc_str or '—'

SCRIPT_DIR = Path(__file__).parent
DB_PATH    = SCRIPT_DIR / "trading_data" / "trading_history.db"
OUT_PATH   = SCRIPT_DIR / "trading_data" / "dashboard.html"


def _engine() -> PaperTradingEngine:
    """Create a paper trading engine bound to the dashboard database."""
    return PaperTradingEngine(db_path=DB_PATH)


# ─── Data Layer ──────────────────────────────────────────────────────────────

def _conn():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    return db

def fetch_system_status():
    with _conn() as db:
        row = db.execute("""
            SELECT defcon_level, signal_score, vix_close, bond_10yr_yield, news_score, created_at
            FROM signal_monitoring ORDER BY created_at DESC LIMIT 1
        """).fetchone()
        return dict(row) if row else {}

def fetch_open_positions():
    engine = _engine()
    positions = engine.get_open_positions()
    return sorted(positions, key=lambda row: row.get('entry_date') or '')

def fetch_closed_trades():
    with _conn() as db:
        rows = db.execute("""
            SELECT asset_symbol, shares, entry_price, exit_price,
                   profit_loss_dollars, profit_loss_percent,
                   exit_reason, entry_date, exit_date, holding_hours
            FROM trade_records WHERE status='closed'
            ORDER BY exit_date DESC LIMIT 20
        """).fetchall()
        return [dict(r) for r in rows]

def fetch_portfolio_stats():
    perf = _engine().get_portfolio_performance()
    positions = fetch_open_positions()

    return {
        'total_trades': perf.get('total_trades', 0),
        'closed': perf.get('closed_trades', 0),
        'open_count': perf.get('open_trades', 0),
        'wins': perf.get('winning_trades', 0),
        'losses': perf.get('losing_trades', 0),
        'realized_pnl': round(perf.get('total_profit_loss_dollars', 0) or 0, 2),
        'unrealized_pnl': round(sum((p.get('unrealized_pnl_dollars') or 0) for p in positions), 2),
        'deployed': round(sum((p.get('position_size_dollars') or 0) for p in positions), 2),
        'broker_equity': round(perf.get('broker_equity', 0) or 0, 2),
        'broker_cash': round(perf.get('broker_cash', 0) or 0, 2),
        'broker_buying_power': round(perf.get('broker_buying_power', 0) or 0, 2),
        'broker_long_market_value': round(perf.get('broker_long_market_value', 0) or 0, 2),
        'broker_day_change_dollars': round(perf.get('broker_day_change_dollars', 0) or 0, 2),
    }

def fetch_daily_briefings():
    with _conn() as db:
        rows = db.execute("""
            SELECT date, model_key, market_regime, regime_confidence,
                   headline_summary, key_themes_json, biggest_risk,
                   biggest_opportunity, signal_quality, macro_alignment,
                   portfolio_assessment, watchlist_json, defcon_forecast,
                   model_confidence, created_at
            FROM daily_briefings
            ORDER BY created_at DESC LIMIT 12
        """).fetchall()
        return [dict(r) for r in rows]

def fetch_macro():
    with _conn() as db:
        row = db.execute("""
            SELECT yield_curve_spread, fed_funds_rate, unemployment_rate,
                   m2_yoy_change, hy_oas_bps, consumer_sentiment,
                   rate_10y, rate_2y, macro_score, defcon_modifier,
                   bearish_signals, bullish_signals, signals_json, created_at
            FROM macro_indicators ORDER BY created_at DESC LIMIT 1
        """).fetchone()
        return dict(row) if row else {}

def fetch_acquisition_watchlist():
    with _conn() as db:
        rows = db.execute("""
            SELECT ticker, source, market_regime, model_confidence,
                   entry_conditions, biggest_risk, biggest_opportunity,
                   status, notes, date_added, created_at
            FROM acquisition_watchlist
            WHERE status NOT IN ('archived', 'invalidated')
            ORDER BY
                CASE source
                    WHEN 'stop_loss_rebound'            THEN 1
                    WHEN 'profit_target_reaccumulation' THEN 2
                    ELSE 3
                END,
                created_at DESC
            LIMIT 20
        """).fetchall()
        return [dict(r) for r in rows]

def fetch_signal_history():
    """Last 48 monitoring cycles for sparkline charts.

    news_score falls back to the nearest news_signals row when signal_monitoring
    has a zero (pre-fix historical data or cycles where news wasn't fetched).
    """
    with _conn() as db:
        rows = db.execute("""
            SELECT sm.defcon_level, sm.signal_score, sm.vix_close,
                   COALESCE(
                       NULLIF(sm.news_score, 0.0),
                       (SELECT ns.news_score FROM news_signals ns
                        WHERE ns.created_at <= sm.created_at
                        ORDER BY ns.created_at DESC LIMIT 1),
                       0
                   ) as news_score,
                   sm.created_at
            FROM signal_monitoring sm
            ORDER BY sm.created_at DESC LIMIT 48
        """).fetchall()
        return list(reversed([dict(r) for r in rows]))

def fetch_recent_news():
    with _conn() as db:
        rows = db.execute("""
            SELECT ns.news_signal_id, ns.news_score, ns.dominant_crisis_type as crisis_type,
                   ns.article_count, ns.breaking_count, ns.sentiment_summary as sentiment,
                   ns.gemini_flash_json, ns.created_at,
                   ga.recommended_action as gemini_pro_action,
                   ga.reasoning as gemini_pro_reasoning,
                   ga.confidence_in_signal as gemini_pro_confidence,
                   ga.model_used as deep_dive_model
            FROM news_signals ns
            LEFT JOIN gemini_analysis ga ON ns.news_signal_id = ga.news_signal_id AND ga.trigger_type IN ('elevated', 'breaking')
            ORDER BY ns.created_at DESC LIMIT 6
        """).fetchall()
        return [dict(r) for r in rows]

def fetch_sector_vix():
    """Fetch latest sector and VIX term structure if available in news_signals JSON"""
    with _conn() as db:
        row = db.execute("SELECT gemini_flash_json FROM news_signals WHERE gemini_flash_json IS NOT NULL ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row: return {}
        # This is a bit of a hack since we don't have a dedicated table yet, 
        # but the orchestrator stores the results in the signals context.
        # Actually, let's just return placeholders for now or try to parse the latest signal
        return {}

def fetch_congressional():
    with _conn() as db:
        clusters = db.execute("""
            SELECT ticker, signal_strength, buy_count as politician_count,
                   total_amount as total_dollar_amount,
                   bipartisan as is_bipartisan, committee_relevance, created_at
            FROM congressional_cluster_signals
            ORDER BY created_at DESC LIMIT 10
        """).fetchall()
        trades = db.execute("""
            SELECT politician as politician_name, ticker, direction as transaction_type,
                   amount as amount_range, transaction_date, party,
                   source as chamber
            FROM congressional_trades
            ORDER BY transaction_date DESC LIMIT 15
        """).fetchall()
        return [dict(r) for r in trades], [dict(r) for r in clusters] # swapped order for internal use if needed

def fetch_hound_candidates():
    with _conn() as db:
        rows = db.execute("""
            SELECT ticker, alpha_score as meme_score, why_next as why_next_gme, signals, risks, action_suggestion, created_at
            FROM grok_hound_candidates
            WHERE status = 'pending'
            ORDER BY alpha_score DESC LIMIT 10
        """).fetchall()
        return [dict(r) for r in rows]

def fetch_hound_last_run():
    """Return ISO timestamp of most recent hound run, or None."""
    with _conn() as db:
        row = db.execute("""
            SELECT created_at FROM grok_hound_candidates
            ORDER BY created_at DESC LIMIT 1
        """).fetchone()
        return row[0] if row else None

def fetch_grok_usage():
    """
    Return Grok API usage today (midnight UTC) from grok_analysis + daily_briefings.
    Grok is pay-per-token (no daily limit), so we track calls and tokens only.
    """
    try:
        import sqlite3 as _sq
        from datetime import datetime, timezone
        _today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        _conn = _sq.connect(str(DB_PATH))
        # Deep dive calls — now stored in gemini_analysis with model_used='grok-*'
        _d = _conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) "
            "FROM gemini_analysis WHERE model_used LIKE 'grok%' AND created_at >= ?", (_today,)
        ).fetchone() or (0, 0, 0)
        # Daily briefing calls
        _b = _conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) "
            "FROM daily_briefings WHERE model_key='grok' AND date=?", (_today,)
        ).fetchone() or (0, 0, 0)
        _conn.close()
        return {
            'calls':       (_d[0] or 0) + (_b[0] or 0),
            'tokens_in':   (_d[1] or 0) + (_b[1] or 0),
            'tokens_out':  (_d[2] or 0) + (_b[2] or 0),
            'deep_calls':  _d[0] or 0,
            'brief_calls': _b[0] or 0,
        }
    except Exception:
        return {}


def fetch_gemini_usage():
    """
    Return Gemini usage since midnight UTC for all tracked models.
    Returns dict keyed by model_id:
      {
        'gemini-2.5-pro': {
            'calls': 42, 'tokens_in': 210000, 'tokens_out': 18000,
            'daily_limit': 1500, 'rpm_limit': 120,
            'pct': 0.028, 'status': 'ok',
            'resets_in_s': 51420,   # seconds until midnight UTC
        }, ...
      }
    """
    try:
        import gemini_client
        # Use reset-aligned counts — tallied since midnight UTC
        # so percentages and "resets in" are consistent.
        usage = gemini_client.get_reset_aligned_usage()
        result = {}
        for model_id, data in usage.items():
            pct = data.get('pct', 0.0)
            if pct >= gemini_client.QUOTA_BLOCK_PCT:
                status = 'block'
            elif pct >= gemini_client.QUOTA_WARN_PCT:
                status = 'warn'
            else:
                status = 'ok'
            result[model_id] = {
                **data,
                'status':     status,
                'soft_limit': data.get('daily_limit', 0),  # backward compat
            }
        return result
    except Exception:
        return {}


def fetch_stream_health():
    """Fetch latest real-time stream health from stream_health table."""
    try:
        with _conn() as db:
            row = db.execute("""
                SELECT timestamp, status, ticks, tps, tickers,
                       entries, exits, peaks, errors, feed, details_json
                FROM stream_health
                ORDER BY id DESC LIMIT 1
            """).fetchone()
            if not row:
                return None
            result = dict(row)
            try:
                result['details'] = json.loads(result.get('details_json') or '{}')
            except Exception:
                result['details'] = {}
            return result
    except Exception:
        return None


def fetch_exit_queue():
    """Fetch open positions enriched with exit levels from trade_records and
    conditional_tracking (analyst-set stops/TPs). Uses COALESCE so analyst
    levels fill in when the trade_record fields are NULL (manual entries)."""
    # Ensure broker positions are synced into trade_records before querying exit queue.
    try:
        _engine().get_open_positions()
    except Exception:
        pass
    with _conn() as db:
        try:
            rows = db.execute("""
                SELECT
                    t.asset_symbol,
                    t.shares,
                    t.entry_price,
                    t.current_price,
                    t.peak_price,
                    t.position_size_dollars,
                    t.unrealized_pnl_dollars,
                    t.unrealized_pnl_percent,
                    t.entry_date,
                    t.defcon_at_entry,
                    -- Effective exit levels: trade_record value wins, else most-recent analyst conditional
                    COALESCE(t.stop_loss,     c.stop_loss)     AS stop_loss,
                    COALESCE(t.take_profit_1, c.take_profit_1) AS take_profit_1,
                    COALESCE(t.take_profit_2, c.take_profit_2) AS take_profit_2,
                    c.stop_loss_rationale,
                    c.take_profit_rationale,
                    c.invalidation_conditions_json,
                    c.thesis_summary,
                    c.watch_tag,
                    c.research_confidence,
                    CASE WHEN c.id IS NOT NULL THEN 1 ELSE 0 END AS has_framework
                FROM trade_records t
                LEFT JOIN (
                    -- One row per ticker: the most recent conditional by created_at
                    SELECT * FROM conditional_tracking
                    WHERE status IN ('active','invalidated','filled')
                      AND id IN (
                          SELECT id FROM conditional_tracking c2
                          WHERE c2.status IN ('active','invalidated','filled')
                          GROUP BY UPPER(c2.ticker)
                          HAVING id = MAX(id)
                      )
                ) c ON UPPER(t.asset_symbol) = UPPER(c.ticker)
                WHERE t.status = 'open'
                ORDER BY t.entry_date ASC
            """).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


def fetch_active_conditionals():
    """Fetch active entry conditionals ordered by attention score descending."""
    with _conn() as db:
        try:
            rows = db.execute("""
                SELECT ticker, entry_price_target, stop_loss, take_profit_1,
                       research_confidence, watch_tag, thesis_summary,
                       entry_price_rationale, stop_loss_rationale, take_profit_rationale,
                       attention_score, verification_count, date_created
                FROM conditional_tracking
                WHERE status = 'active'
                ORDER BY COALESCE(attention_score, 0) DESC, research_confidence DESC
                LIMIT 30
            """).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


# ─── Utility Helpers ─────────────────────────────────────────────────────────

def defcon_color(level):
    colors = {1: '#00ff88', 2: '#7fff00', 3: '#ffd700', 4: '#ff8c00', 5: '#ff3333'}
    return colors.get(int(level), '#aaa')

def defcon_label(level):
    labels = {1: 'BULLISH', 2: 'ELEVATED', 3: 'CAUTIOUS', 4: 'DEFENSIVE', 5: 'BEARISH'}
    return labels.get(int(level), 'UNKNOWN')

def pnl_color(val):
    try:
        v = float(val or 0)
        return '#00ff88' if v >= 0 else '#ff4444'
    except Exception:
        return '#aaa'

def fmt_dollar(val):
    try:
        v = float(val or 0)
        sign = '+' if v >= 0 else ''
        return f"{sign}${v:,.2f}"
    except Exception:
        return '$0.00'

def fmt_pct(val):
    try:
        v = float(val or 0)
        sign = '+' if v >= 0 else ''
        return f"{sign}{v:.2f}%"
    except Exception:
        return '0.00%'

def macro_score_ring(score):
    """SVG ring for macro score"""
    try:
        s = float(score)
    except Exception:
        s = 50
    r = 38
    circ = 2 * 3.14159 * r
    dash = (s / 100) * circ
    color = '#00ff88' if s >= 60 else '#ffd700' if s >= 40 else '#ff4444'
    return (
        '<svg width="100" height="100" viewBox="0 0 100 100">'
        f'<circle cx="50" cy="50" r="{r}" fill="none" stroke="#1a1a2e" stroke-width="10"/>'
        f'<circle cx="50" cy="50" r="{r}" fill="none" stroke="{color}" stroke-width="10"'
        f' stroke-dasharray="{dash:.1f} {circ:.1f}"'
        f' stroke-dashoffset="{circ/4:.1f}" stroke-linecap="round"/>'
        f'<text x="50" y="45" text-anchor="middle" fill="{color}" font-size="18" font-weight="bold" font-family="monospace">{s:.0f}</text>'
        f'<text x="50" y="62" text-anchor="middle" fill="#888" font-size="9" font-family="monospace">MACRO</text>'
        '</svg>'
    )

def sparkline(values, color='#00d4ff', height=40, width=180):
    if not values:
        return f'<svg width="{width}" height="{height}"></svg>'
    mn, mx = min(values), max(values)
    rng = mx - mn or 1
    pts = []
    for i, v in enumerate(values):
        x = int(i / max(len(values) - 1, 1) * width)
        y = int((1 - (v - mn) / rng) * (height - 4) + 2)
        pts.append(f"{x},{y}")
    path = ' '.join(pts)
    fill_pts = f"0,{height} " + path + f" {width},{height}"
    grad_id = f"sg{abs(hash(color)) % 100000}"
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<defs><linearGradient id="{grad_id}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{color}" stop-opacity="0.4"/>'
        f'<stop offset="100%" stop-color="{color}" stop-opacity="0"/>'
        f'</linearGradient></defs>'
        f'<polygon points="{fill_pts}" fill="url(#{grad_id})"/>'
        f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="1.5" stroke-linejoin="round"/>'
        f'</svg>'
    )

def regime_badge(regime):
    colors = {
        'bullish': '#00ff88', 'risk-on': '#00ff88',
        'transitioning': '#ffd700',
        'bearish': '#ff4444', 'risk-off': '#ff4444',
        'neutral': '#888'
    }
    r = (regime or 'unknown').lower()
    color = next((v for k, v in colors.items() if k in r), '#888')
    return (
        f'<span style="background:{color}22;color:{color};border:1px solid {color};'
        f'border-radius:4px;padding:2px 8px;font-size:11px;font-weight:700;'
        f'letter-spacing:1px;text-transform:uppercase;">{regime or "—"}</span>'
    )

def exit_badge(reason):
    if reason == 'profit_target':
        return '<span style="color:#00ff88;font-size:11px;">&#10003; PROFIT TARGET</span>'
    elif reason == 'stop_loss':
        return '<span style="color:#ff4444;font-size:11px;">&#10007; STOP LOSS</span>'
    elif reason == 'manual':
        return '<span style="color:#7eb8f7;font-size:11px;">&#8617; MANUAL</span>'
    return f'<span style="color:#888;font-size:11px;">{reason or "—"}</span>'

def action_badge(action):
    c = {'BUY': '#00ff88', 'SELL': '#ff4444', 'WAIT': '#ffd700', 'HOLD': '#888'}.get(action, '#888')
    return f'<span style="color:{c};font-weight:700;font-size:12px;">{action or "—"}</span>'

def sig_pill(sig):
    sev = sig.get('severity', 'neutral')
    c = {'bullish': '#00ff88', 'bearish': '#ff4444', 'neutral': '#888'}.get(sev, '#888')
    arrow = '&#9650;' if sev == 'bullish' else '&#9660;' if sev == 'bearish' else '&#9670;'
    return (
        f'<div style="color:{c};font-size:12px;margin:3px 0;">'
        f'{arrow} {sig.get("description", "")}</div>'
    )


# ─── Section Builders ─────────────────────────────────────────────────────────

def build_open_rows(positions):
    if not positions:
        return '<tr><td colspan="10" style="color:#555;text-align:center;padding:20px;">No open positions</td></tr>'
    rows = []
    for p in positions:
        ep    = float(p.get('entry_price') or 0)
        cp    = float(p.get('current_price') or ep)
        sh    = int(p.get('shares') or 0)
        pnl_d = float(p.get('unrealized_pnl_dollars') or 0)
        pnl_p = float(p.get('unrealized_pnl_percent') or 0)
        mv    = cp * sh
        dc    = p.get('defcon_at_entry', '?')
        stop  = p.get('stop_loss')
        tp1   = p.get('take_profit_1')
        color = pnl_color(pnl_d)
        bar_w = min(int(abs(pnl_p) * 3), 100)
        bar_c = '#00ff88' if pnl_d >= 0 else '#ff4444'

        # Stop distance color
        if stop and cp:
            stop_dist = (float(stop) - cp) / cp * 100
            stop_style = 'color:#ff4444;font-weight:700;' if abs(stop_dist) <= 3 else 'color:#ffb300;' if abs(stop_dist) <= 8 else 'color:#888;'
            stop_cell = f'<span style="{stop_style}">${float(stop):,.2f}</span>'
        else:
            stop_cell = '<span style="color:#444;">—</span>'

        # TP1 distance color
        if tp1 and cp:
            tp_dist = (float(tp1) - cp) / cp * 100
            tp_style = 'color:#00ff88;font-weight:700;' if 0 < tp_dist <= 3 else 'color:#00d4ff;' if 0 < tp_dist <= 8 else 'color:#888;'
            tp_cell = f'<span style="{tp_style}">${float(tp1):,.2f}</span>'
        else:
            tp_cell = '<span style="color:#444;">—</span>'

        rows.append(
            '<tr class="trow">'
            f'<td class="sym" onclick="showChart(\'{p.get("asset_symbol", "?")}\')">{p.get("asset_symbol", "?")}</td>'
            f'<td>{sh:,}</td>'
            f'<td>${ep:,.2f}</td>'
            f'<td>${cp:,.2f}</td>'
            f'<td>${mv:,.2f}</td>'
            f'<td style="color:{color};">{fmt_dollar(pnl_d)}</td>'
            f'<td><div style="display:flex;align-items:center;gap:6px;">'
            f'<div style="width:60px;background:#111;border-radius:3px;height:4px;">'
            f'<div style="width:{bar_w}%;background:{bar_c};height:4px;border-radius:3px;"></div></div>'
            f'<span style="color:{color};">{fmt_pct(pnl_p)}</span></div></td>'
            f'<td>{stop_cell}</td>'
            f'<td>{tp_cell}</td>'
            f'<td><span style="font-size:11px;color:#888">D{dc}</span></td>'
            '</tr>'
        )
    return ''.join(rows)

def build_closed_rows(closed):
    if not closed:
        return '<tr><td colspan="8" style="color:#555;text-align:center;padding:20px;">No closed trades</td></tr>'
    rows = []
    for t in closed:
        pnl_d = float(t.get('profit_loss_dollars') or 0)
        pnl_p = float(t.get('profit_loss_percent') or 0)
        color = pnl_color(pnl_d)
        rows.append(
            '<tr class="trow">'
            f'<td class="sym" onclick="showChart(\'{t.get("asset_symbol", "?")}\')">{t.get("asset_symbol", "?")}</td>'
            f'<td>{int(t.get("shares") or 0):,}</td>'
            f'<td>${float(t.get("entry_price") or 0):,.2f}</td>'
            f'<td>${float(t.get("exit_price") or 0):,.2f}</td>'
            f'<td style="color:{color};">{fmt_dollar(pnl_d)}</td>'
            f'<td style="color:{color};">{fmt_pct(pnl_p)}</td>'
            f'<td>{exit_badge(t.get("exit_reason"))}</td>'
            f'<td style="color:#666;font-size:11px;">{t.get("exit_date", "—")}</td>'
            '</tr>'
        )
    return ''.join(rows)

def build_exit_queue_rows(positions):
    """Build exit queue table rows for all open positions."""
    if not positions:
        return '<tr><td colspan="10" style="color:#555;text-align:center;padding:20px;">No open positions</td></tr>'

    from datetime import datetime as _dt
    rows = []
    for p in positions:
        sym      = p.get('asset_symbol', '?')
        entry    = float(p.get('entry_price')    or 0)
        current  = float(p.get('current_price')  or entry)
        peak     = float(p.get('peak_price')     or entry or 0)
        upnl_d   = float(p.get('unrealized_pnl_dollars')  or 0)
        upnl_pct = float(p.get('unrealized_pnl_percent')  or 0)
        stop     = p.get('stop_loss')
        tp1      = p.get('take_profit_1')
        tp2      = p.get('take_profit_2')
        has_fw   = bool(p.get('has_framework'))
        watch_tag = p.get('watch_tag') or ''

        analyst_stop = float(stop) if stop else None
        trailing_stop = round(peak * 0.97, 2) if peak else None
        effective_stop = analyst_stop
        stop_source = 'analyst'
        if trailing_stop and trailing_stop > 0:
            if effective_stop is None or trailing_stop > effective_stop:
                effective_stop = trailing_stop
                stop_source = 'trailing'

        # ── Thesis / popup (mirrors entry queue pattern) ──────────────────
        thesis_full  = p.get('thesis_summary') or '—'
        thesis_short = thesis_full[:160]
        stop_rat     = p.get('stop_loss_rationale') or ''
        tp_rat       = p.get('take_profit_rationale') or ''
        inv_cond     = p.get('invalidation_conditions_json') or ''

        def _esc(s):
            return s.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;')
        exit_popup_lines = [thesis_full]
        if analyst_stop and stop_rat:
            exit_popup_lines.append(f'\n🛑 THESIS FLOOR   ${analyst_stop:.2f}\n{stop_rat}')
        if trailing_stop and trailing_stop > 0:
            trailing_note = (
                f"3% below peak ${peak:.2f}" if peak and peak > entry
                else f"3% below entry/peak base ${peak:.2f}"
            )
            exit_popup_lines.append(f'\n📉 TRAILING STOP   ${trailing_stop:.2f}\n{trailing_note}')
        if tp1 and tp_rat:
            exit_popup_lines.append(f'\n🎯 TP1    ${float(tp1):.2f}\n{tp_rat}')
        if inv_cond:
            exit_popup_lines.append(f'\n⚠️ INVALIDATION\n{inv_cond}')
        exit_popup_full = '\n'.join(exit_popup_lines)
        exit_popup_attr = _esc(exit_popup_full)

        # ── Hold time ─────────────────────────────────────────────────────
        hold_str = '—'
        try:
            ed = _dt.fromisoformat(str(p.get('entry_date') or '')[:10])
            days = (_dt.now() - ed).days
            hold_str = f'{days}d'
        except Exception:
            pass

        # ── Distance calculations ─────────────────────────────────────────
        def _dist_cell(target, price, direction):
            """Return (display_str, cell_style) for a distance-to-level cell."""
            if target is None or price is None or price == 0:
                return '—', 'color:#444;'
            target = float(target)
            dist_pct = (target - price) / price * 100
            if direction == 'down':   # stop — negative is bad (already broken)
                dist_abs = abs(dist_pct)
                if dist_abs <= 3:
                    style = 'color:#ff4444;font-weight:700;'
                elif dist_abs <= 8:
                    style = 'color:#ffb300;font-weight:600;'
                else:
                    style = 'color:#888;'
                return f'{dist_pct:+.1f}%', style
            else:                     # TP — positive is good
                if 0 < dist_pct <= 3:
                    style = 'color:#00ff88;font-weight:700;'
                elif 0 < dist_pct <= 8:
                    style = 'color:#00d4ff;'
                elif dist_pct <= 0:   # already above TP
                    style = 'color:#00ff88;font-weight:700;'
                else:
                    style = 'color:#888;'
                return f'{dist_pct:+.1f}%', style

        # ── Stop cell ─────────────────────────────────────────────────────
        if effective_stop:
            stop_f = float(effective_stop)
            sd, ss = _dist_cell(stop_f, current, 'down')
            stop_label = 'TRAIL' if stop_source == 'trailing' else 'FLOOR'
            stop_cell = (
                f'<div style="font-size:12px;font-weight:600;">${stop_f:,.2f}</div>'
                f'<div style="font-size:10px;{ss}">{sd}</div>'
                f'<div style="font-size:9px;color:{"#00d4ff" if stop_source == "trailing" else "#888"};letter-spacing:1px;">{stop_label}</div>'
            )
        else:
            stop_cell = '<div style="color:#ff4444;font-size:10px;letter-spacing:1px;">⚠ NOT SET</div>'

        # ── TP1 cell ──────────────────────────────────────────────────────
        if tp1:
            tp1_f = float(tp1)
            td1, ts1 = _dist_cell(tp1_f, current, 'up')
            tp1_cell = (
                f'<div style="font-size:12px;font-weight:600;">${tp1_f:,.2f}</div>'
                f'<div style="font-size:10px;{ts1}">{td1}</div>'
            )
        else:
            tp1_cell = '<div style="color:#444;font-size:10px;">—</div>'

        # ── TP2 cell ──────────────────────────────────────────────────────
        if tp2:
            tp2_f = float(tp2)
            td2, ts2 = _dist_cell(tp2_f, current, 'up')
            tp2_cell = (
                f'<div style="font-size:12px;font-weight:600;">${tp2_f:,.2f}</div>'
                f'<div style="font-size:10px;{ts2}">{td2}</div>'
            )
        else:
            tp2_cell = '<div style="color:#444;font-size:10px;">—</div>'

        # ── Framework badge ───────────────────────────────────────────────
        fw_badge = (
            '<span style="background:#1a2a1a;color:#00ff88;font-size:9px;'
            'padding:2px 6px;border-radius:3px;letter-spacing:1px;">🎯 ANALYST</span>'
            if has_fw else
            '<span style="background:#2a1a1a;color:#ff8c44;font-size:9px;'
            'padding:2px 6px;border-radius:3px;letter-spacing:1px;">✋ MANUAL</span>'
        )

        # ── P&L cell ──────────────────────────────────────────────────────
        pnl_c = pnl_color(upnl_d)

        # ── Watch tag pill ────────────────────────────────────────────────
        tag_html = ''
        if watch_tag:
            tag_html = (
                f'<div style="font-size:9px;color:#888;margin-top:2px;'
                f'letter-spacing:1px;">{watch_tag.upper()}</div>'
            )

        rows.append(
            '<tr class="trow">'
            f'<td class="sym" onclick="showChart(\'{sym}\')">'
            f'  {sym}{tag_html}'
            f'</td>'
            f'<td style="color:#ddd;">${entry:,.2f}<br>'
            f'  <span style="color:#888;font-size:10px;">→ ${current:,.2f}</span></td>'
            f'<td style="color:{pnl_c};font-weight:700;">{fmt_dollar(upnl_d)}'
            f'  <br><span style="font-size:10px;">{fmt_pct(upnl_pct)}</span></td>'
            f'<td>{stop_cell}</td>'
            f'<td>{tp1_cell}</td>'
            f'<td>{tp2_cell}</td>'
            f'<td style="color:#666;font-size:11px;">{hold_str}</td>'
            f'<td class="has-thesis" style="color:#888;font-size:10px;max-width:280px;word-wrap:break-word;overflow-wrap:break-word;" '
            f'data-thesis="{exit_popup_attr}" data-ticker="{sym}">'
            f'{thesis_short}{"…" if len(thesis_full) > 160 else ""}</td>'
            f'<td>{fw_badge}</td>'
            '</tr>'
        )

    return ''.join(rows)


def source_badge(source):
    cfg = {
        'stop_loss_rebound':           ('#ff4444', '🔄 REBOUND'),
        'profit_target_reaccumulation':('#00d4ff', '♻️  RE-ACCUM'),
        'daily_briefing':              ('#c084fc', '🧠 BRIEFING'),
        'manual':                      ('#ffd700', '✋ MANUAL'),
        'grok_hound':                  ('#ff8c00', '🦮 HOUND'),
        'grok_hound_auto':             ('#ff8c00', '🦮 HOUND'),
    }
    color, label = cfg.get(source, ('#888', source.upper().replace('_', ' ')))
    return (
        f'<span style="background:{color}22;color:{color};border:1px solid {color}55;'
        f'border-radius:4px;padding:2px 7px;font-size:10px;font-weight:700;'
        f'white-space:nowrap;">{label}</span>'
    )

def build_wl_rows(watchlist):
    if not watchlist:
        return '<tr><td colspan="7" style="color:#555;text-align:center;padding:20px;">Watchlist empty — waiting for next daily briefing</td></tr>'
    rows = []
    for w in watchlist:
        conf   = float(w.get('model_confidence') or 0)
        conf_c = '#00ff88' if conf >= 0.7 else '#ffd700' if conf >= 0.5 else '#ff8c00'
        stat   = w.get('status', 'pending')
        stat_c = {'pending': '#ffd700', 'active': '#00ff88', 'invalidated': '#ff4444'}.get(stat, '#888')
        raw_cond = w.get('entry_conditions') or '—'
        cond   = raw_cond[:250]
        cond_attr = raw_cond.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;')
        src    = w.get('source', 'daily_briefing')
        wl_ticker = w.get('ticker', '?')
        # Color-code thesis cell by status: analyst_pass=dimmer, conditional_set=brighter
        cond_c = '#888' if stat == 'analyst_pass' else '#aaa'
        rows.append(
            '<tr class="trow">'
            f'<td class="sym" onclick="showChart(\'{wl_ticker}\')">{wl_ticker}</td>'
            f'<td>{source_badge(src)}</td>'
            f'<td><span style="color:{conf_c};font-weight:700;">{conf:.0%}</span></td>'
            f'<td>{regime_badge(w.get("market_regime"))}</td>'
            f'<td class="has-thesis" style="color:{cond_c};font-size:11px;max-width:320px;word-wrap:break-word;overflow-wrap:break-word;" '
            f'data-thesis="{cond_attr}" data-ticker="{wl_ticker}">'
            f'{cond}{"…" if len(raw_cond)>250 else ""}</td>'
            f'<td><span style="color:{stat_c};font-size:11px;text-transform:uppercase;">{stat}</span></td>'
            f'<td style="color:#666;font-size:11px;">{w.get("date_added", "—")}</td>'
            '</tr>'
        )
    return ''.join(rows)

def _attention_badge(score):
    """Return emoji badge for attention score (None/0-39=cold, 40-74=warm, 75+=hot)."""
    if score is None or float(score) < 40:
        return '⬜'
    elif float(score) < 75:
        return '🟡'
    else:
        return '🔥'

def build_conditional_rows(conditionals):
    if not conditionals:
        return '<tr><td colspan="8" style="color:#555;text-align:center;padding:20px;">No active conditionals</td></tr>'
    rows = []
    for c in conditionals:
        score    = c.get('attention_score')
        badge    = _attention_badge(score)
        score_str = f"{score:.0f}" if score is not None else '—'
        conf     = float(c.get('research_confidence') or 0)
        conf_c   = '#00ff88' if conf >= 0.75 else '#ffd700' if conf >= 0.5 else '#888'
        target   = c.get('entry_price_target')
        stop     = c.get('stop_loss')
        tp1      = c.get('take_profit_1')
        ticker   = c.get('ticker', '?')
        tag      = (c.get('watch_tag') or 'untagged').replace('-', ' ').title()
        thesis_full  = c.get('thesis_summary') or '—'
        thesis       = thesis_full[:160]
        entry_rat    = c.get('entry_price_rationale') or ''
        stop_rat     = c.get('stop_loss_rationale') or ''
        tp_rat       = c.get('take_profit_rationale') or ''
        verif        = int(c.get('verification_count') or 0)

        # Build the structured popup body (newlines become visible in pre-wrap)
        def _esc(s):
            return s.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;')
        popup_lines = [thesis_full]
        if target and entry_rat:
            popup_lines.append(f'\n📍 ENTRY  ${target:.2f}\n{entry_rat}')
        if stop and stop_rat:
            popup_lines.append(f'\n🛑 STOP   ${stop:.2f}\n{stop_rat}')
        if tp1 and tp_rat:
            popup_lines.append(f'\n🎯 TP1    ${tp1:.2f}\n{tp_rat}')
        popup_full = '\n'.join(popup_lines)
        popup_attr = _esc(popup_full)

        rows.append(
            '<tr class="trow">'
            f'<td class="sym" onclick="showChart(\'{ticker}\')">{badge} {ticker}</td>'
            f'<td style="color:#aaa;font-size:11px;text-align:center;">{score_str}</td>'
            f'<td><span style="color:{conf_c};font-weight:700;">{conf:.0%}</span></td>'
            f'<td style="color:#00d4ff;font-size:11px;">{f"${target:.2f}" if target else "—"}</td>'
            f'<td style="color:#ff8c00;font-size:11px;">{f"${stop:.2f}" if stop else "—"}</td>'
            f'<td style="color:#7fff00;font-size:11px;">{f"${tp1:.2f}" if tp1 else "—"}</td>'
            f'<td style="color:#888;font-size:10px;">{tag}</td>'
            f'<td class="has-thesis" style="color:#666;font-size:10px;max-width:280px;word-wrap:break-word;overflow-wrap:break-word;" '
            f'data-thesis="{popup_attr}" data-ticker="{ticker}">'
            f'{thesis}{"…" if len(thesis_full) > 160 else ""}</td>'
            '</tr>'
        )
    return ''.join(rows)

def _utc_to_et_str(utc_str: str) -> str:
    """Convert a UTC datetime string ('2026-03-01 05:50:55') to local-tz 'MM/DD HH:MM AM/PM' label."""
    try:
        from datetime import timezone
        _UTC = timezone.utc
        dt_utc = datetime.fromisoformat(utc_str.replace('T', ' ')[:19]).replace(tzinfo=_UTC)
        dt_local = dt_utc.astimezone(_LOCAL_TZ)
        return dt_local.strftime('%m/%d %I:%M %p').lstrip('0')
    except Exception:
        return (utc_str or '—')[11:16]


def build_news_items(news):
    if not news:
        return '<div style="color:#555;text-align:center;padding:20px;">No news signals</div>'
    items = []
    for n in news:
        score = float(n.get('news_score') or 0)
        sc = '#ff4444' if score >= 70 else '#ffd700' if score >= 45 else '#888'
        ts = _utc_to_et_str(n.get('created_at') or '')

        # Primary insight: prefer Pro reasoning, fall back to sentiment summary
        reasoning = (n.get('gemini_pro_reasoning') or '').strip()
        sentiment  = (n.get('sentiment') or '').strip()
        if not reasoning and sentiment:
            insight_html = f'<div style="font-size:11px;color:#777;margin-top:4px;">{sentiment[:120]}</div>'
        elif reasoning:
            insight_html = f'<div style="font-size:11px;color:#888;margin-top:4px;font-style:italic;">{reasoning[:130]}{"…" if len(reasoning)>=130 else ""}</div>'
        else:
            insight_html = '<div style="font-size:11px;color:#444;margin-top:4px;font-style:italic;">Low-score cycle — Flash analysis not triggered</div>'

        items.append(
            '<div class="news-item">'
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">'
            f'<span style="color:#666;font-size:10px;">{ts} ET <span onclick="sendCommand(\'/update\')" style="cursor:pointer;margin-left:5px;color:#555;" title="Rerun Analysis">&#8635;</span></span>'
            f'<span style="color:{sc};font-size:13px;font-weight:700;">{score:.1f}</span>'
            f'<span style="color:#aaa;font-size:11px;">{(n.get("crisis_type","")).replace("_"," ").upper()}</span>'
            '<div>'
            f'{action_badge(n.get("gemini_pro_action"))}'
            '</div>'
            '</div>'
            f'<div style="font-size:11px;color:#555;">{n.get("article_count",0)} articles &middot; {n.get("breaking_count",0)} breaking</div>'
            f'{insight_html}'
            '</div>'
        )
    return ''.join(items)

def build_cong_cluster_rows(clusters):
    if not clusters:
        return '<tr><td colspan="6" style="color:#555;text-align:center;padding:16px;">No cluster signals (S3 data intermittent)</td></tr>'
    rows = []
    for c in clusters:
        strength = float(c.get('signal_strength') or 0)
        sc = '#00ff88' if strength >= 70 else '#ffd700' if strength >= 40 else '#888'
        bi = '&#9889; BIPARTISAN' if c.get('is_bipartisan') else ''
        rows.append(
            '<tr class="trow">'
            f'<td class="sym">{c.get("ticker","?")}</td>'
            f'<td style="color:{sc};font-weight:700;">{strength:.0f}</td>'
            f'<td>{c.get("politician_count",0)}</td>'
            f'<td>${float(c.get("total_dollar_amount") or 0):,.0f}</td>'
            f'<td><span style="color:#7eb8f7;font-size:10px;">{bi}</span></td>'
            f'<td style="color:#666;font-size:11px;">{(c.get("created_at") or "—")[:10]}</td>'
            '</tr>'
        )
    return ''.join(rows)

def build_cong_trade_rows(trades):
    if not trades:
        return '<tr><td colspan="5" style="color:#555;text-align:center;padding:16px;">No congressional trades recorded</td></tr>'
    rows = []
    for t in trades:
        is_buy = 'Purchase' in (t.get('transaction_type') or '')
        color = '#00ff88' if is_buy else '#ff4444'
        arrow = '&#9650;' if is_buy else '&#9660;'
        rows.append(
            '<tr class="trow">'
            f'<td style="color:{color};">{arrow} {t.get("ticker","?")}</td>'
            f'<td style="color:#aaa;font-size:11px;">{t.get("politician_name","?")}</td>'
            f'<td style="color:#888;font-size:10px;">{t.get("party","?")} &middot; {t.get("chamber","?")}</td>'
            f'<td style="color:#888;font-size:11px;">{t.get("amount_range","?")}</td>'
            f'<td style="color:#666;font-size:11px;">{t.get("transaction_date","—")}</td>'
            '</tr>'
        )
    return ''.join(rows)

def build_hound_rows(candidates):
    if not candidates:
        return '<tr><td colspan="6" style="color:#555;text-align:center;padding:16px;">🐕 Hound is still hunting...</td></tr>'
    rows = []
    for c in candidates:
        score = int(c.get('meme_score') or 0)
        sc = '#00ff88' if score >= 75 else '#ffd700' if score >= 50 else '#888'
        ticker = c.get('ticker', '?')
        rows.append(
            '<tr class="trow">'
            f'<td class="sym" onclick="showChart(\'{ticker}\')">{ticker}</td>'
            f'<td style="color:{sc};font-weight:700;">{score}</td>'
            f'<td style="color:#ddd;font-size:11px;">{c.get("why_next_gme","—")}</td>'
            f'<td>{action_badge(c.get("action_suggestion","").upper())}</td>'
            f'<td style="color:#666;font-size:11px;">{(c.get("created_at") or "—")[5:10].replace("-", "/")}</td>'
            f'<td><div style="display:flex;gap:5px;">'
            f'<button onclick="approveTicker(\'{ticker}\')" style="background:#00ff8822;color:#00ff88;border:1px solid #00ff8844;padding:2px 6px;border-radius:3px;font-size:9px;cursor:pointer;">APPROVE</button>'
            f'<button onclick="rejectTicker(\'{ticker}\')" style="background:#ff444422;color:#ff4444;border:1px solid #ff444444;padding:2px 6px;border-radius:3px;font-size:9px;cursor:pointer;">REJECT</button>'
            f'</div></td>'
            '</tr>'
        )
    return ''.join(rows)

def build_flash_card(b, label, time_str, emoji, border_color='#00d4ff33'):
    """Compact card for morning/midday flash briefings (plain-text summary, no JSON)."""
    if not b:
        fired = False
        return (
            f'<div class="model-card" style="border-color:{border_color};opacity:0.45;">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">'
            f'<div class="card-label" style="font-size:11px;">{emoji} {label}</div>'
            f'<div style="color:#555;font-size:10px;">{time_str}</div></div>'
            f'<div style="color:#444;font-size:11px;font-style:italic;">Not yet fired</div>'
            f'</div>'
        )

    summary    = b.get('headline_summary') or '—'
    macro_line = b.get('macro_alignment') or ''
    ts         = (b.get('created_at') or '')[:16].replace('T', ' ')

    # Parse gaps from full_response_json
    gaps = []
    try:
        import json as _j
        fr = _j.loads(b.get('full_response_json') or '{}')
        gaps = fr.get('gaps', [])
    except Exception:
        pass
    gaps_html = (
        f'<div style="color:#666;font-size:10px;margin-top:6px;">🔍 Gaps: {", ".join(gaps[:3])}</div>'
        if gaps else ''
    )

    return (
        f'<div class="model-card" style="border-color:{border_color};display:flex;flex-direction:column;">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-shrink:0;">'
        f'<div class="card-label" style="font-size:11px;">{emoji} {label}</div>'
        f'<div style="color:#888;font-size:10px;">{time_str} · {ts}</div></div>'
        f'<div style="color:#999;font-size:10px;margin-bottom:6px;">{macro_line}</div>'
        f'<div style="color:#ccc;font-size:11px;line-height:1.6;flex:1;">{summary}</div>'
        f'{gaps_html}'
        f'</div>'
    )


def build_model_card(b, title, icon):
    if not b:
        return f'<div class="model-card"><div class="card-label">{icon} {title}</div><p style="color:#555;margin-top:8px;">No data yet</p></div>'
    conf = float(b.get('model_confidence') or 0)
    conf_c = '#00ff88' if conf >= 0.7 else '#ffd700' if conf >= 0.5 else '#ff4444'
    try:
        themes = json.loads(b.get('key_themes_json') or '[]')
    except Exception:
        themes = []
    theme_pills = ''.join(f'<div class="theme-pill">{t}</div>' for t in themes)
    try:
        wl = json.loads(b.get('watchlist_json') or '[]')
    except Exception:
        wl = []
    ticker_tags = ''.join(f'<span class="ticker-tag" style="cursor:pointer;" onclick="showChart(\'{t}\')">{t}</span>' for t in wl)
    
    # Extract full text for detailed fields with fallbacks for legacy/varied keys
    sig_quality = b.get('signal_quality') or b.get('signal_quality_assessment') or '—'
    port_assessment = b.get('portfolio_assessment') or '—'
    risk = b.get('biggest_risk') or b.get('biggest_risk_today') or '—'
    opp = b.get('biggest_opportunity') or b.get('biggest_opportunity_today') or '—'
    
    return (
        '<div class="model-card" style="display:flex;flex-direction:column;max-height:600px;">'
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-shrink:0;">'
        f'<div class="card-label">{icon} {title}</div>'
        '<div style="display:flex;gap:8px;align-items:center;">'
        f'{regime_badge(b.get("market_regime"))}'
        f'<span style="color:{conf_c};font-weight:700;font-size:13px;">{conf:.0%}</span>'
        '</div></div>'
        
        '<div class="scroll-wrap" style="flex:1;overflow-y:auto;padding-right:5px;">'
        f'<div style="color:#ddd;font-size:12px;line-height:1.6;margin-bottom:12px;font-weight:bold;">{b.get("headline_summary","—")}</div>'
        f'<div class="themes-row" style="margin-bottom:12px;">{theme_pills}</div>'
        
        '<div style="margin-bottom:10px;">'
        '<div class="micro-label">SIGNAL QUALITY</div>'
        f'<div style="color:#aaa;font-size:11px;line-height:1.5;">{sig_quality}</div>'
        '</div>'
        
        '<div style="margin-bottom:10px;">'
        '<div class="micro-label">PORTFOLIO ASSESSMENT</div>'
        f'<div style="color:#aaa;font-size:11px;line-height:1.5;">{port_assessment}</div>'
        '</div>'
        
        '<div class="two-col" style="margin-bottom:10px;">'
        '<div><div class="micro-label">BIGGEST RISK</div>'
        f'<div style="color:#ff8888;font-size:11px;line-height:1.5;">{risk}</div></div>'
        '<div><div class="micro-label">OPPORTUNITY</div>'
        f'<div style="color:#88ff88;font-size:11px;line-height:1.5;">{opp}</div></div>'
        '</div>'
        
        '<div style="margin-bottom:10px;"><div class="micro-label">WATCHLIST</div>'
        f'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:4px;">{ticker_tags}</div></div>'
        
        '<div><div class="micro-label">DEFCON FORECAST</div>'
        f'<div style="color:#888;font-size:11px;line-height:1.5;">{b.get("defcon_forecast", "—")}</div></div>'
        '</div>'
        
        '</div>'
    )


# ─── Main HTML Assembly ───────────────────────────────────────────────────────

def build_html(status, positions, closed, stats, briefings, macro, watchlist,
               sig_history, news, cong_clusters, cong_trades, hound_candidates, hound_last_run=None,
               conditionals=None, gemini_usage=None, grok_usage=None, exit_queue=None, stream_health=None):

    _ln = _local_now()
    now_str    = _ln.strftime('%Y-%m-%d %H:%M:%S ') + _ln.tzname()
    last_cycle = _utc_to_local(status.get('created_at', '—'))

    # Hound last-run display
    if hound_last_run:
        try:
            lr = datetime.fromisoformat(str(hound_last_run).replace('Z', ''))
            # hound timestamps are stored in UTC; convert to local for display
            lr_local = lr.replace(tzinfo=ZoneInfo('UTC')).astimezone(_LOCAL_TZ)
            delta = _local_now() - lr_local
            mins = int(delta.total_seconds() // 60)
            hound_last_str = f"{lr_local.strftime('%m/%d %I:%M %p')} ({mins}m ago)" if mins < 120 else lr_local.strftime('%m/%d %I:%M %p')
        except Exception:
            hound_last_str = str(hound_last_run)[:16]
    else:
        hound_last_str = 'No runs yet'

    # ── Grok usage counter widget ────────────────────────────────────────────
    _gu = grok_usage or {}
    _g_calls      = _gu.get('calls', 0)
    _g_tok_in     = _gu.get('tokens_in', 0)
    _g_tok_out    = _gu.get('tokens_out', 0)
    _g_deep       = _gu.get('deep_calls', 0)
    _g_brief      = _gu.get('brief_calls', 0)
    _g_call_color = '#00ff88' if _g_calls > 0 else '#555'
    grok_usage_html = f"""
      <div class="stat">
        <div class="stat-label">&#120143; Grok Usage &mdash; Since Midnight UTC</div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:2px;">
          <span style="color:#aaa;font-size:9px;">grok-4-1-fast-reasoning &nbsp;&middot;&nbsp; pay-per-token</span>
          <span style="color:{_g_call_color};font-size:10px;font-weight:600;">{_g_calls} calls &nbsp;&middot;&nbsp; {_g_tok_in:,} in &nbsp;&middot;&nbsp; {_g_tok_out:,} out</span>
        </div>
        <div style="color:#444;font-size:9px;">deep_dives: {_g_deep} &nbsp;&middot;&nbsp; briefings: {_g_brief} &nbsp;&middot;&nbsp; total tok: {_g_tok_in + _g_tok_out:,}</div>
      </div>"""

    # ── Gemini quota widget HTML ─────────────────────────────────────────────
    _quota_color = {'ok': '#00ff88', 'warn': '#ffb300', 'block': '#ff4444'}
    _quota_label = {'ok': 'OK', 'warn': 'WARN', 'block': '⚠ NEAR LIMIT'}
    _auth_icon   = {'cli': '🔐', 'rest': '🔑', 'unknown': '❓'}
    _model_short = {
        'gemini-3.1-pro-preview':      '3.1 Pro Preview ★ REASONING (250/d, 25 RPM)',
        'gemini-3-flash-preview':      '3 Flash ★ FAST (1500/d, 120 RPM)',
        'gemini-2.5-pro':              '2.5 Pro ↩ CLI FALLBACK (1500/d, 120 RPM)',
        'gemini-3.1-flash-lite-preview': '3.1 Flash Lite ↩ REST FALLBACK (1500/d, 120 RPM)',
    }
    _model_order = [
        'gemini-3.1-pro-preview',
        'gemini-3-flash-preview',
        'gemini-2.5-pro',
        'gemini-3.1-flash-lite-preview',
    ]
    gemini_usage = gemini_usage or {}
    _quota_rows  = ''
    for _mid in _model_order:
        _d       = gemini_usage.get(_mid, {})
        _calls   = _d.get('calls', 0)
        _limit   = _d.get('daily_limit', 0)
        _rpm     = _d.get('rpm_limit', '?')
        _pct     = _d.get('pct', 0.0)
        _st      = _d.get('status', 'ok')
        _col     = _quota_color.get(_st, '#00ff88')
        _lbl     = _quota_label.get(_st, 'OK')
        _tok_in  = _d.get('tokens_in', 0) or 0
        _tok_out = _d.get('tokens_out', 0) or 0
        _bar_w   = min(int(_pct * 100), 100)
        _short   = _model_short.get(_mid, _mid)
        # Auth breakdown string (e.g. "🔐 cli: 42 · 🔑 rest: 3")
        _auth_bd = _d.get('auth_breakdown', {})
        if _auth_bd:
            _auth_parts = []
            for _ak, _av in sorted(_auth_bd.items()):
                _ai = _auth_icon.get(_ak, '')
                _auth_parts.append(f'{_ai}{_ak}:{_av}')
            _auth_str = ' &middot; '.join(_auth_parts)
        else:
            _auth_str = 'no calls'
        # Resets-in string (midnight UTC)
        _rs = _d.get('resets_in_s', 0)
        if _rs > 0:
            _rh, _rm = divmod(_rs // 60, 60)
            _reset_str = f'resets at midnight UTC ({_rh}h {_rm}m)'
        elif _calls == 0:
            _reset_str = 'no calls today'
        else:
            _reset_str = 'resets at midnight UTC'
        _quota_rows += f"""
        <div style="margin-bottom:8px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:2px;">
            <span style="color:#aaa;font-size:9px;">{_short}</span>
            <span style="color:{_col};font-size:10px;font-weight:600;">{_calls}/{_limit} &nbsp;·&nbsp; {_pct*100:.0f}% &nbsp;·&nbsp; {_lbl}</span>
          </div>
          <div style="background:#1a1a2e;border-radius:3px;height:5px;overflow:hidden;">
            <div style="width:{_bar_w}%;height:5px;background:{_col};border-radius:3px;transition:width 0.3s;"></div>
          </div>
          <div style="color:#444;font-size:9px;margin-top:1px;">in: {_tok_in:,} tok &nbsp;·&nbsp; out: {_tok_out:,} tok &nbsp;·&nbsp; {_auth_str} &nbsp;·&nbsp; {_reset_str}</div>
        </div>"""
    if not _quota_rows:
        _quota_rows = '<div style="color:#555;font-size:10px;">No calls logged yet</div>'
    gemini_quota_html = f"""
      <div class="stat">
        <div class="stat-label">&#128200; Gemini Quota &mdash; Since Midnight UTC</div>
        {_quota_rows}
      </div>"""

    # ── Real-time stream health widget ───────────────────────────────────────
    stream_html = ''
    if stream_health:
        _sh = stream_health
        _st_status = _sh.get('status', 'unknown')
        _st_color = '#00ff88' if _st_status == 'streaming' else (
            '#ffb300' if 'reconnect' in _st_status else '#ff4444' if 'failed' in _st_status or 'disabled' in _st_status else '#aaa')
        _st_tps = _sh.get('tps', 0)
        _st_tickers = _sh.get('tickers', 0)
        _st_ticks = _sh.get('ticks', 0)
        _st_entries = _sh.get('entries', 0)
        _st_exits = _sh.get('exits', 0)
        _st_peaks = _sh.get('peaks', 0)
        _st_errors = _sh.get('errors', 0)
        _st_feed = _sh.get('feed', '?').upper()
        _st_details = _sh.get('details', {})
        _st_reconnects = _st_details.get('reconnects', 0)
        _st_ts = _sh.get('timestamp', '')
        try:
            _st_ts_display = _utc_to_local(_st_ts) if _st_ts else '—'
        except Exception:
            _st_ts_display = _st_ts or '—'

        stream_html = f"""
      <div class="stat">
        <div class="stat-label">&#128308; Real-Time Stream &mdash; Alpaca WebSocket ({_st_feed})</div>
        <div style="display:flex;gap:18px;flex-wrap:wrap;font-size:11px;margin-top:4px;">
          <span style="color:{_st_color};font-weight:bold;">{_st_status.upper()}</span>
          <span title="Ticks per second">{_st_tps}/s</span>
          <span title="Subscribed tickers">{_st_tickers} tickers</span>
          <span title="Total ticks received">{_st_ticks:,} ticks</span>
          <span title="Entry triggers fired" style="color:#00ff88;">&#127919; {_st_entries} entries</span>
          <span title="Exit triggers fired" style="color:#ff6b6b;">&#128721; {_st_exits} exits</span>
          <span title="Peak price updates" style="color:#6bb3ff;">&#9650; {_st_peaks} peaks</span>
          <span title="Reconnections">&#128260; {_st_reconnects} reconnects</span>
          {'<span style="color:#ff4444;">&#9888; ' + str(_st_errors) + ' errors</span>' if _st_errors > 0 else ''}
        </div>
        <div style="color:#555;font-size:9px;margin-top:3px;">Last health log: {_st_ts_display}</div>
      </div>"""

    total_capital = 100_000.0
    realized      = float(stats.get('realized_pnl') or 0)
    unrealized    = float(stats.get('unrealized_pnl') or 0)
    deployed      = float(stats.get('deployed') or 0)
    account_value = total_capital + realized + unrealized
    cash          = total_capital + realized - deployed
    total_pnl     = realized + unrealized
    wins          = int(stats.get('wins') or 0)
    losses        = int(stats.get('losses') or 0)
    win_rate      = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

    vix_vals  = [r['vix_close']    for r in sig_history if r.get('vix_close')]
    news_vals = [r['news_score']   for r in sig_history if r.get('news_score') is not None]
    sig_vals  = [r['signal_score'] for r in sig_history if r.get('signal_score') is not None]

    latest_b = next((b for b in briefings if b.get('model_key') == 'reasoning'), {})
    latest_grok = next((b for b in briefings if b.get('model_key') == 'grok'), {})
    fast_b   = next((b for b in briefings if b.get('model_key') == 'fast'), {})
    bal_b    = next((b for b in briefings if b.get('model_key') == 'balanced'), {})

    # Today's intraday flash briefings — match most recent of each type within 36h
    # (36h window handles after-midnight viewing where ET date != briefing date)
    _today_str  = _et_now().strftime('%Y-%m-%d')
    _cutoff_str = (_et_now() - __import__('datetime').timedelta(hours=36)).strftime('%Y-%m-%d %H:%M:%S')
    morning_b = next((b for b in briefings
                      if b.get('model_key') == 'morning_flash'
                      and (b.get('created_at') or '') >= _cutoff_str), {})
    midday_b  = next((b for b in briefings
                      if b.get('model_key') == 'midday_flash'
                      and (b.get('created_at') or '') >= _cutoff_str), {})

    macro_sigs = []
    try:
        macro_sigs = json.loads(macro.get('signals_json') or '[]')
    except Exception:
        pass

    defcon          = int(status.get('defcon_level') or 5)
    dc_color        = defcon_color(defcon)
    dc_label        = defcon_label(defcon)
    signal_score    = float(status.get('signal_score') or 0)
    vix             = float(status.get('vix_close') or 0)
    bond            = float(status.get('bond_10yr_yield') or 0)
    macro_score_val = float(macro.get('macro_score') or 50)
    rate_2y         = float(macro.get('rate_2y') or 0)

    # DEFCON block row
    defcon_blocks = ''
    for i in range(1, 6):
        active = (i == defcon)
        bg = defcon_color(i) if active else '#1a1a2e'
        fc = '#000' if active else defcon_color(i)
        fw = '900' if active else '400'
        defcon_blocks += (
            f'<div style="background:{bg};color:{fc};font-weight:{fw};'
            f'width:34px;height:34px;display:flex;align-items:center;'
            f'justify-content:center;border-radius:4px;font-size:14px;'
            f'border:1px solid {defcon_color(i)}33;">{i}</div>'
        )

    vix_spark  = sparkline(vix_vals,  '#ff8c00')
    news_spark = sparkline(news_vals, '#c084fc')
    sig_spark  = sparkline(sig_vals,  '#00d4ff')

    vix_last  = f"{vix_vals[-1]:.2f}"  if vix_vals  else '—'
    news_last = f"{news_vals[-1]:.1f}" if news_vals else '—'
    sig_last  = f"{sig_vals[-1]:.1f}"  if sig_vals  else '—'

    briefing_date = latest_b.get('date', '—')

    try:
        latest_b_themes = json.loads(latest_b.get('key_themes_json') or '[]')
    except Exception:
        latest_b_themes = []
    latest_theme_pills = ''.join(f'<div class="theme-pill">{t}</div>' for t in latest_b_themes)
    latest_conf = float(latest_b.get('model_confidence') or 0)
    latest_conf_c = '#00ff88' if latest_conf >= 0.7 else '#ffd700' if latest_conf >= 0.5 else '#ff4444'

    macro_ring = macro_score_ring(macro_score_val)
    macro_supportive = 'SUPPORTIVE' if macro_score_val >= 60 else 'NEUTRAL' if macro_score_val >= 40 else 'HEADWIND'
    yc_spread = float(macro.get('yield_curve_spread') or 0)
    yc_color = '#00ff88' if yc_spread > 0 else '#ff4444'
    hy_bps = float(macro.get('hy_oas_bps') or 0)
    hy_color = '#ff4444' if hy_bps > 400 else '#00ff88'
    cs_val = float(macro.get('consumer_sentiment') or 0)
    cs_color = '#ff4444' if cs_val < 60 else '#00ff88'

    open_rows      = build_open_rows(positions)
    closed_rows    = build_closed_rows(closed)
    exit_queue_rows = build_exit_queue_rows(exit_queue or [])
    wl_rows        = build_wl_rows(watchlist)
    cond_rows      = build_conditional_rows(conditionals or [])
    news_items     = build_news_items(news)
    hound_rows     = build_hound_rows(hound_candidates)
    cong_cl_rows   = build_cong_cluster_rows(cong_clusters)
    cong_tr_rows   = build_cong_trade_rows(cong_trades)
    reasoning_card = build_model_card(latest_b, 'REASONING (Gemini 3.1)', '🔬')
    grok_card      = build_model_card(latest_grok, 'DEEP DIVE (Grok 4.1)', '𝕏')

    # Daily schedule intraday cards
    morning_card = build_flash_card(morning_b, 'MARKET OPEN', '9:30 AM', '🌅', '#00d4ff33')
    midday_card  = build_flash_card(midday_b,  'MID-DAY',     '12:00 PM', '☀️', '#ffd70033')
    # Close card — compact version of the reasoning brief if available
    close_date_str = latest_b.get('date', '—')
    close_fired = bool(latest_b and (latest_b.get('created_at') or '') >= _cutoff_str)
    if close_fired:
        _close_summary = (latest_b.get('headline_summary') or '')[:280]
        _close_regime  = latest_b.get('market_regime', 'unknown')
        _close_conf    = float(latest_b.get('model_confidence') or 0)
        close_card = (
            f'<div class="model-card" style="border-color:#c084fc33;display:flex;flex-direction:column;">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-shrink:0;">'
            f'<div class="card-label" style="font-size:11px;">📋 CLOSE DEEP DIVE</div>'
            f'<div style="color:#888;font-size:10px;">4:30 PM · {(latest_b.get("created_at") or "")[:16].replace("T"," ")}</div></div>'
            f'<div style="color:#999;font-size:10px;margin-bottom:6px;">{regime_badge(_close_regime)} conf={_close_conf:.0%}</div>'
            f'<div style="color:#ccc;font-size:11px;line-height:1.6;flex:1;">{_close_summary}{"..." if len(latest_b.get("headline_summary","")) > 280 else ""}</div>'
            f'</div>'
        )
    else:
        close_card = (
            '<div class="model-card" style="border-color:#c084fc33;opacity:0.45;">'
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">'
            '<div class="card-label" style="font-size:11px;">📋 CLOSE DEEP DIVE</div>'
            '<div style="color:#555;font-size:10px;">4:30 PM</div></div>'
            '<div style="color:#444;font-size:11px;font-style:italic;">Not yet fired — triggers after market close</div>'
            '</div>'
        )

    macro_sig_pills = ''.join(sig_pill(s) for s in macro_sigs)

    total_pnl_pct = total_pnl / total_capital * 100
    defcon_mod_val = float(macro.get('defcon_modifier') or 0)

    # Broker mode + live DB row counts for architecture panel
    try:
        import sqlite3 as _sq
        _conn = _sq.connect(str(DB_PATH))
        _rows = _conn.execute("""
            SELECT SUM(cnt) FROM (
                SELECT COUNT(*) AS cnt FROM signal_monitoring
                UNION ALL SELECT COUNT(*) FROM news_signals
                UNION ALL SELECT COUNT(*) FROM gemini_analysis
                UNION ALL SELECT COUNT(*) FROM macro_indicators
                UNION ALL SELECT COUNT(*) FROM daily_briefings
                UNION ALL SELECT COUNT(*) FROM conditional_tracking
            )
        """).fetchone()
        _conn.close()
        db_rows = int(_rows[0]) if _rows and _rows[0] else 0
    except Exception:
        db_rows = 0

    try:
        import subprocess as _sp
        _ps = _sp.run(['pgrep', '-a', '-f', 'hightrade_orchestrator'], capture_output=True, text=True)
        _line = _ps.stdout.strip()
        if '--broker' in _line:
            broker_mode = _line.split('--broker')[1].strip().split()[0]
        elif 'full_auto' in _line:
            broker_mode = 'full_auto'
        elif 'disabled' in _line:
            broker_mode = 'disabled'
        else:
            broker_mode = 'semi_auto'
    except Exception:
        broker_mode = 'semi_auto'

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>HighTrade Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
:root {
  --bg:#08090f; --panel:#0d0e1a; --border:#1e2040;
  --text:#e2e8f0; --dim:#64748b; --accent:#00d4ff;
  --green:#00ff88; --red:#ff4444; --gold:#ffd700;
}
* { box-sizing:border-box; margin:0; padding:0; }
body { background:var(--bg); color:var(--text); font-family:'SF Mono','Fira Code','Cascadia Code',monospace; font-size:13px; }
.page { max-width:1600px; margin:0 auto; padding:20px; }

/* Header */
.header { display:flex; justify-content:space-between; align-items:center;
  padding:16px 24px; border-bottom:1px solid var(--border); margin-bottom:20px;
  background:linear-gradient(90deg,#0d0e1a,#08090f); border-radius:10px; }
.header-title { font-size:22px; font-weight:900; letter-spacing:4px; color:var(--accent); }
.header-meta { color:var(--dim); font-size:11px; text-align:right; line-height:1.8; }

/* Grid layouts */
.grid-top    { display:grid; grid-template-columns:260px 1fr 260px; gap:16px; margin-bottom:16px; }
.grid-mid    { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px; }
.grid-three  { display:grid; grid-template-columns:1fr 1fr 1fr; gap:16px; margin-bottom:16px; }
.grid-four   { display:grid; grid-template-columns:1fr 1fr 1fr 1fr; gap:16px; margin-bottom:16px; }
.grid-full   { margin-bottom:16px; }

/* Panels */
.panel { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:16px; }
.panel-title { font-size:10px; font-weight:700; letter-spacing:2px; color:var(--dim);
  text-transform:uppercase; margin-bottom:14px; display:flex; align-items:center; gap:6px; }
.panel-title::before { content:''; display:inline-block; width:3px; height:12px;
  background:var(--accent); border-radius:2px; }

/* Stat cards */
.stat-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:10px; }
.stat { background:#0a0b14; border:1px solid var(--border); border-radius:8px; padding:12px; }
.stat-label { font-size:9px; color:var(--dim); letter-spacing:1.5px; text-transform:uppercase; margin-bottom:4px; }
.stat-value { font-size:20px; font-weight:700; }
.stat-sub   { font-size:10px; color:var(--dim); margin-top:2px; }

/* DEFCON */
.defcon-num   { font-size:72px; font-weight:900; line-height:1; }
.defcon-label { font-size:11px; letter-spacing:3px; margin-top:4px; }
.defcon-row   { display:flex; gap:6px; margin-top:14px; }

/* Tables */
table { width:100%; border-collapse:collapse; }
th { font-size:9px; color:var(--dim); letter-spacing:1.5px; text-transform:uppercase;
     padding:6px 8px; border-bottom:1px solid var(--border); text-align:left; font-weight:400; }
.trow td { padding:8px 8px; border-bottom:1px solid #111; font-size:12px; }
.trow:hover { background:#ffffff05; }
.sym { font-weight:700; color:var(--accent); font-size:14px; cursor:pointer; text-decoration:none; }
.sym:hover { text-decoration:underline; color:#fff; }

/* Model cards */
.model-card { background:#0a0b14; border:1px solid var(--border); border-radius:8px; padding:14px; }
.card-label { font-size:10px; font-weight:700; letter-spacing:2px; color:var(--dim); text-transform:uppercase; }
.themes-row { display:flex; flex-wrap:wrap; gap:6px; }
.theme-pill { background:#1a1a2e; border:1px solid #2a2a50; border-radius:20px;
              padding:3px 10px; font-size:10px; color:#aaa; }
.micro-label { font-size:9px; color:var(--dim); letter-spacing:1.5px; text-transform:uppercase; margin-bottom:3px; }
.two-col { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.ticker-tag { background:#00d4ff15; color:var(--accent); border:1px solid #00d4ff33;
              border-radius:4px; padding:2px 8px; font-size:11px; font-weight:700; }

/* News */
.news-item { border-bottom:1px solid #111; padding:10px 0; }
.news-item:last-child { border-bottom:none; }
.news-scroll { max-height:360px; overflow-y:auto; }

/* Macro */
.macro-grid { display:grid; grid-template-columns:100px 1fr; gap:16px; align-items:start; }
.macro-row { display:flex; justify-content:space-between; padding:5px 0; border-bottom:1px solid #111; font-size:12px; }
.macro-row:last-child { border:none; }

/* Scrollable tables */
.scroll-wrap { max-height:300px; overflow-y:auto; }
.scroll-wrap::-webkit-scrollbar { width:4px; }
.scroll-wrap::-webkit-scrollbar-thumb { background:var(--border); border-radius:4px; }
.news-scroll::-webkit-scrollbar { width:4px; }
.news-scroll::-webkit-scrollbar-thumb { background:var(--border); border-radius:4px; }

/* Sparkline row */
.spark-row { display:flex; align-items:center; gap:10px; padding:8px 0; border-bottom:1px solid #111; }
.spark-row:last-child { border-bottom:none; }
.spark-label { width:80px; font-size:10px; color:var(--dim); text-transform:uppercase; letter-spacing:1px; flex-shrink:0; }
.spark-val { font-size:13px; font-weight:700; width:55px; text-align:right; flex-shrink:0; }

/* Section separator */
.section-head { font-size:10px; letter-spacing:3px; color:var(--dim); text-transform:uppercase;
  margin:24px 0 12px; padding-bottom:6px; border-bottom:1px solid var(--border); }

/* Live indicator */
.live-dot { display:inline-block; width:7px; height:7px; background:var(--green);
  border-radius:50%; margin-right:4px; animation:pulse 2s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }

/* Win rate bar */
.wr-bar  { background:#111; border-radius:4px; height:6px; overflow:hidden; margin-top:4px; }
.wr-fill { height:100%; border-radius:4px; background:linear-gradient(90deg,var(--green),#00d4ff); }

/* Chart Modal */
#chart-modal { 
    display:none; position:fixed; z-index:1000; left:0; top:0; width:100%; height:100%; 
    background:rgba(0,0,0,0.85); backdrop-filter:blur(5px);
}
.modal-content {
    background:var(--panel); margin:5% auto; padding:24px; border:1px solid var(--border);
    width:80%; max-width:900px; border-radius:12px; box-shadow:0 20px 50px rgba(0,0,0,0.5);
}
.chart-controls { display:flex; gap:10px; margin-bottom:20px; }
.chart-btn { 
    background:#1a1a2e; color:#888; border:1px solid var(--border); padding:6px 12px; 
    border-radius:4px; cursor:pointer; font-size:11px; font-weight:700;
}
.chart-btn.active { background:var(--accent); color:#000; border-color:var(--accent); }
.close-modal { float:right; cursor:pointer; font-size:24px; color:var(--dim); }
.close-modal:hover { color:#fff; }

/* Footer */
.footer { text-align:center; color:#2a2a40; font-size:10px; letter-spacing:2px; padding:24px 0; margin-top:10px; }

/* ── Rolling log overlay ── */
@keyframes htLogRoll {
  from { transform: translateY(0); }
  to   { transform: translateY(-50%); }
}
#log-overlay-wrap {
  position: absolute; top:0; right:0;
  width: 40%; height: 100%;
  overflow: hidden;
  pointer-events: none;
  -webkit-mask-image: linear-gradient(to right, transparent 0%, rgba(0,0,0,0.6) 10%, black 24%);
          mask-image: linear-gradient(to right, transparent 0%, rgba(0,0,0,0.6) 10%, black 24%);
}
#log-reel {
  opacity: 0.52;
  font-family: 'SF Mono','Fira Code','Cascadia Code',monospace;
  font-size: 8px;
  color: #00ff88;
  line-height: 1.5;
  white-space: pre;
  padding: 6px 14px 6px 18px;
  will-change: transform;
}

/* ── Thesis hover popup ── */
#thesis-popup {
  position:fixed; z-index:9999; display:none;
  background:#0d0e1a; border:1px solid #00d4ff44;
  border-radius:8px; padding:14px 16px;
  max-width:440px; min-width:180px;
  box-shadow:0 8px 32px rgba(0,0,0,0.85), 0 0 0 1px #00d4ff18;
  pointer-events:none;
}
#thesis-popup .tp-header {
  font-size:9px; letter-spacing:2px; color:#00d4ff;
  text-transform:uppercase; margin-bottom:8px; font-weight:700;
}
#thesis-popup .tp-body {
  font-size:12px; color:#ccc; line-height:1.65;
  white-space:pre-wrap; word-break:break-word;
}
.has-thesis { cursor:default; }
.has-thesis:hover { color:#ddd !important; }
</style>
</head>
<body>
<div class="page">
""" + f"""
<!-- ═══ HEADER ═══ -->
<div style="border-radius:10px 10px 0 0;overflow:hidden;line-height:0;margin-bottom:0;position:relative;">
  <img src="/header-image" style="width:100%;height:auto;display:block;" alt="HighTrade"/>
  <div id="log-overlay-wrap">
    <div id="log-reel"></div>
  </div>
</div>
<div class="header" style="border-radius:0 0 10px 10px;margin-bottom:20px;padding:8px 24px;border-top:none;">
  <div></div>
  <div class="header-meta">
    <span class="live-dot"></span>LIVE &nbsp;&middot;&nbsp;
    Generated: {now_str} &nbsp;&middot;&nbsp;
    Last cycle: {last_cycle}<br/>
    <span style="opacity:0.35;font-size:9px;letter-spacing:2px;">v2.4-INTERACTIVE</span>
  </div>
</div>

<!-- ═══ COMMAND CENTER ═══ -->
<div class="grid-full">
  <div class="panel" style="border-color:var(--accent)44;">
    <div class="panel-title">📡 Command Center &mdash; AI &amp; Execution Control</div>
    <div style="display:grid;grid-template-columns:1fr 300px;gap:20px;">
      
      <!-- Custom Prompt Box -->
      <div>
        <div class="micro-label">CUSTOM AI PROMPT</div>
        <div style="display:flex;gap:10px;margin-bottom:10px;">
          <select id="model-select" style="background:#1a1a2e;color:#ddd;border:1px solid var(--border);padding:5px;border-radius:4px;">
            <option value="reasoning">Gemini 3.1 Pro (Reasoning)</option>
            <option value="balanced">Gemini 3 Flash (Balanced)</option>
            <option value="fast">Gemini 3 Flash (Fast)</option>
            <option value="grok">Grok 4.1 (X-Powered)</option>
          </select>
          <input type="text" id="custom-prompt" placeholder="Ask AI about the market, positions, or specific tickers..."
                 style="flex:1;background:#0a0b14;color:var(--text);border:1px solid var(--border);padding:8px;border-radius:4px;">
          <button onclick="sendPrompt()" style="background:var(--accent);color:#000;border:none;padding:0 20px;border-radius:4px;font-weight:700;cursor:pointer;">SEND</button>
          <button onclick="clearChat()" title="Clear chat history" style="background:#1a1a2e;color:#666;border:1px solid #333;padding:0 14px;border-radius:4px;cursor:pointer;font-size:13px;">&#10005;</button>
        </div>
        <div id="ai-response" style="background:#0a0b14;border:1px solid #111;border-radius:4px;padding:10px;min-height:60px;font-size:12px;color:#aaa;white-space:pre-wrap;">AI response will appear here...</div>
      </div>

      <!-- Quick Control Buttons -->
      <div style="border-left:1px solid #111;padding-left:20px;">
        <div class="micro-label">SYSTEM OVERRIDES</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:5px;">
          <button onclick="sendCommand('/status')" style="background:#1a1a2e;color:var(--accent);border:1px solid var(--accent)44;padding:8px;border-radius:4px;cursor:pointer;">STATUS</button>
          <button onclick="sendCommand('/update')" style="background:#1a1a2e;color:var(--green);border:1px solid var(--green)44;padding:8px;border-radius:4px;cursor:pointer;">RUN CYCLE</button>
          <button onclick="sendCommand('/hold')" style="background:#1a1a2e;color:var(--gold);border:1px solid var(--gold)44;padding:8px;border-radius:4px;cursor:pointer;">HOLD</button>
          <button onclick="sendCommand('/start')" style="background:#1a1a2e;color:var(--green);border:1px solid var(--green)44;padding:8px;border-radius:4px;cursor:pointer;">START</button>
          <button onclick="sendCommand('/briefing')" style="background:#1a1a2e;color:#c084fc;border:1px solid #c084fc44;padding:8px;border-radius:4px;cursor:pointer;">DAILY RPT</button>
          <button onclick="sendCommand('/research')" style="background:#1a1a2e;color:var(--accent);border:1px solid var(--accent)44;padding:8px;border-radius:4px;cursor:pointer;">RESEARCH</button>
          <button onclick="sendCommand('/hunt')" style="background:#1a1a2e;color:#ff8c00;border:1px solid #ff8c0044;padding:8px;border-radius:4px;cursor:pointer;">🦮 HUNT</button>
          <button onclick="sendCommand('/estop')" style="background:#1a1a2e;color:var(--red);border:1px solid var(--red)44;padding:8px;border-radius:4px;cursor:pointer;">E-STOP</button>
        </div>
      </div>

    </div>
  </div>
</div>

<script>
function showToast(msg, ok) {{
    let t = document.getElementById('cmd-toast');
    if (!t) {{
        t = document.createElement('div');
        t.id = 'cmd-toast';
        t.style.cssText = 'position:fixed;bottom:24px;right:24px;padding:10px 18px;border-radius:6px;font-size:12px;font-family:monospace;z-index:9999;max-width:420px;word-wrap:break-word;transition:opacity .4s;pointer-events:none;';
        document.body.appendChild(t);
    }}
    t.style.background = ok ? '#0d1f14' : '#1f0d0d';
    t.style.border = '1px solid ' + (ok ? '#00ff8855' : '#ff444455');
    t.style.color = ok ? '#00ff88' : '#ff4444';
    t.style.opacity = '1';
    t.innerText = msg;
    clearTimeout(t._hide);
    t._hide = setTimeout(() => {{ t.style.opacity = '0'; }}, 6000);
}}

async function sendCommand(cmd) {{
    const btn = event?.target;
    const originalText = btn?.innerText;
    if (btn) {{ btn.innerText = 'WAIT...'; btn.disabled = true; }}

    try {{
        const resp = await fetch('/api/command', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ command: cmd }})
        }});
        const data = await resp.json();
        const ok  = data.ok !== false;
        const msg = data.message || data.error || 'Done';

        // STATUS: show structured data in AI response pane
        if (cmd === '/status' && data.data) {{
            const d = data.data;
            const lines = Object.entries(d).map(([k,v]) => k.padEnd(18) + v).join('\\n');
            const output = document.getElementById('ai-response');
            if (output) {{
                output.style.color = 'var(--accent)';
                output.innerText = '[ SYSTEM STATUS ]\\n' + lines;
            }}
            showToast('STATUS loaded', true);
        }} else {{
            showToast((ok ? '✅ ' : '❌ ') + msg, ok);
        }}
    }} catch (e) {{
        showToast('❌ ' + e, false);
    }} finally {{
        if (btn) {{ btn.innerText = originalText; btn.disabled = false; }}
    }}
}}

async function sendPrompt() {{
    const promptInput = document.getElementById('custom-prompt');
    const prompt = promptInput.value;
    const model = document.getElementById('model-select').value;
    const output = document.getElementById('ai-response');
    
    if (!prompt) return;
    
    // Append user message to local log
    const timestamp = new Date().toLocaleTimeString();
    appendChat('USER', prompt, timestamp);
    promptInput.value = '';
    
    output.innerText = '🤖 Thinking...';
    output.style.color = 'var(--accent)';
    
    try {{
        const resp = await fetch('/api/prompt', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ prompt, model }})
        }});
        const data = await resp.json();
        if (data.ok) {{
            output.innerText = data.response;
            output.style.color = '#ddd';
            appendChat('AI (' + model + ')', data.response, new Date().toLocaleTimeString());
        }} else {{
            output.innerText = '❌ Error: ' + data.message;
            output.style.color = 'var(--red)';
        }}
    }} catch (e) {{
        output.innerText = '❌ Failed to connect to server';
        output.style.color = 'var(--red)';
    }}
}}

function appendChat(role, text, time) {{
    const log = JSON.parse(localStorage.getItem('hightrade_chat_log') || '[]');
    log.push({{ role, text, time }});
    // Keep last 20 messages
    if (log.length > 20) log.shift();
    localStorage.setItem('hightrade_chat_log', JSON.stringify(log));
    renderChat();
}}

function renderChat() {{
    const output = document.getElementById('ai-response');
    if (!output) return;
    const log = JSON.parse(localStorage.getItem('hightrade_chat_log') || '[]');
    if (log.length === 0) {{
        output.innerText = 'AI response will appear here...';
        return;
    }}
    
    output.innerHTML = log.map(m => 
        `<div style="margin-bottom:8px; border-bottom:1px solid #ffffff05; padding-bottom:4px;">
            <span style="color:var(--dim); font-size:9px;">[${{m.time}}]</span> 
            <b style="color:${{m.role === 'USER' ? 'var(--accent)' : 'var(--green)'}}; font-size:10px;">${{m.role}}:</b> 
            <div style="margin-top:2px; color:#ccc; font-size:11px;">${{m.text}}</div>
        </div>`
    ).join('');
    // Scroll to bottom
    output.scrollTop = output.scrollHeight;
}}

function clearChat() {{
    localStorage.removeItem('hightrade_chat_log');
    renderChat();
}}

// Initialize chat on load
setTimeout(renderChat, 100);

// ── Rolling log overlay ──────────────────────────────────────────────────────
(function() {{
  var LOG_POLL_MS  = 12000;   // re-fetch every 12 s
  var SECS_PER_LINE = 1.7;    // scroll speed: seconds per line of log text
  var MIN_DURATION  = 36;     // minimum scroll cycle seconds
  var _currentLines = [];

  function _applyRoll(reel, lines) {{
    if (!lines || !lines.length) return;
    _currentLines = lines;

    // Double the content for seamless infinite loop
    var content = lines.join('\\n');
    reel.textContent = content + '\\n\\n' + content;

    // Duration proportional to content length so scroll speed is constant
    var dur = Math.max(MIN_DURATION, lines.length * SECS_PER_LINE);

    // Reset animation cleanly
    reel.style.animation = 'none';
    void reel.offsetHeight;  // force reflow
    reel.style.animation = 'htLogRoll ' + dur + 's linear infinite';
  }}

  async function _fetchAndRoll() {{
    var reel = document.getElementById('log-reel');
    if (!reel) return;
    try {{
      var resp = await fetch('/api/logs');
      if (!resp.ok) return;
      var data = await resp.json();
      if (!data.ok || !data.lines || !data.lines.length) return;

      // Only re-render if content actually changed (avoid animation jank)
      var newSig = data.lines.slice(-3).join('|');
      var oldSig = _currentLines.slice(-3).join('|');
      if (newSig !== oldSig) {{
        _applyRoll(reel, data.lines);
      }}
    }} catch(e) {{ /* log endpoint not available yet — silently ignore */ }}
  }}

  // Initial load after a short delay (let page settle)
  setTimeout(_fetchAndRoll, 800);
  setInterval(_fetchAndRoll, LOG_POLL_MS);
}})();

async function approveTicker(ticker) {{
    if (!confirm('Send ' + ticker + ' to Acquisition Pipeline for deep analysis?')) return;
    
    try {{
        const resp = await fetch('/api/approve', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ ticker: ticker }})
        }});
        const data = await resp.json();
        if (data.ok) {{
            alert(data.message);
            location.reload();
        }} else {{
            alert('Error: ' + data.message);
        }}
    }} catch (e) {{
        alert('Error: ' + e);
    }}
}}

async function rejectTicker(ticker) {{
    if (!confirm('Are you sure you want to ignore ' + ticker + '?')) return;
    
    try {{
        const resp = await fetch('/api/reject', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ ticker: ticker }})
        }});
        const data = await resp.json();
        if (data.ok) location.reload();
        else alert('Error: ' + data.message);
    }} catch (e) {{
        alert('Error: ' + e);
    }}
}}

// Handle Enter key in prompt box
document.getElementById('custom-prompt')?.addEventListener('keypress', function (e) {{
    if (e.key === 'Enter') sendPrompt();
}});
</script>

<!-- ═══ ROW 1: DEFCON · PORTFOLIO · MACRO ═══ -->
<div class="grid-top">

  <!-- DEFCON PANEL -->
  <div class="panel" style="display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;border-color:{dc_color}44;">
    <div class="panel-title" style="justify-content:center;">System Status</div>
    <div class="defcon-num" style="color:{dc_color};">D{defcon}</div>
    <div class="defcon-label" style="color:{dc_color};">{dc_label}</div>
    <div class="defcon-row">{defcon_blocks}</div>
    <div style="margin-top:16px;display:grid;grid-template-columns:1fr 1fr;gap:8px;width:100%;">
      <div class="stat">
        <div class="stat-label">Signal Score</div>
        <div class="stat-value" style="font-size:18px;color:#00d4ff;">{signal_score:.1f}</div>
      </div>
      <div class="stat">
        <div class="stat-label">VIX</div>
        <div class="stat-value" style="font-size:18px;color:{'#ff8c00' if vix > 20 else '#00ff88'};">{vix:.2f}</div>
      </div>
      <div class="stat">
        <div class="stat-label">10Y Yield</div>
        <div class="stat-value" style="font-size:16px;color:#aaa;">{bond:.2f}%</div>
      </div>
      <div class="stat">
        <div class="stat-label">2Y Yield</div>
        <div class="stat-value" style="font-size:16px;color:#aaa;">{rate_2y:.2f}%</div>
      </div>
    </div>
  </div>

  <!-- PORTFOLIO PANEL -->
  <div class="panel">
    <div class="panel-title">Portfolio</div>
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-label">Account Value</div>
        <div class="stat-value" style="font-size:22px;color:{'#00ff88' if account_value >= total_capital else '#ff4444'};">${account_value:,.0f}</div>
        <div class="stat-sub">Base Capital: $100,000</div>
      </div>
      <div class="stat">
        <div class="stat-label">Total P&amp;L</div>
        <div class="stat-value" style="font-size:22px;color:{pnl_color(total_pnl)};">{fmt_dollar(total_pnl)}</div>
        <div class="stat-sub">{fmt_pct(total_pnl_pct)} on capital</div>
      </div>
      <div class="stat">
        <div class="stat-label">Realized P&amp;L</div>
        <div class="stat-value" style="font-size:18px;color:{pnl_color(realized)};">{fmt_dollar(realized)}</div>
        <div class="stat-sub">{wins}W / {losses}L closed</div>
      </div>
      <div class="stat">
        <div class="stat-label">Unrealized P&amp;L</div>
        <div class="stat-value" style="font-size:18px;color:{pnl_color(unrealized)};">{fmt_dollar(unrealized)}</div>
        <div class="stat-sub">{len(positions)} open position{'s' if len(positions) != 1 else ''}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Cash Available</div>
        <div class="stat-value" style="font-size:18px;">${cash:,.0f}</div>
        <div class="stat-sub">Deployed: ${deployed:,.0f}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Win Rate</div>
        <div class="stat-value" style="font-size:18px;color:{'#00ff88' if win_rate >= 50 else '#ff8c00'};">{win_rate:.0f}%</div>
        <div class="wr-bar"><div class="wr-fill" style="width:{win_rate}%;"></div></div>
      </div>
    </div>
  </div>

  <!-- MACRO PANEL -->
  <div class="panel">
    <div class="panel-title">Macro Environment</div>
    <div class="macro-grid">
      <div style="display:flex;flex-direction:column;align-items:center;">
        {macro_ring}
        <div style="font-size:9px;color:#555;margin-top:4px;text-align:center;">{macro_supportive}</div>
      </div>
      <div>
        <div class="macro-row"><span style="color:#888;">Fed Funds</span><span>{float(macro.get('fed_funds_rate') or 0):.2f}%</span></div>
        <div class="macro-row"><span style="color:#888;">Yield Curve</span><span style="color:{yc_color};">{yc_spread:+.2f}%</span></div>
        <div class="macro-row"><span style="color:#888;">Unemployment</span><span>{float(macro.get('unemployment_rate') or 0):.1f}%</span></div>
        <div class="macro-row"><span style="color:#888;">HY Spreads</span><span style="color:{hy_color};">{hy_bps:.0f} bps</span></div>
        <div class="macro-row"><span style="color:#888;">Cons. Sentiment</span><span style="color:{cs_color};">{cs_val:.1f}</span></div>
        <div class="macro-row"><span style="color:#888;">M2 YoY</span><span>{float(macro.get('m2_yoy_change') or 0):+.2f}%</span></div>
      </div>
    </div>
    <div style="margin-top:10px;border-top:1px solid #111;padding-top:8px;">
      {macro_sig_pills}
    </div>
  </div>

</div>

<!-- ═══ SIGNAL SPARKLINES ═══ -->
<div class="grid-full">
  <div class="panel">
    <div class="panel-title">Signal History &mdash; Last {len(sig_history)} Monitoring Cycles</div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;">
      <div class="spark-row">
        <div class="spark-label">VIX</div>
        {vix_spark}
        <div class="spark-val" style="color:#ff8c00;">{vix_last}</div>
      </div>
      <div class="spark-row">
        <div class="spark-label">News Score</div>
        {news_spark}
        <div class="spark-val" style="color:#c084fc;">{news_last}</div>
      </div>
      <div class="spark-row">
        <div class="spark-label">Signal</div>
        {sig_spark}
        <div class="spark-val" style="color:#00d4ff;">{sig_last}</div>
      </div>
    </div>
  </div>
</div>

<!-- ═══ DAILY INTELLIGENCE CONSENSUS ═══ -->
<!-- ═══ DAILY ANALYSIS SCHEDULE ═══ -->
<div class="section-head">&#128197; Daily Analysis Schedule &mdash; {_today_str}</div>

<div class="grid-three" style="margin-bottom:0;">
  {morning_card}
  {midday_card}
  {close_card}
</div>

<!-- ═══ CLOSE DEEP DIVE — FULL ANALYSIS ═══ -->
<div class="section-head" style="margin-top:18px;">&#129504; Close Deep Dive &mdash; {close_date_str}</div>

<div class="grid-mid">
  {reasoning_card}
  {grok_card}
</div>

<!-- ═══ PORTFOLIO POSITIONS ═══ -->
<div class="section-head">&#128202; Portfolio Positions</div>

<div class="grid-full">
  <div class="panel">
    <div class="panel-title">Open Positions</div>
    <div class="scroll-wrap">
      <table>
        <thead><tr>
          <th>Symbol</th><th>Shares</th><th>Entry</th><th>Current</th>
          <th>Mkt Value</th><th>Unrlzd P&amp;L</th><th>Return</th><th>Stop</th><th>TP1</th><th>DEFCON@Entry</th>
        </tr></thead>
        <tbody>{open_rows}</tbody>
      </table>
    </div>
  </div>
</div>

<div class="grid-full">
  <div class="panel" style="border-color:#ff444422;">
    <div class="panel-title">🎯 Exit Queue &mdash; Open Position Management
      <span style="font-size:10px;color:#666;font-weight:400;margin-left:10px;">
        🔴 stop &lt;3% &nbsp; 🟡 stop &lt;8% &nbsp; 🟢 near TP &nbsp;
        <span style="background:#1a2a1a;color:#00ff88;padding:1px 5px;border-radius:2px;font-size:9px;">🎯 ANALYST</span> = analyst exit framework attached
      </span>
    </div>
    <div class="scroll-wrap">
      <table>
        <thead><tr>
          <th>Symbol</th><th>Entry → Current</th><th>Unrealized P&amp;L</th>
          <th>Stop Loss</th><th>TP1</th><th>TP2</th>
          <th>Hold</th><th>Thesis</th><th>Framework</th>
        </tr></thead>
        <tbody>{exit_queue_rows}</tbody>
      </table>
    </div>
  </div>
</div>

<div class="grid-full">
  <div class="panel">
    <div class="panel-title">Closed Trades</div>
    <div class="scroll-wrap">
      <table>
        <thead><tr>
          <th>Symbol</th><th>Shares</th><th>Entry</th><th>Exit</th>
          <th>P&amp;L</th><th>Return</th><th>Exit Reason</th><th>Close Date</th>
        </tr></thead>
        <tbody>{closed_rows}</tbody>
      </table>
    </div>
  </div>
</div>

<!-- ═══ ACQUISITION PIPELINE ═══ -->
<div class="section-head">&#127919; Acquisition Pipeline</div>

<div class="grid-full">
  <div class="panel">
    <div class="panel-title">Watchlist &mdash; Research Queue</div>
    <div class="scroll-wrap">
      <table>
        <thead><tr>
          <th>Ticker</th><th>Source</th><th>Conf</th><th>Regime</th><th>Thesis</th><th>Status</th><th>Added</th>
        </tr></thead>
        <tbody>{wl_rows}</tbody>
      </table>
    </div>
  </div>
</div>

<!-- ═══ ACTIVE CONDITIONALS ═══ -->
<div class="grid-full">
  <div class="panel">
    <div class="panel-title">Active Conditionals &mdash; Entry Queue &nbsp;<span style="font-size:10px;color:#666;font-weight:400;">🔥 hot (&ge;75) &nbsp; 🟡 warm (&ge;40) &nbsp; ⬜ cold</span></div>
    <div class="scroll-wrap">
      <table>
        <thead><tr>
          <th>Ticker</th><th>Attention</th><th>Conf</th><th>Target</th><th>Stop</th><th>TP1</th><th>Tag</th><th>Thesis</th>
        </tr></thead>
        <tbody>{cond_rows}</tbody>
      </table>
    </div>
  </div>
</div>

<!-- ═══ NEWS · CONGRESSIONAL ═══ -->
<div class="grid-mid">

  <div class="panel">
    <div class="panel-title">News Intelligence &mdash; Recent Signals</div>
    <div class="news-scroll">{news_items}</div>
  </div>

  <div class="panel">
    <div class="panel-title">Congressional Intelligence</div>
    <div style="margin-bottom:14px;">
      <div class="micro-label" style="margin-bottom:6px;">Cluster Buy Signals</div>
      <table>
        <thead><tr><th>Ticker</th><th>Strength</th><th>Count</th><th>$ Volume</th><th>Flag</th><th>Date</th></tr></thead>
        <tbody>{cong_cl_rows}</tbody>
      </table>
    </div>
    <div>
      <div class="micro-label" style="margin-bottom:6px;">Recent Disclosures</div>
      <div class="scroll-wrap" style="max-height:180px;">
        <table>
          <thead><tr><th>Trade</th><th>Politician</th><th>Party &bull; Chamber</th><th>Amount</th><th>Date</th></tr></thead>
          <tbody>{cong_tr_rows}</tbody>
        </table>
      </div>
    </div>
  </div>

</div>

<!-- ═══ GROK HOUND ═══ -->
<div class="grid-full">
  <div class="panel" style="border-color:#ff8c0044;">
    <div class="panel-title">🐕 Grok Hound &mdash; High-Alpha &amp; Momentum Opportunities</div>
    <div class="scroll-wrap">
      <table>
        <thead><tr>
          <th>Ticker</th><th>Alpha Score</th><th>Alpha Thesis</th><th>Suggestion</th><th>Found</th><th>Action</th>
        </tr></thead>
        <tbody>{hound_rows}</tbody>
      </table>
    </div>
  </div>
</div>

<!-- ═══ SYSTEM ARCHITECTURE ═══ -->
<div class="section-head">&#127959;&#65039; System Architecture</div>

<div class="grid-three">

  <div class="panel">
    <div class="panel-title">&#128269; Research Pipeline</div>
    <div style="display:flex;flex-direction:column;gap:8px;">
      <div class="stat">
        <div class="stat-label">News Engine</div>
        <div style="color:#00ff88;font-size:11px;">&#9679; ACTIVE &mdash; 15-min cycles &middot; dedup-gated Gemini</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">5-component scoring: sentiment &middot; concentration &middot; urgency &middot; confidence &middot; specificity &middot; Flash+Pro only fires on new articles or breaking</div>
      </div>
      <div class="stat">
        <div class="stat-label">FRED Macro Tracker</div>
        <div style="color:#00ff88;font-size:11px;">&#9679; ACTIVE &mdash; Score {macro_score_val:.0f}/100 &middot; every ~60 min</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">9 indicators: yield curve &middot; fed funds &middot; unemployment &middot; M2 &middot; HY spreads &middot; consumer sentiment &middot; DEFCON modifier</div>
      </div>
      <div class="stat">
        <div class="stat-label">Congressional Tracker</div>
        <div style="color:{'#ffd700' if not cong_clusters else '#00ff88'};font-size:11px;">{'&#9888;&#65039; S3 intermittent &mdash; Capitol Trades fallback' if not cong_clusters else '&#9679; ACTIVE &mdash; every ~60 min'}</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">House + Senate disclosures &middot; cluster detection (3+ politicians/30 days) &middot; bipartisan signal weighting</div>
      </div>
      <div class="stat">
        <div class="stat-label">Acquisition Pipeline</div>
        <div style="color:#00ff88;font-size:11px;">&#9679; ACTIVE &mdash; hourly verifier &middot; DEFCON 1-2: every 15 min</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">Researcher &rarr; Analyst (3.1 Pro → 3 Flash → 2.5 Pro) &rarr; Verifier (Flash · hourly / 15-min at DEFCON 1-2) &rarr; Conditionals &middot; deep checks: 9 AM · 12:30 PM · 4:30 PM</div>
      </div>
      <div class="stat">
        <div class="stat-label">🦮 Grok Hound &mdash; Alpha Scanner</div>
        <div style="color:#ff8c00;font-size:11px;">&#9679; grok-4-1-fast-reasoning &middot; X.com momentum feed &middot; hourly cycles</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">5-signal scoring (sentiment &middot; concentration &middot; urgency &middot; confidence &middot; specificity) &middot; auto-promotes &ge;75 alpha &middot; feeds researcher &rarr; analyst pipeline</div>
        <div style="color:#555;font-size:10px;margin-top:2px;">Last run: {hound_last_str}</div>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title">&#129504; Intelligence Layer</div>
    <div style="display:flex;flex-direction:column;gap:8px;">
      <div class="stat">
        <div class="stat-label">Gemini 3.1 Pro Preview &mdash; Reasoning Tier ★ PRIMARY</div>
        <div style="color:#00ff88;font-size:11px;">&#9679; gemini-3.1-pro-preview &middot; thinking=-1 (dynamic) &middot; OAuth CLI</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">4:30 PM deep daily briefing &middot; acquisition analyst &middot; pre-purchase &amp; exit gates &middot; 16k output &middot; 250/d &middot; 25 RPM</div>
      </div>
      <div class="stat">
        <div class="stat-label">Gemini 3 Flash Preview &mdash; Fast Tier ★ PRIMARY</div>
        <div style="color:#00ff88;font-size:11px;">&#9679; gemini-3-flash-preview &middot; thinking=8k &middot; OAuth CLI</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">Per-cycle news triage &middot; briefings &middot; verifier &middot; Step-1 fallback for Pro &middot; 1500/d &middot; 120 RPM</div>
      </div>
      <div class="stat">
        <div class="stat-label">Gemini 2.5 Pro &mdash; CLI Fallback</div>
        <div style="color:#ffb300;font-size:11px;">&#9679; gemini-2.5-pro &middot; thinking=8k &middot; OAuth CLI &middot; Step-2 fallback</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">Activates when Flash unavailable via CLI &middot; 1500/d &middot; 120 RPM</div>
      </div>
      <div class="stat">
        <div class="stat-label">Gemini 3.1 Flash Lite &mdash; REST Fallback</div>
        <div style="color:#7eb8f7;font-size:11px;">&#9679; gemini-3.1-flash-lite-preview &middot; no thinking &middot; API Key (REST)</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">Catches all CLI failures &middot; separate quota pool from OAuth &middot; 1500/d &middot; 120 RPM</div>
      </div>
      <div class="stat">
        <div class="stat-label">Grok 4.1 &mdash; Primary Deep Dive</div>
        <div style="color:#00ff88;font-size:11px;">&#9679; grok-4-1-fast-reasoning &middot; X-Powered &middot; Native live search</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">&#120143; Elevated signal deep analysis &middot; Real-time data gap resolution &middot; Daily close briefing</div>
      </div>
{grok_usage_html}
{gemini_quota_html}
{stream_html}
      <div class="stat">
        <div class="stat-label">Auth &amp; Fallback Chain</div>
        <div style="color:#7eb8f7;font-size:11px;">&#128274; OAuth CLI primary &middot; REST API key fallback &middot; auto-downgrade at 90%</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">Fallback: 3.1 Pro &rarr; 3 Flash (CLI) &rarr; Flash Lite (REST) &rarr; 2.5 Pro (REST) &middot; RPM pacing per model &middot; thread-safe quota tracking</div>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title">&#129302; Broker / Execution</div>
    <div style="display:flex;flex-direction:column;gap:8px;">
      <div class="stat">
        <div class="stat-label">DEFCON System</div>
        <div style="color:{dc_color};font-size:11px;">&#9679; D{defcon} &mdash; {dc_label} &middot; Macro modifier: {defcon_mod_val:+.1f}</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">Signal-driven 1&ndash;5 scale &middot; news + yield + VIX + macro composite &middot; escalation AND de-escalation tracked</div>
      </div>
      <div class="stat">
        <div class="stat-label">Broker Agent &mdash; {broker_mode.upper()}</div>
        <div style="color:#00ff88;font-size:11px;">&#9679; Buys DEFCON 1&ndash;2 &middot; conditional entries &middot; autonomous exits</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">Watch tags: breakout &middot; momentum &middot; mean-reversion &middot; defensive-hedge &middot; macro-hedge &middot; earnings-play &middot; rebound</div>
      </div>
      <div class="stat">
        <div class="stat-label">Pre-Purchase Gate</div>
        <div style="color:#00ff88;font-size:11px;">&#9679; Pro 3 veto at trigger time &middot; fail-open</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">Live DEFCON + news score + macro score + VIX &middot; vetoed entries stay active and retry next cycle</div>
      </div>
      <div class="stat">
        <div class="stat-label">Position Sizing &amp; Commands</div>
        <div style="color:#aaa;font-size:11px;">Base $10K &middot; Min $3K &middot; Max $20K &middot; {len(positions)} open &middot; ${deployed:,.0f} deployed</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">Slack: /buy &middot; /sell &middot; /hold &middot; /briefing &middot; /status &middot; IPC file bridge &middot; full override</div>
      </div>
    </div>
  </div>

</div>

<!-- ═══ DATA LAYER ═══ -->
<div style="margin-top:16px;">
  <div class="panel" style="border-color:#2a2a3a;">
    <div class="panel-title" style="color:#888;">&#128451; Data Layer &mdash; SQLite WAL &middot; {db_rows} active rows across 6 tables &middot; 5 performance indexes &middot; 3 SQL views</div>
    <div style="display:flex;gap:24px;flex-wrap:wrap;margin-top:6px;">
      <div style="color:#555;font-size:10px;">&#128202; signal_monitoring &mdash; 15-min cycle snapshots</div>
      <div style="color:#555;font-size:10px;">&#128240; news_signals &mdash; scored article batches + Flash JSON</div>
      <div style="color:#555;font-size:10px;">&#129504; gemini_analysis &mdash; Flash + Pro reasoning records</div>
      <div style="color:#555;font-size:10px;">&#128200; macro_indicators &mdash; FRED snapshots</div>
      <div style="color:#555;font-size:10px;">&#128203; daily_briefings &mdash; morning &middot; midday &middot; close synthesis</div>
      <div style="color:#555;font-size:10px;">&#127919; conditional_tracking &mdash; watch-tagged entry conditionals</div>
      <div style="color:#555;font-size:10px;">&#128065;&#65039; v_active_positions &middot; v_active_conditionals &middot; v_daily_signal_summary</div>
    </div>
  </div>
</div>

<div class="footer">HIGHTRADE &middot; PAPER TRADING &middot; NOT FINANCIAL ADVICE &middot; {now_str}</div>
</div>

<!-- ═══ CHART MODAL ═══ -->
<div id="chart-modal" onclick="if(event.target==this)closeChart()">
    <div class="modal-content">
        <span class="close-modal" onclick="closeChart()">&times;</span>
        <div id="modal-header" style="margin-bottom:20px;">
            <h2 id="modal-ticker" style="color:var(--accent); letter-spacing:2px;">TICKER</h2>
            <div id="modal-price" style="font-size:24px; font-weight:700;">$0.00</div>
            <div id="modal-change" style="font-size:14px;">+0.00%</div>
        </div>
        <div class="chart-controls">
            <button class="chart-btn" id="btn-1d" onclick="updateChart('1d')">1D</button>
            <button class="chart-btn" id="btn-5d" onclick="updateChart('5d')">5D</button>
            <button class="chart-btn active" id="btn-1mo" onclick="updateChart('1mo')">1M</button>
            <button class="chart-btn" id="btn-1y" onclick="updateChart('1y')">1Y</button>
        </div>
        <div style="height:400px; width:100%;">
            <canvas id="tickerChart"></canvas>
        </div>
    </div>
</div>

<script>
let currentTicker = '';
let priceChart = null;

// --- CHART LOGIC ---
async function showChart(ticker) {{
    currentTicker = ticker;
    document.getElementById('chart-modal').style.display = 'block';
    updateChart('1mo');
}}

function closeChart() {{
    document.getElementById('chart-modal').style.display = 'none';
}}

async function updateChart(period) {{
    document.querySelectorAll('.chart-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('btn-' + period).classList.add('active');
    
    try {{
        const resp = await fetch(`/api/history/${{currentTicker}}?period=${{period}}`);
        const data = await resp.json();
        if (!data.ok) throw new Error(data.message);
        
        document.getElementById('modal-ticker').innerText = data.ticker;
        document.getElementById('modal-price').innerText = '$' + data.current.toLocaleString();
        const changeEl = document.getElementById('modal-change');
        changeEl.innerText = (data.change_pct >= 0 ? '+' : '') + data.change_pct + '%';
        changeEl.style.color = data.change_pct >= 0 ? 'var(--green)' : 'var(--red)';
        
        const ctx = document.getElementById('tickerChart').getContext('2d');
        if (priceChart) priceChart.destroy();
        const chartColor = data.change_pct >= 0 ? '#00ff88' : '#ff4444';
        
        priceChart = new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: data.labels,
                datasets: [{{
                    data: data.prices,
                    borderColor: chartColor,
                    borderWidth: 2,
                    pointRadius: 0,
                    tension: 0.1,
                    fill: true,
                    backgroundColor: chartColor + '11'
                }}]
            }},
            options: {{
                responsive: true, maintainAspectRatio: false,
                plugins: {{ legend: {{ display: false }} }},
                scales: {{
                    x: {{ display: false }},
                    y: {{ grid: {{ color: '#1e2040' }}, ticks: {{ color: '#64748b' }} }}
                }},
                interaction: {{ intersect: false, mode: 'index' }}
            }}
        }});
    }} catch (e) {{ console.error(e); }}
}}

// --- CHAT LOGIC ---
async function sendPrompt() {{
    const promptInput = document.getElementById('custom-prompt');
    const prompt = promptInput.value;
    const model = document.getElementById('model-select').value;
    const output = document.getElementById('ai-response');
    if (!prompt) return;
    
    appendChat('USER', prompt, new Date().toLocaleTimeString());
    promptInput.value = '';
    output.innerText = '🤖 Thinking...';
    
    try {{
        const resp = await fetch('/api/prompt', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ prompt, model }})
        }});
        const data = await resp.json();
        if (data.ok) {{
            appendChat('AI (' + model + ')', data.response, new Date().toLocaleTimeString());
        }} else {{
            output.innerText = '❌ Error: ' + data.message;
        }}
    }} catch (e) {{ output.innerText = '❌ Connection failed'; }}
}}

// ── Thesis hover popup ──────────────────────────────────────────────────────
(function() {{
  var popup = document.createElement('div');
  popup.id = 'thesis-popup';
  popup.innerHTML = '<div class="tp-header"></div><div class="tp-body"></div>';
  document.body.appendChild(popup);

  var tpHead = popup.querySelector('.tp-header');
  var tpBody = popup.querySelector('.tp-body');

  document.addEventListener('mouseover', function(e) {{
    var cell = e.target.closest('[data-thesis]');
    if (!cell) {{ popup.style.display = 'none'; return; }}
    var text = cell.dataset.thesis;
    if (!text || text === '\u2014' || text === '') {{ return; }}
    var ticker = cell.dataset.ticker || '';
    tpHead.textContent = ticker ? ticker + ' \u2014 THESIS' : 'THESIS';
    tpBody.textContent = text;
    popup.style.display = 'block';
  }});

  document.addEventListener('mousemove', function(e) {{
    if (popup.style.display === 'none') return;
    var x = e.clientX + 18;
    var y = e.clientY + 14;
    var pw = popup.offsetWidth;
    var ph = popup.offsetHeight;
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    if (x + pw > vw - 12) x = e.clientX - pw - 18;
    if (y + ph > vh - 12) y = e.clientY - ph - 14;
    popup.style.left = x + 'px';
    popup.style.top  = y + 'px';
  }});

  document.addEventListener('mouseout', function(e) {{
    var from = e.target.closest('[data-thesis]');
    var to   = e.relatedTarget && e.relatedTarget.closest ? e.relatedTarget.closest('[data-thesis]') : null;
    if (from && !to) popup.style.display = 'none';
  }});
}})();

</script>
</body>
</html>"""

    return html


# ─── HTML generation helper (used by both CLI and Flask) ─────────────────────

def generate_dashboard_html():
    """Fetch all data and build HTML. Called on every request in server mode."""
    status    = fetch_system_status()
    positions = fetch_open_positions()
    closed    = fetch_closed_trades()
    stats     = fetch_portfolio_stats()
    briefings = fetch_daily_briefings()
    macro     = fetch_macro()
    watchlist = fetch_acquisition_watchlist()
    sig_hist  = fetch_signal_history()
    news      = fetch_recent_news()
    cong_tr, cong_cl = fetch_congressional()
    hound_candidates = fetch_hound_candidates()
    hound_last_run   = fetch_hound_last_run()
    conditionals     = fetch_active_conditionals()
    gemini_usage     = fetch_gemini_usage()
    grok_usage       = fetch_grok_usage()
    exit_queue       = fetch_exit_queue()
    stream_health    = fetch_stream_health()
    return build_html(status, positions, closed, stats, briefings, macro,
                      watchlist, sig_hist, news, cong_cl, cong_tr, hound_candidates, hound_last_run,
                      conditionals=conditionals, gemini_usage=gemini_usage, grok_usage=grok_usage,
                      exit_queue=exit_queue, stream_health=stream_health)


# ─── Flask server ─────────────────────────────────────────────────────────────

def run_server(host='0.0.0.0', port=5055):
    try:
        from flask import Flask, Response, request as flask_request
    except ImportError:
        print("Flask not installed. Run:  pip install flask")
        sys.exit(1)

    import socket
    app = Flask(__name__)

    # Detect local IP for share link
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = 'localhost'

    @app.route('/header-image')
    def header_image():
        from flask import send_file as _send_file
        img_path = SCRIPT_DIR / 'highTRADE.jpeg'
        return _send_file(img_path, mimetype='image/jpeg', max_age=3600)

    @app.route('/')
    def dashboard():
        html = generate_dashboard_html()
        # Inject scroll-preserving JS refresh (replaces meta http-equiv="refresh")
        refresh = flask_request.args.get('refresh', '60')
        try:
            refresh_secs = int(refresh)
        except ValueError:
            refresh_secs = 60
        if refresh_secs > 0:
            soft_refresh_js = f"""
<script>
(function() {{
  var _refreshMs = {refresh_secs * 1000};
  var _timer = setInterval(function() {{
    var sy = window.scrollY;
    sessionStorage.setItem('ht_scroll_y', sy);
    window.location.reload();
  }}, _refreshMs);
  // On load restore scroll position (set before reload)
  var _saved = sessionStorage.getItem('ht_scroll_y');
  if (_saved !== null) {{
    window.scrollTo(0, parseInt(_saved, 10));
    sessionStorage.removeItem('ht_scroll_y');
  }}
  // Expose manual soft-reload helper for buttons etc.
  window._htSoftReload = function() {{
    sessionStorage.setItem('ht_scroll_y', window.scrollY);
    window.location.reload();
  }};
}})();
</script>"""
            html = html.replace('</body>', soft_refresh_js + '\n</body>', 1)
        return Response(html, mimetype='text/html')

    @app.route('/api/command', methods=['POST'])
    def handle_command():
        try:
            data = flask_request.get_json()
            cmd = data.get('command')
            if not cmd: return {'ok': False, 'message': 'Missing command'}, 400
            
            # Send to orchestrator via IPC
            from hightrade_cmd import send_command
            resp = send_command(cmd)
            return resp
        except Exception as e:
            return {'ok': False, 'message': str(e)}, 500

    @app.route('/api/approve', methods=['POST'])
    def handle_approve():
        try:
            data = flask_request.get_json()
            ticker = data.get('ticker')
            if not ticker: return {'ok': False, 'message': 'Missing ticker'}, 400
            
            with _conn() as db:
                # 1. Fetch the Hound's report
                row = db.execute("""
                    SELECT alpha_score, why_next, signals, risks, action_suggestion 
                    FROM grok_hound_candidates WHERE ticker = ?
                """, (ticker,)).fetchone()
                
                if not row:
                    return {'ok': False, 'message': 'Candidate data not found'}, 404
                
                hound_data = dict(row)
                
                # 2. Insert into acquisition_watchlist (Procurement)
                # status='pending' triggers the next Acquisition Researcher cycle
                # Build a rich signals note for context
                signals_raw = hound_data['signals'] or ''
                try:
                    sig_list = json.loads(signals_raw) if signals_raw.startswith('[') else []
                    signals_note = '; '.join(sig_list[:3]) if sig_list else signals_raw
                except Exception:
                    signals_note = signals_raw
                action = (hound_data['action_suggestion'] or '').upper().replace('_', ' ')
                notes_text = f"[{action}] {signals_note}" if signals_note else f"[{action}]"

                db.execute("""
                    INSERT OR REPLACE INTO acquisition_watchlist
                    (date_added, ticker, source, model_confidence, biggest_risk,
                     biggest_opportunity, entry_conditions, notes, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    datetime.now().strftime('%Y-%m-%d'),
                    ticker,
                    'grok_hound',
                    float(hound_data['alpha_score'] or 0) / 100.0,
                    hound_data['risks'],
                    hound_data['why_next'],
                    hound_data['why_next'],   # thesis as entry_conditions (was: action_suggestion)
                    notes_text,               # [ACTION] + signals summary
                    'pending'
                ))
                
                # 3. Mark as watched in hound table so it leaves the queue
                db.execute("UPDATE grok_hound_candidates SET status = 'watched' WHERE ticker = ?", (ticker,))
                db.commit()
                
            return {'ok': True, 'message': f'Ticker {ticker} sent to Acquisition Pipeline for final analysis'}
        except Exception as e:
            return {'ok': False, 'message': str(e)}, 500

    @app.route('/api/reject', methods=['POST'])
    def handle_reject():
        try:
            data = flask_request.get_json()
            ticker = data.get('ticker')
            if not ticker: return {'ok': False, 'message': 'Missing ticker'}, 400
            
            with _conn() as db:
                # Flag as ignored so Hound skips it in future
                db.execute("UPDATE grok_hound_candidates SET status = 'ignored' WHERE ticker = ?", (ticker,))
                db.commit()
            return {'ok': True, 'message': f'Ticker {ticker} moved to ignore list'}
        except Exception as e:
            return {'ok': False, 'message': str(e)}, 500

    @app.route('/api/prompt', methods=['POST'])
    def handle_prompt():
        try:
            data = flask_request.get_json()
            prompt = data.get('prompt')
            model_key = data.get('model', 'fast')
            if not prompt: return {'ok': False, 'message': 'Missing prompt'}, 400
            
            # Call AI
            if 'grok' in model_key.lower():
                import grok_client
                text, in_tok, out_tok = grok_client.call(prompt)
            else:
                import gemini_client
                text, in_tok, out_tok = gemini_client.call(prompt, model_key=model_key, caller='dashboard')
                
            return {
                'ok': True, 
                'response': text, 
                'stats': {'in': in_tok, 'out': out_tok}
            }
        except Exception as e:
            return {'ok': False, 'message': str(e)}, 500

    @app.route('/api/history/<ticker>')
    def handle_history(ticker):
        try:
            import yfinance as yf
            period = flask_request.args.get('period', '1mo') # 1d, 5d, 1mo, 1y
            interval = '15m' if period == '1d' else '1h' if period == '5d' else '1d'
            
            stock = yf.Ticker(ticker)
            hist = stock.history(period=period, interval=interval)
            
            if hist.empty:
                return {'ok': False, 'message': 'No data found'}, 404
                
            # Format for Chart.js
            labels = [ts.strftime('%Y-%m-%d %H:%M') for ts in hist.index]
            prices = [round(float(p), 2) for p in hist['Close']]
            
            return {
                'ok': True,
                'ticker': ticker,
                'labels': labels,
                'prices': prices,
                'current': prices[-1],
                'change_pct': round(((prices[-1] - prices[0]) / prices[0]) * 100, 2)
            }
        except Exception as e:
            return {'ok': False, 'message': str(e)}, 500

    @app.route('/health')
    def health():
        status = fetch_system_status()
        return {
            'status': 'ok',
            'defcon': status.get('defcon_level'),
            'signal_score': status.get('signal_score'),
            'last_cycle': status.get('created_at'),
        }

    @app.route('/api/logs')
    def get_logs():
        """Return last 35 cleaned lines from the orchestrator log for the rolling overlay."""
        import os, re as _re
        _base = Path(__file__).parent.resolve()
        # Prefer the unified logs/ path (written by launchd stdout); fall back to /tmp
        _candidates = [
            _base / 'logs' / 'orchestrator.log',
            Path('/tmp/orchestrator.log'),
        ]
        log_path = next((str(p) for p in _candidates if p.exists()), None)
        if log_path is None:
            return {'ok': False, 'lines': ['Orchestrator log not found — is it running?']}
        try:
            with open(log_path, 'r') as _f:
                raw = _f.readlines()
            cleaned = []
            for line in raw[-60:]:                           # read 60, trim to 35 after cleaning
                line = line.strip()
                if not line:
                    continue
                # Strip Python logging prefix: "INFO:module.submodule:  text" → "text"
                line = _re.sub(r'^(DEBUG|INFO|WARNING|ERROR|CRITICAL):[^:]*:\s*', '', line)
                # Collapse leading whitespace to single space
                line = line.strip()
                if line:
                    cleaned.append(line)
            return {'ok': True, 'lines': cleaned[-35:]}
        except FileNotFoundError:
            return {'ok': False, 'lines': ['Orchestrator log not found — is it running?']}
        except Exception as _e:
            return {'ok': False, 'lines': [f'Log read error: {_e}']}

    print()
    print("  ⚡ HighTrade Dashboard Server")
    print(f"   Local:   http://localhost:{port}")
    print(f"   Network: http://{local_ip}:{port}  ← share this with your team")
    print(f"   Health:  http://{local_ip}:{port}/health")
    print(f"   Auto-refresh: every 60s  (append ?refresh=30 to change, ?refresh=0 to disable)")
    print()
    print("   Press Ctrl+C to stop")
    print()
    app.run(host=host, port=port, debug=False, threaded=True)


# ─── CLI entry point ──────────────────────────────────────────────────────────

def main():
    if not DB_PATH.exists():
        print("ERROR: Database not found. Is hightrade_orchestrator.py running?")
        sys.exit(1)

    # Server mode
    if '--serve' in sys.argv or '-s' in sys.argv:
        port = 5055
        for arg in sys.argv:
            if arg.startswith('--port='):
                try:
                    port = int(arg.split('=')[1])
                except ValueError:
                    pass
        run_server(port=port)
        return

    # Static file generation mode
    print("  HighTrade Dashboard Generator")
    print(f"   DB:  {DB_PATH}")
    print(f"   Out: {OUT_PATH}")
    print("   Fetching data...", end='', flush=True)

    html = generate_dashboard_html()
    print(" done")

    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(html, encoding='utf-8')
    print(f"   Dashboard written -> {OUT_PATH}")

    if '--open' in sys.argv or '-o' in sys.argv:
        webbrowser.open(f'file://{OUT_PATH.resolve()}')
        print("   Opened in browser")


if __name__ == '__main__':
    main()
