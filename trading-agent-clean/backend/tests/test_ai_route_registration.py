import importlib
import sys
import warnings
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _routes_for_path(app, path: str):
    return [route for route in app.routes if getattr(route, "path", None) == path]


def _prefixed_route_count(app, prefix: str) -> int:
    return sum(1 for route in app.routes if str(getattr(route, "path", "")).startswith(prefix))


def test_label_audit_route_registered_once_and_openapi_warning_removed() -> None:
    import main

    routes = _routes_for_path(main.app, "/api/ai/features/label-audit")

    assert len(routes) == 1
    assert "GET" in (getattr(routes[0], "methods", set()) or set())

    main.app.openapi_schema = None
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        schema = main.app.openapi()

    assert "/api/ai/features/label-audit" in schema["paths"]
    assert "get" in schema["paths"]["/api/ai/features/label-audit"]
    assert not any(
        "Duplicate Operation ID" in str(item.message)
        and "get_ai_features_label_audit_api_ai_features_label_audit_get" in str(item.message)
        for item in caught
    )


def test_historical_ohlcv_cli_and_ai_routes_still_import() -> None:
    historical_cli = importlib.import_module("cli.historical_ohlcv_orchestrate")
    ai_routes = importlib.import_module("routes.ai")

    assert hasattr(historical_cli, "parse_args")
    assert hasattr(historical_cli, "run")
    assert hasattr(ai_routes, "get_ai_features_label_audit")
    assert hasattr(ai_routes, "preview_historical_orchestration")
    assert hasattr(ai_routes, "verify_historical_orchestration")


def test_tradingview_and_paper_route_counts_unchanged() -> None:
    import main
    from routes import paper, tv

    assert len(tv.router.routes) == 5
    assert _prefixed_route_count(main.app, "/api/tv") == 5

    assert len(paper.router.routes) == 24
    assert _prefixed_route_count(main.app, "/api/paper") == 24
