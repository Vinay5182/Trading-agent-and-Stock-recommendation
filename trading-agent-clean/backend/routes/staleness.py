from datetime import datetime, timezone

from nse_universe import get_universe_result


def parse_timestamp(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_score_stale(market_latest, scored_latest) -> bool:
    if market_latest and not scored_latest:
        return True
    market_time = parse_timestamp(market_latest)
    scored_time = parse_timestamp(scored_latest)
    if market_time and scored_time:
        return scored_time < market_time
    if market_latest and scored_latest:
        return str(scored_latest) < str(market_latest)
    return False


async def latest_market_data_updated_at(db, index_name: str):
    universe = get_universe_result(index_name)
    symbols = [row["canonical_symbol"] for row in universe["symbols"]]
    query = {"exchange": "NSE", "canonical_symbol": {"$in": symbols}}
    latest = await db.market_data.find_one(query, {"_id": 0, "updated_at": 1}, sort=[("updated_at", -1)])
    return latest.get("updated_at") if latest else None


async def score_staleness_for_query(db, index_name: str, scored_query: dict) -> dict:
    market_latest = await latest_market_data_updated_at(db, index_name)
    latest = await db.scored_candidates.find_one(
        scored_query,
        {"_id": 0, "updated_at": 1},
        sort=[("updated_at", -1)],
    )
    scored_latest = latest.get("updated_at") if latest else None
    return {
        "market_data_latest_updated_at": market_latest,
        "scored_candidates_latest_updated_at": scored_latest,
        "is_score_stale": is_score_stale(market_latest, scored_latest),
    }
