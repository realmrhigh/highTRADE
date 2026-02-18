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
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DB_PATH    = SCRIPT_DIR / "trading_data" / "trading_history.db"
OUT_PATH   = SCRIPT_DIR / "trading_data" / "dashboard.html"


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
    with _conn() as db:
        rows = db.execute("""
            SELECT asset_symbol, shares, entry_price, current_price,
                   position_size_dollars, unrealized_pnl_dollars,
                   unrealized_pnl_percent, entry_date, defcon_at_entry
            FROM trade_records WHERE status='open'
            ORDER BY entry_date
        """).fetchall()
        return [dict(r) for r in rows]

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
    with _conn() as db:
        row = db.execute("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) as closed,
                SUM(CASE WHEN status='open'   THEN 1 ELSE 0 END) as open_count,
                SUM(CASE WHEN exit_reason='profit_target' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN exit_reason='stop_loss'     THEN 1 ELSE 0 END) as losses,
                ROUND(SUM(CASE WHEN status='closed' THEN profit_loss_dollars ELSE 0 END),2) as realized_pnl,
                ROUND(SUM(CASE WHEN status='open'   THEN unrealized_pnl_dollars ELSE 0 END),2) as unrealized_pnl,
                ROUND(SUM(CASE WHEN status='open'   THEN position_size_dollars ELSE 0 END),2) as deployed
            FROM trade_records
        """).fetchone()
        return dict(row) if row else {}

def fetch_daily_briefings():
    with _conn() as db:
        rows = db.execute("""
            SELECT date, model_key, market_regime, regime_confidence,
                   headline_summary, key_themes_json, biggest_risk,
                   biggest_opportunity, signal_quality, macro_alignment,
                   portfolio_assessment, watchlist_json, defcon_forecast,
                   model_confidence, created_at
            FROM daily_briefings
            ORDER BY created_at DESC LIMIT 6
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
                   entry_conditions, status, date_added, created_at
            FROM acquisition_watchlist
            ORDER BY created_at DESC LIMIT 20
        """).fetchall()
        return [dict(r) for r in rows]

def fetch_signal_history():
    """Last 48 monitoring cycles for sparkline charts"""
    with _conn() as db:
        rows = db.execute("""
            SELECT defcon_level, signal_score, vix_close, news_score, created_at
            FROM signal_monitoring ORDER BY created_at DESC LIMIT 48
        """).fetchall()
        return list(reversed([dict(r) for r in rows]))

def fetch_recent_news():
    with _conn() as db:
        rows = db.execute("""
            SELECT news_signal_id, news_score, dominant_crisis_type as crisis_type,
                   article_count, breaking_count, sentiment_summary as sentiment,
                   gemini_flash_json, created_at
            FROM news_signals ORDER BY created_at DESC LIMIT 6
        """).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            # Parse gemini_flash_json for action/confidence/reasoning
            try:
                gf = json.loads(d.get('gemini_flash_json') or '{}')
                d['gemini_pro_action']     = gf.get('recommended_action', gf.get('action', ''))
                d['gemini_pro_confidence'] = gf.get('confidence_in_signal', gf.get('confidence', 0))
                d['gemini_pro_reasoning']  = gf.get('reasoning', '')
            except Exception:
                d['gemini_pro_action']     = ''
                d['gemini_pro_confidence'] = 0
                d['gemini_pro_reasoning']  = ''
            results.append(d)
        return results

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
        return [dict(r) for r in clusters], [dict(r) for r in trades]


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
        return '<tr><td colspan="8" style="color:#555;text-align:center;padding:20px;">No open positions</td></tr>'
    rows = []
    for p in positions:
        ep    = float(p.get('entry_price') or 0)
        cp    = float(p.get('current_price') or ep)
        sh    = int(p.get('shares') or 0)
        pnl_d = float(p.get('unrealized_pnl_dollars') or 0)
        pnl_p = float(p.get('unrealized_pnl_percent') or 0)
        mv    = cp * sh
        dc    = p.get('defcon_at_entry', '?')
        color = pnl_color(pnl_d)
        bar_w = min(int(abs(pnl_p) * 3), 100)
        bar_c = '#00ff88' if pnl_d >= 0 else '#ff4444'
        rows.append(
            '<tr class="trow">'
            f'<td class="sym">{p.get("asset_symbol", "?")}</td>'
            f'<td>{sh:,}</td>'
            f'<td>${ep:,.2f}</td>'
            f'<td>${cp:,.2f}</td>'
            f'<td>${mv:,.2f}</td>'
            f'<td style="color:{color};">{fmt_dollar(pnl_d)}</td>'
            f'<td><div style="display:flex;align-items:center;gap:6px;">'
            f'<div style="width:60px;background:#111;border-radius:3px;height:4px;">'
            f'<div style="width:{bar_w}%;background:{bar_c};height:4px;border-radius:3px;"></div></div>'
            f'<span style="color:{color};">{fmt_pct(pnl_p)}</span></div></td>'
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
            f'<td class="sym">{t.get("asset_symbol", "?")}</td>'
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

def build_wl_rows(watchlist):
    if not watchlist:
        return '<tr><td colspan="6" style="color:#555;text-align:center;padding:20px;">Watchlist empty — waiting for next daily briefing</td></tr>'
    rows = []
    for w in watchlist:
        conf = float(w.get('model_confidence') or 0)
        conf_c = '#00ff88' if conf >= 0.7 else '#ffd700' if conf >= 0.5 else '#888'
        stat = w.get('status', 'pending')
        stat_c = {'pending': '#ffd700', 'active': '#00ff88', 'invalidated': '#ff4444'}.get(stat, '#888')
        cond = (w.get('entry_conditions') or '—')[:90]
        rows.append(
            '<tr class="trow">'
            f'<td class="sym">{w.get("ticker", "?")}</td>'
            f'<td><span style="color:{conf_c};font-weight:700;">{conf:.0%}</span></td>'
            f'<td>{regime_badge(w.get("market_regime"))}</td>'
            f'<td style="color:#aaa;font-size:11px;">{cond}{"…" if len(w.get("entry_conditions",""))>90 else ""}</td>'
            f'<td><span style="color:{stat_c};font-size:11px;text-transform:uppercase;">{stat}</span></td>'
            f'<td style="color:#666;font-size:11px;">{w.get("date_added", "—")}</td>'
            '</tr>'
        )
    return ''.join(rows)

def build_news_items(news):
    if not news:
        return '<div style="color:#555;text-align:center;padding:20px;">No news signals</div>'
    items = []
    for n in news:
        score = float(n.get('news_score') or 0)
        sc = '#ff4444' if score >= 70 else '#ffd700' if score >= 45 else '#888'
        ts = (n.get('created_at') or '—')[11:16]
        reasoning = (n.get('gemini_pro_reasoning') or '')[:130]
        items.append(
            '<div class="news-item">'
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">'
            f'<span style="color:#888;font-size:10px;">{ts}</span>'
            f'<span style="color:{sc};font-size:13px;font-weight:700;">{score:.1f}</span>'
            f'<span style="color:#aaa;font-size:11px;">{(n.get("crisis_type","")).replace("_"," ").upper()}</span>'
            f'{action_badge(n.get("gemini_pro_action"))}'
            '</div>'
            f'<div style="font-size:11px;color:#666;">{n.get("article_count",0)} articles &middot; {n.get("breaking_count",0)} breaking &middot; {(n.get("sentiment") or "")[:40]}</div>'
            f'<div style="font-size:11px;color:#888;margin-top:4px;font-style:italic;">{reasoning}{"…" if len(reasoning)>=130 else ""}</div>'
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
    ticker_tags = ''.join(f'<span class="ticker-tag">{t}</span>' for t in wl)
    return (
        '<div class="model-card">'
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">'
        f'<div class="card-label">{icon} {title}</div>'
        '<div style="display:flex;gap:8px;align-items:center;">'
        f'{regime_badge(b.get("market_regime"))}'
        f'<span style="color:{conf_c};font-weight:700;font-size:13px;">{conf:.0%}</span>'
        '</div></div>'
        f'<div style="color:#ddd;font-size:12px;line-height:1.6;margin-bottom:10px;">{b.get("headline_summary","—")}</div>'
        f'<div class="themes-row">{theme_pills}</div>'
        '<div class="two-col" style="margin-top:10px;">'
        '<div><div class="micro-label">BIGGEST RISK</div>'
        f'<div style="color:#ff8888;font-size:11px;line-height:1.5;word-wrap:break-word;overflow-wrap:break-word;">{(b.get("biggest_risk") or "—")[:180]}</div></div>'
        '<div><div class="micro-label">OPPORTUNITY</div>'
        f'<div style="color:#88ff88;font-size:11px;line-height:1.5;word-wrap:break-word;overflow-wrap:break-word;">{(b.get("biggest_opportunity") or "—")[:180]}</div></div>'
        '</div>'
        '<div style="margin-top:10px;"><div class="micro-label">WATCHLIST</div>'
        f'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:4px;">{ticker_tags}</div></div>'
        '<div style="margin-top:8px;"><div class="micro-label">DEFCON FORECAST</div>'
        f'<div style="color:#aaa;font-size:11px;line-height:1.5;word-wrap:break-word;overflow-wrap:break-word;">{(b.get("defcon_forecast") or "—")[:180]}</div></div>'
        '</div>'
    )


# ─── Main HTML Assembly ───────────────────────────────────────────────────────

def build_html(status, positions, closed, stats, briefings, macro, watchlist,
               sig_history, news, cong_clusters, cong_trades):

    now_str    = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    last_cycle = status.get('created_at', '—')

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

    latest_b = next((b for b in briefings if b.get('model_key') == 'reasoning'), briefings[0] if briefings else {})
    fast_b   = next((b for b in briefings if b.get('model_key') == 'fast'), {})
    bal_b    = next((b for b in briefings if b.get('model_key') == 'balanced'), {})

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
    wl_rows        = build_wl_rows(watchlist)
    news_items     = build_news_items(news)
    cong_cl_rows   = build_cong_cluster_rows(cong_clusters)
    cong_tr_rows   = build_cong_trade_rows(cong_trades)
    reasoning_card = build_model_card(latest_b, 'REASONING (Pro 3)', '&#129504;')
    fast_card      = build_model_card(fast_b,   'FAST (Flash)',       '&#9889;')
    balanced_card  = build_model_card(bal_b,    'BALANCED (Flash 8k)','&#9878;&#65039;')

    macro_sig_pills = ''.join(sig_pill(s) for s in macro_sigs)

    total_pnl_pct = total_pnl / total_capital * 100
    defcon_mod_val = float(macro.get('defcon_modifier') or 0)

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>HighTrade Dashboard</title>
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
.sym { font-weight:700; color:var(--accent); font-size:14px; }

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

/* Footer */
.footer { text-align:center; color:#2a2a40; font-size:10px; letter-spacing:2px; padding:24px 0; margin-top:10px; }
</style>
</head>
<body>
<div class="page">
""" + f"""
<!-- ═══ HEADER ═══ -->
<div class="header">
  <div>
    <div class="header-title">&#9889; HIGHTRADE</div>
    <div style="font-size:10px;color:#2a3a4a;letter-spacing:3px;margin-top:3px;">AUTONOMOUS TRADING INTELLIGENCE SYSTEM</div>
  </div>
  <div class="header-meta">
    <span class="live-dot"></span>LIVE<br/>
    Generated: {now_str}<br/>
    Last cycle: {last_cycle}
  </div>
</div>

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

<!-- ═══ DAILY INTELLIGENCE BRIEFING ═══ -->
<div class="section-head">&#129504; Daily Intelligence Briefing &mdash; {briefing_date}</div>

<div class="grid-full">
  <div class="panel" style="border-color:#7eb8f733;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:20px;">
      <div style="flex:1;">
        <div style="display:flex;gap:10px;align-items:center;margin-bottom:10px;">
          {regime_badge(latest_b.get('market_regime'))}
          <span style="color:#888;font-size:11px;">Confidence: <span style="color:{latest_conf_c};font-weight:700;">{latest_conf:.0%}</span></span>
          <span style="color:#888;font-size:11px;">&bull; Gemini Pro 3 Reasoning</span>
        </div>
        <div style="color:#ddd;font-size:13px;line-height:1.75;">{latest_b.get('headline_summary','No briefing available. Run /briefing to generate one.')}</div>
        <div class="themes-row" style="margin-top:10px;">{latest_theme_pills}</div>
      </div>
      <div style="min-width:240px;max-width:340px;flex-shrink:0;">
        <div style="margin-bottom:12px;">
          <div class="micro-label" style="margin-bottom:4px;">Signal Quality Assessment</div>
          <div style="color:#aaa;font-size:11px;line-height:1.6;word-wrap:break-word;overflow-wrap:break-word;white-space:normal;">{(latest_b.get('signal_quality') or '—')[:280]}</div>
        </div>
        <div>
          <div class="micro-label" style="margin-bottom:4px;">Portfolio Assessment</div>
          <div style="color:#aaa;font-size:11px;line-height:1.6;word-wrap:break-word;overflow-wrap:break-word;white-space:normal;">{(latest_b.get('portfolio_assessment') or '—')[:280]}</div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="grid-three">
  {reasoning_card}
  {fast_card}
  {balanced_card}
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
          <th>Mkt Value</th><th>Unrlzd P&amp;L</th><th>Return</th><th>DEFCON@Entry</th>
        </tr></thead>
        <tbody>{open_rows}</tbody>
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
          <th>Ticker</th><th>Confidence</th><th>Regime</th><th>Entry Conditions</th><th>Status</th><th>Added</th>
        </tr></thead>
        <tbody>{wl_rows}</tbody>
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

<!-- ═══ SYSTEM ARCHITECTURE ═══ -->
<div class="section-head">&#127959;&#65039; System Architecture</div>

<div class="grid-three">

  <div class="panel">
    <div class="panel-title">Research Team</div>
    <div style="display:flex;flex-direction:column;gap:8px;">
      <div class="stat">
        <div class="stat-label">News Engine</div>
        <div style="color:#00ff88;font-size:11px;">&#9679; ACTIVE &mdash; 15-min cycles</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">5-component scoring: sentiment &middot; concentration &middot; urgency &middot; confidence &middot; specificity</div>
      </div>
      <div class="stat">
        <div class="stat-label">Congressional Tracker</div>
        <div style="color:{'#ffd700' if not cong_clusters else '#00ff88'};font-size:11px;">{'&#9888;&#65039; S3 intermittent &mdash; Capitol Trades fallback' if not cong_clusters else '&#9679; ACTIVE'}</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">House + Senate disclosures &middot; cluster detection (3+ politicians in 30 days)</div>
      </div>
      <div class="stat">
        <div class="stat-label">FRED Macro</div>
        <div style="color:#00ff88;font-size:11px;">&#9679; ACTIVE &mdash; Score {macro_score_val:.0f}/100</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">9 indicators: yield curve &middot; fed funds &middot; unemployment &middot; M2 &middot; HY spreads &middot; sentiment</div>
      </div>
      <div class="stat">
        <div class="stat-label">Acquisition Researcher</div>
        <div style="color:#00ff88;font-size:11px;">&#9679; ACTIVE</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">yfinance &middot; SEC EDGAR &middot; analyst targets &middot; news + congressional integration</div>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title">Intelligence Team</div>
    <div style="display:flex;flex-direction:column;gap:8px;">
      <div class="stat">
        <div class="stat-label">Gemini Fast (Flash)</div>
        <div style="color:#00ff88;font-size:11px;">&#9679; thinkingBudget=0 &mdash; news triage</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">Real-time article analysis, action classification every cycle</div>
      </div>
      <div class="stat">
        <div class="stat-label">Gemini Balanced (Flash 8k)</div>
        <div style="color:#00ff88;font-size:11px;">&#9679; thinkingBudget=8000 &mdash; daily fast tier</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">Daily briefing balanced depth, broader reasoning</div>
      </div>
      <div class="stat">
        <div class="stat-label">Gemini Reasoning (Pro 3)</div>
        <div style="color:#00ff88;font-size:11px;">&#9679; thinkingBudget=-1 &mdash; deep analysis</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">Primary daily briefing &middot; acquisition analyst &middot; 16k+ output tokens</div>
      </div>
      <div class="stat">
        <div class="stat-label">Acquisition Verifier</div>
        <div style="color:#00ff88;font-size:11px;">&#9679; Flash daily reverification</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">Confirms / flags / invalidates conditional tracking entries</div>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title">Broker / Execution</div>
    <div style="display:flex;flex-direction:column;gap:8px;">
      <div class="stat">
        <div class="stat-label">DEFCON System</div>
        <div style="color:{dc_color};font-size:11px;">&#9679; D{defcon} &mdash; {dc_label}</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">Signal-driven 1&ndash;5 scale &middot; Macro modifier: {defcon_mod_val:+.1f}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Broker Agent</div>
        <div style="color:#00ff88;font-size:11px;">&#9679; AUTONOMOUS mode</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">Buys on DEFCON 1&ndash;2 &middot; profit target + stop loss exits &middot; acq. conditionals</div>
      </div>
      <div class="stat">
        <div class="stat-label">Position Sizing</div>
        <div style="color:#aaa;font-size:11px;">Base $10K &middot; Min $3K &middot; Max $20K</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">{len(positions)} open &middot; ${deployed:,.0f} deployed &middot; ${cash:,.0f} available</div>
      </div>
      <div class="stat">
        <div class="stat-label">Manual Commands</div>
        <div style="color:#7eb8f7;font-size:11px;">Slack: /buy &middot; /sell &middot; /hold &middot; /briefing</div>
        <div style="color:#666;font-size:10px;margin-top:3px;">IPC file bridge &middot; full override capability</div>
      </div>
    </div>
  </div>

</div>

<div class="footer">HIGHTRADE &middot; PAPER TRADING &middot; NOT FINANCIAL ADVICE &middot; {now_str}</div>
</div>
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
    cong_cl, cong_tr = fetch_congressional()
    return build_html(status, positions, closed, stats, briefings, macro,
                      watchlist, sig_hist, news, cong_cl, cong_tr)


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

    @app.route('/')
    def dashboard():
        html = generate_dashboard_html()
        # Inject auto-refresh meta tag into <head> (every 60s by default)
        refresh = flask_request.args.get('refresh', '60')
        try:
            int(refresh)
        except ValueError:
            refresh = '60'
        if refresh != '0':
            html = html.replace(
                '<meta name="viewport"',
                f'<meta http-equiv="refresh" content="{refresh}"/>\n<meta name="viewport"',
                1
            )
        return Response(html, mimetype='text/html')

    @app.route('/health')
    def health():
        status = fetch_system_status()
        return {
            'status': 'ok',
            'defcon': status.get('defcon_level'),
            'signal_score': status.get('signal_score'),
            'last_cycle': status.get('created_at'),
        }

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
