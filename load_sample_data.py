#!/usr/bin/env python3
"""
Sample data loader - Historical market crises
Populates the database with real historical events
Run after setup_database.py
"""

from crisis_db_utils import CrisisDatabase


def load_sample_crises():
    """Load historical crisis data into database"""

    historical_crises = [
        {
            "date": "2008-09-15",
            "event_type": "bubble_burst",
            "trigger_description": "Lehman Brothers bankruptcy triggers global financial crisis. Credit markets freeze, interbank lending collapses.",
            "drawdown_percent": 57.0,
            "recovery_days": 1251,
            "signals": {
                "credit_spreads_blown_out": True,
                "interbank_lending_frozen": True,
                "vix_above_80": True,
                "yield_curve_inverted": True,
                "bank_cdo_exposure": "massive"
            },
            "resolution_catalyst": "Federal Reserve TARP program, quantitative easing, emergency liquidity facilities"
        },
        {
            "date": "2020-03-16",
            "event_type": "pandemic",
            "trigger_description": "COVID-19 pandemic shock. Global lockdowns announced, economic shutdown fears trigger VIX spike to 82.7.",
            "drawdown_percent": 33.9,
            "recovery_days": 126,
            "signals": {
                "vix_above_50": True,
                "yield_curve_inverted": False,
                "credit_spreads_widened": True,
                "circuit_breakers_triggered": 4,
                "oil_negative_price": False
            },
            "resolution_catalyst": "Fed emergency QE, rate cuts to zero, corporate credit facilities, vaccine development hope"
        },
        {
            "date": "2018-12-24",
            "event_type": "rate_shock",
            "trigger_description": "Fed's continued rate hikes cause equity selloff. December marked worst month for S&P 500 since 1931.",
            "drawdown_percent": 19.8,
            "recovery_days": 65,
            "signals": {
                "fed_rate_hikes": 4,
                "tech_stock_decline": True,
                "high_yield_spreads_widened": True,
                "breadth_deterioration": True
            },
            "resolution_catalyst": "Fed signals pause in rate hike cycle, Powell's dovish pivot"
        },
        {
            "date": "2022-03-07",
            "event_type": "geopolitical",
            "trigger_description": "Russia invades Ukraine. Energy prices spike, geopolitical uncertainty, sanctions imposed.",
            "drawdown_percent": 12.4,
            "recovery_days": 87,
            "signals": {
                "oil_price_spike": 40,
                "vix_elevated": True,
                "equity_volatility_term_steep": True,
                "commodity_prices_rally": True,
                "safe_haven_bid": True
            },
            "resolution_catalyst": "Adaptation to energy disruption, commodity prices stabilize, equities find resilience"
        },
        {
            "date": "2015-08-24",
            "event_type": "liquidity_crisis",
            "trigger_description": "Flash crash triggered by mechanical selling. Trading halted multiple times. China devaluation fears.",
            "drawdown_percent": 8.5,
            "recovery_days": 30,
            "signals": {
                "circuit_breakers_triggered": 1,
                "vix_spike_intraday": 40.7,
                "algorithmic_selling": True,
                "illiquidity_in_etfs": True
            },
            "resolution_catalyst": "Trading halts calm the market, Fed reassurance, valuations attractive"
        },
        {
            "date": "2018-02-05",
            "event_type": "technical_break",
            "trigger_description": "Volatility explosion - 'Volmageddon'. VIX inverse products collapse. XIV liquidates.",
            "drawdown_percent": 11.3,
            "recovery_days": 45,
            "signals": {
                "vix_spike_intraday": 115.0,
                "vix_products_breakdown": True,
                "vol_term_structure_inversion": True,
                "leveraged_etf_decay": True
            },
            "resolution_catalyst": "VIX mean reversion, vol term structure normalizes"
        },
        {
            "date": "2011-08-05",
            "event_type": "rate_shock",
            "trigger_description": "US debt ceiling crisis, US downgrade threat. S&P downgrades US AAA rating.",
            "drawdown_percent": 19.4,
            "recovery_days": 238,
            "signals": {
                "us_credit_spread_widened": True,
                "yield_curve_flattened": True,
                "safe_haven_bid_treasuries": True,
                "equity_risk_premium_elevated": True
            },
            "resolution_catalyst": "Political resolution, Fed commits to low rates, European crisis diverts attention"
        },
        {
            "date": "1987-10-19",
            "event_type": "bubble_burst",
            "trigger_description": "Black Monday - largest single-day percentage decline. Program trading blamed. 22.6% drop in one day.",
            "drawdown_percent": 22.6,
            "recovery_days": 462,
            "signals": {
                "technical_break": True,
                "margin_liquidation_cascade": True,
                "options_volatility_explosion": True,
                "bid_ask_spread_massive": True
            },
            "resolution_catalyst": "Fed injected liquidity, trading halts introduced, circuit breakers installed"
        },
        {
            "date": "2000-03-10",
            "event_type": "bubble_burst",
            "trigger_description": "Dot-com bubble peaks. Tech valuations collapse over following months. NASDAQ down 78% from peak.",
            "drawdown_percent": 49.0,
            "recovery_days": 4897,
            "signals": {
                "tech_valuation_extreme": True,
                "ipo_mania_ending": True,
                "earnings_disappointments": True,
                "credit_conditions_tighten": True
            },
            "resolution_catalyst": "Time - recovery took 15+ years, structural economy shifts, 9/11 amplified decline"
        },
        {
            "date": "2023-03-10",
            "event_type": "liquidity_crisis",
            "trigger_description": "Silicon Valley Bank (SVB) collapse. Banking stress spreads. Depositor panic.",
            "drawdown_percent": 6.8,
            "recovery_days": 14,
            "signals": {
                "bank_duration_risk_realized": True,
                "deposit_flight_risk": True,
                "credit_spread_widened": True,
                "financial_stress_index_elevated": True
            },
            "resolution_catalyst": "Fed emergency funding, FDIC guarantee expanded, banking confidence restored"
        }
    ]

    print("\n📥 Loading Historical Crisis Data")
    print("="*60)

    with CrisisDatabase() as db:
        before_count = db.get_crisis_count()

        for i, crisis in enumerate(historical_crises, 1):
            crisis_id = db.add_crisis(crisis)
            print(f"{i:2d}. {crisis['date']} | {crisis['event_type']:20s} | "
                  f"Drawdown: {crisis['drawdown_percent']:5.1f}% | "
                  f"ID: {crisis_id}")

        after_count = db.get_crisis_count()

    print("\n" + "="*60)
    print(f"✅ Data Load Complete")
    print(f"   Before: {before_count} crises")
    print(f"   Added:  {len(historical_crises)} crises")
    print(f"   After:  {after_count} crises")
    print("="*60)

    # Display summary
    with CrisisDatabase() as db:
        crises_by_type = {}
        all_crises = db.get_all_crises()

        for crisis in all_crises:
            crisis_type = crisis['event_type']
            if crisis_type not in crises_by_type:
                crises_by_type[crisis_type] = []
            crises_by_type[crisis_type].append(crisis)

        print("\n📊 Crisis Distribution by Type:")
        print("-" * 60)
        for crisis_type, crises in sorted(crises_by_type.items()):
            avg_drawdown = sum(c['drawdown_percent'] for c in crises) / len(crises)
            print(f"  {crisis_type:20s}: {len(crises):2d} events | "
                  f"Avg drawdown: {avg_drawdown:5.1f}%")

        print("\n📈 Top 3 Worst Drawdowns:")
        print("-" * 60)
        sorted_by_drawdown = sorted(all_crises, key=lambda x: x['drawdown_percent'], reverse=True)[:3]
        for i, crisis in enumerate(sorted_by_drawdown, 1):
            print(f"  {i}. {crisis['date']} - {crisis['event_type']:20s} ({crisis['drawdown_percent']:5.1f}%)")


