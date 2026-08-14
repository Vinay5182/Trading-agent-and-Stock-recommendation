"""
ai.trade_status
===============
Single authoritative source of truth for all trade terminal-state taxonomies.

Extracted and merged from:
  - backend/ai/features.py
  - backend/services/decision_outcome_dataset.py

Rules:
  - No Mongo reads or writes.
  - No ML training, model loading, or weight initialisation.
  - No TradingView / broker calls.
  - Pure constants; safe to import in any context.

Divergence resolution (AI-DATA-8):
  WIN_STATUSES  : union of both files (added TARGET_1_HIT from DOD).
  LOSS_STATUSES : union of both files (added STOP_LOSS_HIT from DOD;
                  features.py was the narrower subset).
  CLOSED_TRADE_STATUSES : carried from features.py (DOD had no equivalent);
                          verified to be a superset of WIN_STATUSES | LOSS_STATUSES.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Win outcomes
# ---------------------------------------------------------------------------

WIN_STATUSES: frozenset[str] = frozenset(
    {
        "TARGET_HIT",
        "TARGET_1_HIT_FINAL",
        "TARGET_2_HIT",
        "TARGET_3_HIT",
        "T1_HIT",
        "T2_HIT",
        "T3_HIT",
        "WON_T1",
        "WON_T2",
        "WON_T3",
    }
)

# ---------------------------------------------------------------------------
# Loss outcomes
# ---------------------------------------------------------------------------

LOSS_STATUSES: frozenset[str] = frozenset(
    {
        "STOP_HIT",
        "STOPPED",
        "STOPPED_AFTER_T1",
        "SL_HIT",
        "LOST_SL",
        "STOP_LOSS_HIT",        # present in DOD only; merged here
    }
)

# ---------------------------------------------------------------------------
# All terminal / closed states  (superset of WIN ∪ LOSS)
# ---------------------------------------------------------------------------

CLOSED_TRADE_STATUSES: frozenset[str] = frozenset(
    {
        # Win members
        "TARGET_HIT",
        "TARGET_1_HIT_FINAL",
        "TARGET_2_HIT",
        "TARGET_3_HIT",
        "T1_HIT",
        "T2_HIT",
        "T3_HIT",
        "WON_T1",
        "WON_T2",
        "WON_T3",
        # Loss members
        "STOP_HIT",
        "STOPPED",
        "STOPPED_AFTER_T1",
        "SL_HIT",
        "LOST_SL",
        "STOP_LOSS_HIT",
        # Other terminal states
        "CLOSED",
        "EXPIRED",
        "AMBIGUOUS",
        "ENTRY_MISSED_GAP_UP",
    }
)

# Sanity assertion (runs at import time; raises AssertionError on accidental edits)
assert WIN_STATUSES <= CLOSED_TRADE_STATUSES, "WIN_STATUSES must be a subset of CLOSED_TRADE_STATUSES"
assert LOSS_STATUSES <= CLOSED_TRADE_STATUSES, "LOSS_STATUSES must be a subset of CLOSED_TRADE_STATUSES"
assert WIN_STATUSES.isdisjoint(LOSS_STATUSES), "WIN_STATUSES and LOSS_STATUSES must be disjoint"
