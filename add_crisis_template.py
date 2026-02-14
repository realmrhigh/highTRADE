#!/usr/bin/env python3
"""
Template for adding new crises to the database
Copy and modify this file to add your own crisis data
"""

from crisis_db_utils import CrisisDatabase

# Define your crisis data
new_crisis = {
    "date": "YYYY-MM-DD",  # Date of crisis onset
    "event_type": "TYPE",  # e.g., tariff_shock, geopolitical, rate_shock, etc.
    "trigger_description": "Description of what triggered the crisis",
    "drawdown_percent": 0.0,  # Peak-to-trough drawdown as percentage
    "recovery_days": 0,  # Days until recovery to previous highs
    "signals": {
        # Add signal indicators here as key-value pairs
        "signal_name_1": True,
        "signal_name_2": 50,  # Can be boolean, int, string, etc.
    },
    "resolution_catalyst": "What resolved or ended the crisis"
}

# Add to database
with CrisisDatabase() as db:
    crisis_id = db.add_crisis(new_crisis)
    print(f"✅ Added crisis ID: {crisis_id}")
    print(f"   Date: {new_crisis['date']}")
    print(f"   Type: {new_crisis['event_type']}")
    print(f"   Total crises in DB: {db.get_crisis_count()}")