def load_sample_signals():
    """Load sample market signals"""

    sample_signals = [
        {
            "signal_type": "yield_inversion",
            "confidence": 0.92,
            "context": {"spread_bps": -15, "days_inverted": 447},
            "defcon_level": 3
        },
        {
            "signal_type": "credit_spread",
            "confidence": 0.87,
            "context": {"hy_oas": 425, "ig_oas": 95, "spread_widening": True},
            "defcon_level": 2
        },
        {
            "signal_type": "vix_spike",
            "confidence": 0.94,
            "context": {"vix_level": 28.5, "previous_close": 15.2, "20d_avg": 14.8},
            "defcon_level": 2
        }
    ]

    print("\n\n📥 Loading Sample Market Signals")
    print("="*60)

    with CrisisDatabase() as db:
        for i, signal in enumerate(sample_signals, 1):
            signal_id = db.add_signal(signal)
            print(f"{i}. {signal['signal_type']:20s} | "
                  f"Confidence: {signal['confidence']:.2f} | "
                  f"DEFCON {signal['defcon_level']} | "
                  f"ID: {signal_id}")

        recent = db.get_recent_signals(limit=3)

    print("\n" + "="*60)
    print(f"✅ Signals Loaded")
    print(f"   Total signals in database: {len(recent)}")
    print("="*60)


if __name__ == "__main__":
    print("\n🚀 TradingBot Sample Data Loader")

    try:
        load_sample_crises()
        load_sample_signals()

        print("\n\n💡 Next Steps:")
        print("   1. Query the database: python -c \"from crisis_db_utils import CrisisDatabase; "
              "db = CrisisDatabase(); print(f'Crises: {db.get_crisis_count()}')\"")
        print("   2. View README.md for API documentation")
        print("   3. Create analysis scripts for your trading strategy")
        print("\n" + "="*60 + "\n")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
