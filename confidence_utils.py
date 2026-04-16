#!/usr/bin/env python3
"""Confidence calibration helpers for highTRADE.

These helpers intentionally discount raw model confidence so downstream
trade decisions don't trust optimistic self-reporting too much.
"""

from __future__ import annotations

from typing import Optional


def calibrate_percent_confidence(
    raw_confidence: int | float,
    *,
    gap_pct: float = 0.0,
    relative_volume: float = 0.0,
    source_count: int = 0,
    summary_text: str = "",
) -> int:
    """Convert a 0-100 confidence into a more conservative execution score."""
    try:
        raw = int(round(float(raw_confidence)))
    except Exception:
        raw = 0
    raw = max(0, min(100, raw))

    penalty = 0

    # Base haircut for very high scores — models are usually too cute here.
    if raw >= 95:
        penalty += 10
    elif raw >= 90:
        penalty += 8
    elif raw >= 80:
        penalty += 5
    elif raw >= 70:
        penalty += 3

    # Thin evidence = less trustworthy.
    if source_count <= 1:
        penalty += 7
    elif source_count == 2:
        penalty += 4
    elif source_count == 3:
        penalty += 2

    # Weak participation = less conviction.
    try:
        rv = float(relative_volume or 0.0)
    except Exception:
        rv = 0.0
    if rv and rv < 1.5:
        penalty += 4
    elif rv and rv < 2.0:
        penalty += 2

    # Big gaps deserve skepticism.
    try:
        gp = float(gap_pct or 0.0)
    except Exception:
        gp = 0.0
    if gp >= 12:
        penalty += 5
    elif gp >= 8:
        penalty += 3
    elif gp >= 5:
        penalty += 1

    # Vagueness shouldn't score like certainty.
    if len((summary_text or "").strip()) < 40:
        penalty += 2

    return max(0, min(100, raw - penalty))


def calibrate_unit_confidence(
    raw_confidence: int | float,
    *,
    evidence_count: int = 0,
    gap_count: int = 0,
    support_strength: float = 0.0,
    summary_text: str = "",
) -> float:
    """Convert a 0.0-1.0 confidence into a more conservative score."""
    try:
        raw = float(raw_confidence)
    except Exception:
        raw = 0.0
    raw = max(0.0, min(1.0, raw))

    penalty = 0.0

    if raw >= 0.95:
        penalty += 0.08
    elif raw >= 0.90:
        penalty += 0.06
    elif raw >= 0.80:
        penalty += 0.04
    elif raw >= 0.70:
        penalty += 0.02

    if evidence_count <= 1:
        penalty += 0.07
    elif evidence_count == 2:
        penalty += 0.04
    elif evidence_count == 3:
        penalty += 0.02

    if support_strength and support_strength < 1.5:
        penalty += 0.04
    elif support_strength and support_strength < 2.0:
        penalty += 0.02

    if gap_count >= 3:
        penalty += 0.04
    elif gap_count == 2:
        penalty += 0.02

    if len((summary_text or "").strip()) < 40:
        penalty += 0.02

    return max(0.0, min(1.0, raw - penalty))
