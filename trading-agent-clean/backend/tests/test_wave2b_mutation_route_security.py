import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from main import app
from security.operator_intent import OPERATOR_INTENT_HEADER, OPERATOR_INTENT_VALUE

OPERATOR_HEADERS = {OPERATOR_INTENT_HEADER: OPERATOR_INTENT_VALUE}

def get_client() -> TestClient:
    return TestClient(app)

def get_trusted_client() -> TestClient:
    client = TestClient(app)
    client.headers.update(OPERATOR_HEADERS)
    return client

class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.index = 0

    def sort(self, *args, **kwargs) -> 'FakeCursor':
        return self

    def limit(self, limit: int) -> 'FakeCursor':
        self.rows = self.rows[:limit]
        return self

    def __aiter__(self) -> 'FakeCursor':
        return self

    async def __anext__(self) -> dict:
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return row.copy()

class FakeCollection:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.find_calls = []
        self.find_one_calls = []
        self.update_calls = []
        self.insert_calls = []
        self.delete_calls = []

    def find(self, query: dict | None = None, *args, **kwargs) -> FakeCursor:
        self.find_calls.append(query)
        return FakeCursor([row.copy() for row in self.rows])

    async def find_one(self, query: dict, *args, **kwargs) -> dict | None:
        self.find_one_calls.append(query)
        for row in self.rows:
            if all(row.get(k) == v for k, v in query.items() if not k.startswith('$')):
                return row.copy()
        return None

    async def update_one(self, query: dict, update: dict, upsert: bool = False, **kwargs) -> SimpleNamespace:
        self.update_calls.append((query, update, upsert))
        return SimpleNamespace(modified_count=1, matched_count=1, upserted_id='fake_upsert_id')

    async def replace_one(self, query: dict, replacement: dict, upsert: bool = False, *args, **kwargs) -> SimpleNamespace:
        return SimpleNamespace(modified_count=1, matched_count=1, upserted_id='fake_upsert_id')

    async def delete_many(self, query: dict, *args, **kwargs) -> SimpleNamespace:
        self.delete_calls.append(query)
        return SimpleNamespace(deleted_count=len(self.rows))

    async def insert_one(self, document: dict, **kwargs) -> SimpleNamespace:
        self.insert_calls.append(document)
        return SimpleNamespace(inserted_id='fake_inserted_id')

    async def count_documents(self, query: dict = None, *args, **kwargs) -> int:
        return len(self.rows)

    def aggregate(self, pipeline: list[dict], *args, **kwargs) -> FakeCursor:
        return FakeCursor([])

class FakeDB:
    def __init__(self, signals: list[dict] = None, trades: list[dict] = None) -> None:
        self.paper_signals = FakeCollection(signals or [])
        self.paper_trades = FakeCollection(trades or [])
        self.paper_update_locks = FakeCollection([])
        self.momentum_tv_confirmations = FakeCollection([])
        self.swing_tv_confirmations = FakeCollection([])
        self.market_data = FakeCollection([])
        self.market_load_state = FakeCollection([])
        self.pipeline_run_locks = FakeCollection([])
        self.pipeline_run_status = FakeCollection([])
        self.scored_candidates = FakeCollection([])

    def __getitem__(self, name: str) -> FakeCollection:
        if name == 'paper_signals':
            return self.paper_signals
        if name == 'paper_trades':
            return self.paper_trades
        if name == 'paper_update_locks':
            return self.paper_update_locks
        if name == 'momentum_tv_confirmations':
            return self.momentum_tv_confirmations
        if name == 'swing_tv_confirmations':
            return self.swing_tv_confirmations
        if name == 'market_data':
            return self.market_data
        if name == 'market_load_state':
            return self.market_load_state
        if name == 'pipeline_run_locks':
            return self.pipeline_run_locks
        if name == 'pipeline_run_status':
            return self.pipeline_run_status
        if name == 'scored_candidates':
            return self.scored_candidates
        return FakeCollection([])

def patch_database(monkeypatch, signals: list[dict] = None, trades: list[dict] = None) -> FakeDB:
    db = FakeDB(signals, trades)
    monkeypatch.setattr('routes.paper.get_database', lambda: db)
    monkeypatch.setattr('routes.market.get_database', lambda: db)
    monkeypatch.setattr('routes.swing.get_database', lambda: db)
    monkeypatch.setattr('services.paper_sync.get_database', lambda: db)
    monkeypatch.setattr('database.get_database', lambda: db)
    return db

