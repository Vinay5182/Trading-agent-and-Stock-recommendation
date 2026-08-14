import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from math import floor
from config import settings
from services.position_sizing import calculate_proposed_sizing

def test_case_1_portfolio_10_lakh_1_5pct_cap():
    # Portfolio ₹10,00,000 -> 1.5% = ₹15,000
    res = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=99.0, # SL dist = 1 -> risk qty = 3500 (grade A = 0.35% = 3500)
        grade="A",
        current_balance=1000000.0,
        available_margin=500000.0,
        open_margin=100000.0,
        combined_open_risk=1000.0,
        paper_mode=True,
    )
    assert res["ok"] is True
    # 1.5% cap = ₹15,000 margin -> gross exposure = ₹37,500 -> 375 shares
    assert res["final_quantity"] == 375
    assert res["required_margin"] == pytest.approx(15000.0)

def test_case_2_portfolio_20_lakh_1_5pct_cap():
    # Portfolio ₹20,00,000 -> 1.5% = ₹30,000
    res = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=99.0, # SL dist = 1 -> risk qty = 7000 (grade A = 0.35% = 7000)
        grade="A",
        current_balance=2000000.0,
        available_margin=500000.0,
        open_margin=100000.0,
        combined_open_risk=1000.0,
        paper_mode=True,
    )
    assert res["ok"] is True
    # 1.5% cap = ₹30,000 margin -> gross exposure = ₹75,000 -> 750 shares
    assert res["final_quantity"] == 750
    assert res["required_margin"] == pytest.approx(30000.0)

def test_case_3_portfolio_25_lakh_1_5pct_cap():
    # Portfolio ₹25,00,000 -> 1.5% = ₹37,500, BUT ₹30,000 hard cap applies!
    res = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=99.0, # SL dist = 1 -> risk qty = 8750
        grade="A",
        current_balance=2500000.0,
        available_margin=500000.0,
        open_margin=100000.0,
        combined_open_risk=1000.0,
        paper_mode=True,
    )
    assert res["ok"] is True
    # 1.5% = 37,500, capped at ₹30,000 margin -> gross exposure = ₹75,000 -> 750 shares
    assert res["final_quantity"] == 750
    assert res["required_margin"] == pytest.approx(30000.0)

def test_case_4_portfolio_30_lakh_absolute_30k_cap_wins():
    # Portfolio ₹30,00,000 -> 1.5% = ₹45,000, BUT ₹30,000 hard cap applies!
    res = calculate_proposed_sizing(
        entry_price=100.0,
        stop_loss=99.0, # SL dist = 1 -> risk qty = 10500
        grade="A",
        current_balance=3000000.0,
        available_margin=1000000.0,
        open_margin=100000.0,
        combined_open_risk=1000.0,
        paper_mode=True,
    )
    assert res["ok"] is True
    # Hard cap ₹30,000 margin -> gross exposure = ₹75,000 -> 750 shares
    assert res["final_quantity"] == 750
    assert res["required_margin"] == pytest.approx(30000.0)

def test_case_5_utilization_below_90_pct_allowed():
    # Portfolio ₹25,00,000, 90.0% cap = ₹22,50,000. Open margin = ₹10,00,000 (40.0%).
    res = calculate_proposed_sizing(
        entry_price=200.0,
        stop_loss=190.0,
        grade="A",
        current_balance=2500000.0,
        available_margin=1500000.0,
        open_margin=1000000.0,
        combined_open_risk=1000.0,
        paper_mode=True,
    )
    assert res["ok"] is True

def test_case_6_utilization_at_90_pct_capacity_exceeded():
    # Portfolio ₹25,00,000, 90.0% limit = ₹22,50,000. Open margin = ₹22,50,000.
    res = calculate_proposed_sizing(
        entry_price=200.0,
        stop_loss=190.0,
        grade="A",
        current_balance=2500000.0,
        available_margin=250000.0,
        open_margin=2250000.0, # Exactly 90.0%
        combined_open_risk=1000.0,
        paper_mode=True,
    )
    assert res["ok"] is False
    assert res["reason"] == "PORTFOLIO_MARGIN_LIMIT_EXCEEDED"

def test_config_settings_canonical_values():
    assert settings.STARTING_VIRTUAL_BALANCE == 2500000.0
    assert settings.PER_TRADE_CAPITAL_ALLOCATION_PERCENT == 1.5
    assert settings.MAX_PER_TRADE_CAPITAL == 30000.0
    assert settings.PORTFOLIO_MARGIN_LIMIT_PERCENT == 90.0
    assert settings.PORTFOLIO_MARGIN_UTILIZATION_CAP_PERCENT == 90.0
    assert settings.LEVERAGE == 2.5
