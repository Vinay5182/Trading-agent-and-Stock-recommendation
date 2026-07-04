import sys
import copy
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from services.historical_ohlcv_store import (
    classify_persistence_action, 
    HISTORICAL_NOOP_IDENTICAL, 
    HISTORICAL_CONFLICT_CONTENT,
    canonical_content_fingerprint
)

@pytest.fixture
def base_candle():
    return {
        'schema_version': 'phase5a-v1', 
        'candle_id': '789aa98786fe756e7bd8cf679fa75b3f62472a8310e6403b0869736f32207959', 
        'adjusted_close': 2634.939453125, 
        'candle_close_at': '2026-05-25T18:30:00.000000Z', 
        'candle_open_at': '2026-05-24T18:30:00.000000Z', 
        'canonical_symbol': 'ASIANPAINT', 
        'close': 2657.800048828125, 
        'exchange': 'NSE', 
        'high': 2684.10009765625, 
        'is_closed': True, 
        'low': 2653.199951171875, 
        'open': 2672.0, 
        'provider': 'yfinance', 
        'provider_symbol': 'ASIANPAINT.NS', 
        'provenance': {
            'acquisition_method': 'yf.download(auto_adjust=False)', 
            'acquisition_version': 'phase5a-v1', 
            'adjusted_prices': False, 
            'fetched_at': '2026-07-04T14:15:25.242237Z', 
            'normalization_version': 'phase5a-v1', 
            'provider': 'yfinance', 
            'provider_interval': '1d', 
            'provider_row_fingerprint': '7d249a3eafdb1216385503673ce97901194eaf8b3202d7b2baf3a228d567d62d', 
            'provider_symbol': 'ASIANPAINT.NS', 
            'requested_end': '2026-07-02T00:00:00.000000Z', 
            'requested_start': '2026-05-25T00:00:00.000000Z', 
            'source_timezone': 'Asia/Kolkata', 
            'timestamp_semantic': 'open', 
            'validation_reason_codes': [], 
            'validation_status': 'VALID',
            'acquisition_version': 'phase5a-v1'
        }, 
        'quality': {
            'is_conflicting_duplicate': False, 
            'is_duplicate': False, 
            'reason_codes': [], 
            'status': 'VALID'
        }, 
        'timeframe': '1d', 
        'volume': 815621
    }

@pytest.fixture
def base_existing_document(base_candle):
    doc = copy.deepcopy(base_candle)
    doc["canonical_content_fingerprint"] = canonical_content_fingerprint(base_candle)
    return doc

def test_identical_no_drift(base_candle, base_existing_document):
    res = classify_persistence_action(base_candle, existing_document=base_existing_document)
    assert res["action"] == "noop"
    assert res["code"] == HISTORICAL_NOOP_IDENTICAL
    assert "ADJUSTED_CLOSE_DRIFT_TOLERATED" not in res.get("reason_codes", [])

def test_adjusted_close_tiny_drift(base_candle, base_existing_document):
    # Drift the candidate adjusted_close slightly within tolerance
    base_candle["adjusted_close"] = 2634.945  # diff = 0.0055 <= 0.01

    res = classify_persistence_action(base_candle, existing_document=base_existing_document)
    assert res["action"] == "noop"
    assert res["code"] == HISTORICAL_NOOP_IDENTICAL
    assert "ADJUSTED_CLOSE_DRIFT_TOLERATED" in res["reason_codes"]
    assert res["candle_id"] == base_candle["candle_id"]  # stable candle_id

def test_adjusted_close_large_drift(base_candle, base_existing_document):
    # Drift the candidate adjusted_close beyond tolerance
    base_candle["adjusted_close"] = 2634.96  # diff = 0.02 > 0.01

    res = classify_persistence_action(base_candle, existing_document=base_existing_document)
    assert res["action"] == "conflict"
    assert res["code"] == HISTORICAL_CONFLICT_CONTENT

def test_ohlcv_difference_triggers_conflict(base_candle, base_existing_document):
    # Change close price slightly (and adjusted close has tiny drift too)
    base_candle["close"] = 2658.0
    base_candle["adjusted_close"] = 2634.945

    res = classify_persistence_action(base_candle, existing_document=base_existing_document)
    assert res["action"] == "conflict"
    assert res["code"] == HISTORICAL_CONFLICT_CONTENT

def test_volume_difference_triggers_conflict(base_candle, base_existing_document):
    # Change volume (and adjusted close has tiny drift too)
    base_candle["volume"] = 815622
    base_candle["adjusted_close"] = 2634.945

    res = classify_persistence_action(base_candle, existing_document=base_existing_document)
    assert res["action"] == "conflict"
    assert res["code"] == HISTORICAL_CONFLICT_CONTENT

def test_candle_id_stable(base_candle, base_existing_document):
    res = classify_persistence_action(base_candle, existing_document=base_existing_document)
    assert res["candle_id"] == base_candle["candle_id"]