def test_build_plans_defaults_to_save_false(monkeypatch) -> None:
    db = patch_database(monkeypatch)
    client_trusted = get_trusted_client()
    response = client_trusted.post('/api/paper/build-plans')
    assert response.status_code == 200
    payload = response.json()
    assert payload['saved'] is False
    assert db.paper_trades.update_calls == []

def test_run_pipeline_defaults_to_dry_run_true(monkeypatch) -> None:
    db = patch_database(monkeypatch)
    client = get_client()
    response = client.post('/api/paper/run-pipeline')
    assert response.status_code == 200
    payload = response.json()
    assert payload['dry_run'] is True
    assert payload['mongo_writes_enabled'] is False

def test_run_pipeline_real_mode_requires_operator_intent(monkeypatch) -> None:
    db = patch_database(monkeypatch)
    client = get_client()
    response = client.post('/api/paper/run-pipeline?dry_run=false')
    assert response.status_code == 403

    trusted = get_trusted_client()
    response_trusted = trusted.post('/api/paper/run-pipeline?dry_run=false')
    assert response_trusted.status_code == 200
    assert response_trusted.json()['dry_run'] is False

def test_sync_trade_ready_defaults_to_dry_run_true(monkeypatch) -> None:
    db = patch_database(monkeypatch)
    client = get_client()
    response = client.post('/api/paper/sync-trade-ready')
    assert response.status_code == 200
    payload = response.json()
    assert payload['dry_run'] is True
    assert db.paper_signals.update_calls == []
    assert db.paper_trades.update_calls == []

def test_sync_trade_ready_real_mode_requires_operator_intent(monkeypatch) -> None:
    db = patch_database(monkeypatch)
    client = get_client()
    response = client.post('/api/paper/sync-trade-ready?dry_run=false')
    assert response.status_code == 403

    trusted = get_trusted_client()
    response_trusted = trusted.post('/api/paper/sync-trade-ready?dry_run=false')
    assert response_trusted.status_code == 200
    assert response_trusted.json()['dry_run'] is False

def test_auto_update_outcomes_defaults_to_dry_run_true(monkeypatch) -> None:
    db = patch_database(monkeypatch)
    client = get_client()
    response = client.post('/api/paper/auto-update-outcomes')
    assert response.status_code == 200
    payload = response.json()
    assert payload['dry_run'] is True
    assert payload['mongo_writes_enabled'] is False
    assert db.paper_trades.update_calls == []

def test_auto_update_outcomes_real_mode_requires_operator_intent(monkeypatch) -> None:
    db = patch_database(monkeypatch)
    client = get_client()
    response = client.post('/api/paper/auto-update-outcomes?dry_run=false')
    assert response.status_code == 403

    trusted = get_trusted_client()
    response_trusted = trusted.post('/api/paper/auto-update-outcomes?dry_run=false')
    assert response_trusted.status_code == 200
    assert response_trusted.json()['dry_run'] is False

def test_cleanup_invalid_symbols_requires_approval_hash(monkeypatch) -> None:
    db = patch_database(monkeypatch)
    # Seed an invalid document
    db.market_data.rows = [{"_id": "1", "symbol": "DUMMY_STK", "canonical_symbol": "DUMMY_STK"}]
    client = get_client()

    # 1. Verify dry_run preview returns a plan_hash
    response_dry = client.post('/api/market/cleanup-invalid-symbols')
    assert response_dry.status_code == 200
    payload_dry = response_dry.json()
    assert payload_dry['dry_run'] is True
    plan_hash = payload_dry['plan_hash']
    assert len(plan_hash) == 64

    # 2. Verify real run without operator intent fails with 403
    response_no_intent = client.post('/api/market/cleanup-invalid-symbols?dry_run=false')
    assert response_no_intent.status_code == 403

    # 3. Verify real run with operator intent but without approved_plan_hash fails with 400
    trusted = get_trusted_client()
    response_no_hash = trusted.post('/api/market/cleanup-invalid-symbols?dry_run=false')
    assert response_no_hash.status_code == 400

    # 4. Verify real run with operator intent and incorrect approved_plan_hash fails with 400
    response_bad_hash = trusted.post(f'/api/market/cleanup-invalid-symbols?dry_run=false&approved_plan_hash=wrong_hash')
    assert response_bad_hash.status_code == 400

    # 5. Verify real run with operator intent and correct approved_plan_hash succeeds (200)
    response_real = trusted.post(f'/api/market/cleanup-invalid-symbols?dry_run=false&approved_plan_hash={plan_hash}')
    assert response_real.status_code == 200
    payload_real = response_real.json()
    assert payload_real['dry_run'] is False
    assert payload_real['mongo_writes'] is True

