from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import settings

import pytest
from services.position_sizing import calculate_proposed_sizing

def test_emmvee_like_case():
    # EMMVEE: entry 356.95, sl 330.25, Grade A (0.35% of 250,000 balance = 875 risk)
    # Stop distance = 26.7
    # Risk-allowed quantity = floor(875 / 26.7) = 32 shares
    # 1.5% margin cap = 3750 margin (9375 gross) / 356.95 = 26 shares. Capital cap wins!
    res = calculate_proposed_sizing(
        entry_price=356.95,
        stop_loss=330.25,
        grade="A",
        current_balance=250000.0,
        available_margin=250000.0,
        open_margin=0.0,
        combined_open_risk=0.0,
        paper_mode=True
    )
    assert res["ok"] is True
    assert res["final_quantity"] == 26

    # Test with 100,000 balance
    res_small = calculate_proposed_sizing(
        entry_price=356.95,
        stop_loss=330.25,
        grade="A",
        current_balance=100000.0,
        available_margin=100000.0,
        open_margin=0.0,
        combined_open_risk=0.0,
        paper_mode=True
    )
    assert res_small["ok"] is True
    assert res_small["final_quantity"] == 10

def test_texrail_like_case():
    # TEXRAIL: entry 115.65, sl 107.35, Grade A
    # Stop distance = 8.3. On 100,000 balance, risk budget = 350 -> risk qty = 42 shares.
    # 1.5% margin cap = 1500 margin (3750 gross) / 115.65 = 32 shares. Capital cap wins!
    res = calculate_proposed_sizing(
        entry_price=115.65,
        stop_loss=107.35,
        grade="A",
        current_balance=100000.0,
        available_margin=100000.0,
        open_margin=0.0,
        combined_open_risk=0.0,
        paper_mode=True
    )
    assert res["ok"] is True
    assert res["final_quantity"] == 32

def test_hext_like_case():
    # HEXT: entry 557.25, sl 511.35, Grade A
    # Stop distance = 45.9. On 100,000 balance, risk budget = 350. Qty by risk = floor(350 / 45.9) = 7 shares.
    # 1.5% margin cap = 1500 margin (3750 gross) / 557.25 = 6 shares. Capital cap wins!
    res = calculate_proposed_sizing(
        entry_price=557.25,
        stop_loss=511.35,
        grade="A",
        current_balance=100000.0,
        available_margin=100000.0,
        open_margin=0.0,
        combined_open_risk=0.0,
        paper_mode=True
    )
    assert res["ok"] is True
    assert res["final_quantity"] == 6

def test_rejections_and_invalid_cases():
    # quantity 0 -> rejected (risk allowed quantity < 1)
    # Stop distance very large, so risk budget permits 0 shares
    res_zero_qty = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=10.0,
        grade="A",
        current_balance=1000.0,  # risk budget = 3.5, stop distance = 90
        available_margin=1000.0,
        open_margin=0.0,
        combined_open_risk=0.0,
        paper_mode=True
    )
    assert res_zero_qty["ok"] is False
    assert res_zero_qty["reason"] == "RISK_QUANTITY_BELOW_MINIMUM"

    # invalid stop/entry
    res_invalid_sl = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=-5.0,
        grade="A",
        current_balance=100000.0,
        available_margin=100000.0,
        open_margin=0.0,
        combined_open_risk=0.0,
        paper_mode=True
    )
    assert res_invalid_sl["ok"] is False
    assert res_invalid_sl["reason"] == "INVALID_STOP_LOSS"

    res_invalid_dist = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=100.0,
        grade="A",
        current_balance=100000.0,
        available_margin=100000.0,
        open_margin=0.0,
        combined_open_risk=0.0,
        paper_mode=True
    )
    assert res_invalid_dist["ok"] is False
    assert res_invalid_dist["reason"] == "INVALID_STOP_DISTANCE"

def test_live_broker_path_unchanged():
    # Without paper_mode=True (defaulting to False), the ₹5,000 minimum margin floor must still block small positions.
    res_live = calculate_proposed_sizing(
        entry_price=356.95,
        stop_loss=330.25,
        grade="A",
        current_balance=100000.0,
        available_margin=100000.0,
        open_margin=0.0,
        combined_open_risk=0.0,
        paper_mode=False
    )
    assert res_live["ok"] is False
    assert res_live["reason"] == "RISK_QUANTITY_BELOW_MINIMUM"
