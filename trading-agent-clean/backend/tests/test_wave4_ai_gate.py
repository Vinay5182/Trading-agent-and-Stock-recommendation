import sys
import os
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
import pytest
from unittest.mock import patch, MagicMock

from routes.paper import apply_paper_plan_ai_gate_if_ready

@pytest.fixture
def mock_predict():
    with patch("ml.predict.predict_outcome") as mock:
        yield mock

@pytest.fixture
def mock_meta():
    with patch("ml.predict._loaded_meta", {"features": ["risk_score", "rule_score"]}) as mock:
        yield mock

def test_ai_gate_win_allows_trade(mock_predict, mock_meta):
    mock_settings = MagicMock()
    mock_settings.PAPER_AI_HARD_GATE_ENABLED = True
    with patch("routes.paper.settings", mock_settings):
        mock_predict.return_value = {
            "prediction": "WIN",
            "confidence": {"WIN": 0.8, "LOSS": 0.2},
            "model_used": "test_model"
        }
        plan = {"status": "WAITING"}
        signal = {"risk_score": 5, "rule_score": 45}
        apply_paper_plan_ai_gate_if_ready(plan, signal)
        assert plan["ai_gate_decision"] == "PASSED"
        assert plan["status"] == "WAITING"

def test_ai_gate_loss_blocks_trade(mock_predict, mock_meta):
    mock_settings = MagicMock()
    mock_settings.PAPER_AI_HARD_GATE_ENABLED = True
    with patch("routes.paper.settings", mock_settings):
        mock_predict.return_value = {
            "prediction": "LOSS",
            "confidence": {"WIN": 0.2, "LOSS": 0.8},
            "model_used": "test_model"
        }
        plan = {"status": "WAITING"}
        signal = {"risk_score": 5, "rule_score": 45}
        apply_paper_plan_ai_gate_if_ready(plan, signal)
        assert plan["ai_gate_decision"] == "REJECTED"
        assert plan["status"] == "AI_REJECTED"

def test_ai_gate_loss_advisory_only(mock_predict, mock_meta):
    mock_settings = MagicMock()
    mock_settings.PAPER_AI_HARD_GATE_ENABLED = False
    with patch("routes.paper.settings", mock_settings):
        mock_predict.return_value = {
            "prediction": "LOSS",
            "confidence": {"WIN": 0.2, "LOSS": 0.8},
            "model_used": "test_model"
        }
        plan = {"status": "WAITING"}
        signal = {"risk_score": 5, "rule_score": 45}
        apply_paper_plan_ai_gate_if_ready(plan, signal)
        assert plan["ai_gate_decision"] == "REJECTED"
        assert plan["status"] == "WAITING"