def test_load_all_market_batches_requires_approval_hash(monkeypatch) -> None:
    db = patch_database(monkeypatch)
    client = get_client()

    # 1. Verify dry_run preview returns a plan_hash
    response_dry = client.post('/api/market/load-all-batches?max_batches=1')
    assert response_dry.status_code == 200
    payload_dry = response_dry.json()
    assert payload_dry['dry_run'] is True
    plan_hash = payload_dry['plan_hash']
    assert len(plan_hash) == 64

    # 2. Verify real run without operator intent fails with 403
    response_no_intent = client.post('/api/market/load-all-batches?dry_run=false&max_batches=1')
    assert response_no_intent.status_code == 403

    # 3. Verify real run with operator intent but without approved_plan_hash fails with 400
    trusted = get_trusted_client()
    response_no_hash = trusted.post('/api/market/load-all-batches?dry_run=false&max_batches=1')
    assert response_no_hash.status_code == 400

    # 4. Verify real run with operator intent and incorrect approved_plan_hash fails with 400
    response_bad_hash = trusted.post(f'/api/market/load-all-batches?dry_run=false&max_batches=1&approved_plan_hash=wrong_hash')
    assert response_bad_hash.status_code == 400

    # 5. Verify real run with operator intent and correct approved_plan_hash succeeds (200)
    response_real = trusted.post(f'/api/market/load-all-batches?dry_run=false&max_batches=1&approved_plan_hash={plan_hash}')
    assert response_real.status_code == 200
    payload_real = response_real.json()
    assert payload_real['dry_run'] is False

def test_swing_tv_confirmed_deduplication(monkeypatch) -> None:
    db = patch_database(monkeypatch)
    from datetime import datetime, timedelta
    now = datetime.utcnow()

    db.swing_tv_confirmations.rows = [
        {
            "symbol": "TCS",
            "tradingview_symbol": "NSE:TCS",
            "index_name": "BROAD_MARKET_750",
            "timeframes_hash": "hash1",
            "tv_status": "CONFIRMED_SIGNAL",
            "updated_at": now - timedelta(minutes=1),
        },
        {
            "symbol": "TCS",
            "tradingview_symbol": "NSE:TCS",
            "index_name": "BROAD_MARKET_750",
            "timeframes_hash": "hash1",
            "tv_status": "TECHNICAL_FAILED",
            "updated_at": now - timedelta(minutes=5),
        },
        {
            "symbol": "INFY",
            "tradingview_symbol": "NSE:INFY",
            "index_name": "BROAD_MARKET_750",
            "timeframes_hash": "hash2",
            "tv_status": "WAIT_FOR_RETEST",
            "updated_at": now - timedelta(minutes=2),
        }
    ]

    client = get_client()
    response = client.get('/api/swing/tv-confirmed')
    assert response.status_code == 200
    payload = response.json()
    assert payload['saved_rows_count'] == 2
    assert payload['count'] == 2

    rows = payload['rows']
    symbols = [r['symbol'] for r in rows]
    statuses = [r['tv_status'] for r in rows]
    assert "TCS" in symbols
    assert "INFY" in symbols
    assert "TECHNICAL_FAILED" not in statuses

def test_get_routes_do_not_write_or_trigger_actions(monkeypatch) -> None:
    db = patch_database(monkeypatch)
    client = get_client()

    response_health = client.get('/health')
    assert response_health.status_code == 200

    response_tv_status = client.get('/api/tv/runtime-status')
    assert response_tv_status.status_code == 200

    response_swing_tv = client.get('/api/swing/tv-confirmed')
    assert response_swing_tv.status_code == 200

    assert len(db.swing_tv_confirmations.update_calls) == 0
