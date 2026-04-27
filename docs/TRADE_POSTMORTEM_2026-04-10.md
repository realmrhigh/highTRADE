# HighTrade Trade Postmortem

Date: 2026-04-10
Scope: Trades that materially changed balance and what the system missed

## Executive summary
- The account has a real edge in catalyst-driven, short-hold momentum trades when the setup is clean.
- The biggest drag was repeated persistence in weak ideas, especially DAL.
- The system is still under-instrumented for learning because key entry metadata is not being preserved well enough.

## What worked

### BRZE
- Best pocket in the sample.
- Multiple profitable trades, roughly +$51.22 total across 3 trades.
- Pattern: earnings/momentum plus follow-through.
- Lesson: clean catalyst + price reaction + short hold is a valid edge.

### TTD
- Also net positive, roughly +$24.40 across 8 trades.
- Pattern: public dispute / catalyst / recovery narrative with orderly exits.
- Lesson: the system can monetize a story when the tape confirms it.

## What failed

### DAL
- Worst trade family by far, roughly -$419.85 across 9 trades.
- Pattern: repeated re-entry, multiple stop-outs, one invalidation, one manual exit.
- Lesson: once the market rejects a thesis, the system must stop reloading it unless a genuinely new catalyst appears.

### Other losers
- SGML, GSAT, HUM, NCNO, APLS, BTC-USD all contributed smaller losses.
- Most losses were stop-loss exits, which means the problem is mostly bad entries rather than broken exits.

## Key failure modes

1. **Thesis persistence after invalidation**
   - DAL showed the system could keep trying the same dead idea too long.
   - The fix is stronger re-entry gating after stop-outs or invalidation.

2. **Weak preservation of entry reasoning**
   - Entry signal scores are not being retained cleanly in the records.
   - That makes postmortems less useful than they should be.

3. **Not enough portfolio telemetry**
   - The snapshot history is too sparse to reconstruct balance changes cleanly.
   - More frequent snapshots would make the performance story much clearer.

4. **Regime sensitivity still too soft**
   - A setup that works in one tape can fail hard in another.
   - We need stronger weighting for market regime, sector sympathy, and relative strength.

## What to weight more heavily next
- Catalyst specificity
- Multi-source confirmation
- Price action / gap / relative volume
- Sector sympathy and peer reaction
- Tradability and liquidity
- Market regime fit

## What to weight less heavily
- Generic market headlines
- Single-source noise
- Abstract sentiment without a ticker reaction
- “Interesting” stories that don’t have price confirmation

## Practical next steps
- Add a stronger anti-reentry rule after stop-loss / invalidation on the same thesis.
- Preserve entry signal score and reasoning in the trade record.
- Increase portfolio snapshot frequency.
- Build a compact edge stack:
  - discovery score
  - catalyst score
  - regime score
  - execution score
  - conviction threshold
- Use this postmortem as the baseline before changing weights.

## Bottom line
The system is capable of making money, but the current losses came from stubbornness and weak selection, not from a missing ability to find winners.

The edge exists.
The cleanup is about making it less forgiving of bad ideas.
