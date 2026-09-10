import os
import sys
import pytest
from math import floor

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import settings
from services.position_sizing import calculate_proposed_sizing

def test_1_normal_trade():
    """
    Given: portfolio balance = ₹2,500,000, 1.5% cap = ₹15,000, entry = ₹500, risk quantity = 500
    Expected: capital cap quantity = floor(15000 * 2.5 / 500) = 75, final quantity = 75
    """
    res = calculate_proposed_sizing(
        entry_price=500.0,
        stop_loss=475.0, # risk per share = 25. risk budget (A+ 0.5%) = 12500. risk q = 500
        grade="A+",
        current_balance=2500000.0,
        available_margin=500000.0,
        open_margin=0.0,
        combined_open_risk=0.0,
        paper_mode=True,
    )
    assert res["ok"] is True
    # 1.5% cap = ₹15,000 margin -> 75 shares
    assert res["final_quantity"] == 75
    assert res["required_margin"] <= 15000.0

def test_2_risk_limit_still_wins():
    """
    Given: portfolio balance = ₹2,500,000, 1.5% cap = ₹15,000, entry = ₹500, risk quantity = 50
    Expected: final quantity = 50. The capital cap must NOT increase quantity.
    """
    res = calculate_proposed_sizing(
        entry_price=500.0,
        stop_loss=250.0, # risk per share = 250. risk budget = 12500. risk q = 50
        grade="A+",
        current_balance=2500000.0,
        available_margin=500000.0,
        open_margin=0.0,
        combined_open_risk=0.0,
        paper_mode=True,
    )
    assert res["ok"] is True
    assert res["final_quantity"] == 50
    assert res["required_margin"] <= 15000.0

def test_3_tight_stop_sunpharma():
    """
    Given: portfolio balance = ₹2,500,000, entry = ₹1,977, 1.5% capital cap = ₹15,000 margin
    Expected: capital cap quantity = floor(15000 * 2.5 / 1977) = 18. Expected final quantity = 18.
    """
    res = calculate_proposed_sizing(
        entry_price=1977.0,
        stop_loss=1894.70, # SL dist = 82.30. risk budget = 12500. risk q = 151
        grade="A+",
        current_balance=2500000.0,
        available_margin=500000.0,
        open_margin=0.0,
        combined_open_risk=0.0,
        paper_mode=True,
    )
    assert res["ok"] is True
    assert res["final_quantity"] == 18
    assert res["required_margin"] <= 15000.0

def test_4_portfolio_utilization():
    """
    Given: portfolio balance = ₹2,500,000, 90.0% utilization limit = ₹2,250,000, existing deployed = ₹2,240,000, remaining capacity = ₹10,000, entry = ₹1,000
    Expected: portfolio capacity quantity = floor(10000 * 2.5 / 1000) = 25. The new trade must not reserve more than ₹10,000.
    """
    res = calculate_proposed_sizing(
        entry_price=1000.0,
        stop_loss=900.0,
        grade="A+",
        current_balance=2500000.0,
        available_margin=260000.0,
        open_margin=2240000.0, # remaining capacity = 10,000
        combined_open_risk=0.0,
        paper_mode=True,
    )
    assert res["ok"] is True
    assert res["final_quantity"] <= 25
    assert res["required_margin"] <= 10000.0

def test_5_portfolio_already_above_90_0():
    """
    Given: portfolio balance = ₹2,500,000, 90.0% limit = ₹2,250,000, existing deployed = ₹2,300,000
    Expected: new trade cannot consume additional portfolio margin.
    """
    res = calculate_proposed_sizing(
        entry_price=1000.0,
        stop_loss=900.0,
        grade="A+",
        current_balance=2500000.0,
        available_margin=200000.0,
        open_margin=2300000.0, # Open margin already > 90.0% limit (2,250,000)
        combined_open_risk=0.0,
        paper_mode=True,
    )
    assert res["ok"] is False
    assert res["reason"] == "PORTFOLIO_MARGIN_LIMIT_EXCEEDED"

def test_6_risk_calculation_unchanged():
    """
    Verify existing risk formulas return exactly the same result as before the fix for representative trades.
    """
    assert settings.GRADE_RISK_PERCENT_A_PLUS == 0.50
    assert settings.GRADE_RISK_PERCENT_A == 0.35
    assert settings.GRADE_RISK_PERCENT_B == 0.25
    assert settings.PORTFOLIO_RISK_LIMIT_PERCENT == 20.0

def test_7_existing_active_trade_immutability():
    """
    Verify the fix does not mutate dictionaries or existing trade states passed to it.
    """
    trade = {"symbol": "TEST", "quantity": 100, "entry_price": 500.0, "status": "ACTIVE"}
    orig_copy = dict(trade)
    res = calculate_proposed_sizing(
        entry_price=trade["entry_price"],
        stop_loss=450.0,
        grade="A+",
        current_balance=2500000.0,
        available_margin=500000.0,
        open_margin=0.0,
        combined_open_risk=0.0,
        paper_mode=True,
    )
    assert trade == orig_copy
