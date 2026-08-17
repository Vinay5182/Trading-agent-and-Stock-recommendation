import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from database import get_database

logger = logging.getLogger("uvicorn.error")

class MLProgressRepository:
    """Repository for ml_candidate_daily_progress collection (Single-document per trade with daily_progress array)"""

    @staticmethod
    def get_collection():
        return get_database()["ml_candidate_daily_progress"]

    @classmethod
    async def create_indexes(cls):
        try:
            collection = cls.get_collection()
            await collection.create_index("paper_trade_id", unique=True)
            await collection.create_index("candidate_id")
            await collection.create_index("daily_progress.progress_date")
        except Exception as e:
            logger.error(f"Error creating indexes for MLProgressRepository: {e}")

    @classmethod
    async def upsert_daily_progress(cls, progress_data: Dict[str, Any]) -> bool:
        try:
            collection = cls.get_collection()
            paper_trade_id = str(progress_data.get("paper_trade_id") or progress_data.get("candidate_id") or "")
            if not paper_trade_id:
                logger.error("upsert_daily_progress called without paper_trade_id")
                return False

            progress_date = str(progress_data.get("progress_date") or progress_data.get("date") or "")[:10]

            daily_entry = {
                "progress_date": progress_date,
                "open": float(progress_data.get("open") or 0.0),
                "high": float(progress_data.get("high") or 0.0),
                "low": float(progress_data.get("low") or 0.0),
                "close": float(progress_data.get("close") or progress_data.get("current_price") or 0.0),
                "volume": float(progress_data.get("volume") or 0.0),
                "holding_days": int(progress_data.get("holding_days") or 0),
                "price_change_per_share": float(progress_data.get("price_change_per_share") or 0.0),
                "pnl_percent": float(progress_data.get("pnl_percent") or 0.0),
                "rr_progress": float(progress_data.get("rr_progress") or 0.0),
                "runup": float(progress_data.get("runup") or 0.0),
                "drawdown": float(progress_data.get("drawdown") or 0.0),
                "qty_remaining": float(progress_data.get("qty_remaining") or 0.0),
                "unrealized_position_pnl": float(progress_data.get("unrealized_position_pnl") or 0.0),
                "realized_position_pnl": float(progress_data.get("realized_position_pnl") or 0.0),
                "total_position_pnl": float(progress_data.get("total_position_pnl") or 0.0)
            }

            now = datetime.utcnow()
            top_set = {
                "status": progress_data.get("status", "ACTIVE"),
                "trade_status": progress_data.get("trade_status") or progress_data.get("status", "ACTIVE"),
                "current_price": float(progress_data.get("current_price") or progress_data.get("close") or 0.0),
                "current_rr": float(progress_data.get("rr_progress") or 0.0),
                "current_pnl": float(progress_data.get("total_position_pnl") or progress_data.get("unrealized_pnl") or 0.0),
                "holding_days": int(progress_data.get("holding_days") or 0),
                "updated_at": now
            }

            for id_field in ("symbol", "canonical_symbol", "tradingview_symbol", "candidate_id"):
                val = progress_data.get(id_field)
                if val is not None:
                    top_set[id_field] = val

            top_set_on_insert = {
                "paper_trade_id": paper_trade_id,
                "strategy_version": progress_data.get("strategy_version", "v1.0"),
                "analysis_version": progress_data.get("analysis_version", "v1.0"),
                "created_at": progress_data.get("created_at") or now,
                "paper_only": True,
                "daily_progress": []
            }

            # Remove fields from top_set_on_insert that are present in top_set to prevent MongoDB path conflict
            for k in list(top_set_on_insert.keys()):
                if k in top_set:
                    del top_set_on_insert[k]

            await collection.update_one(
                {"paper_trade_id": paper_trade_id},
                {"$set": top_set, "$setOnInsert": top_set_on_insert},
                upsert=True
            )

            res = await collection.update_one(
                {"paper_trade_id": paper_trade_id, "daily_progress.progress_date": progress_date},
                {"$set": {"daily_progress.$": daily_entry, "updated_at": now}}
            )

            if res.matched_count == 0:
                await collection.update_one(
                    {"paper_trade_id": paper_trade_id},
                    {"$push": {"daily_progress": daily_entry}, "$set": {"updated_at": now}}
                )

            return True
        except Exception as e:
            logger.error(f"Error upserting daily progress for paper_trade_id {progress_data.get('paper_trade_id')}: {e}")
            return False

    @classmethod
    async def get_progress_by_candidate(cls, candidate_id: str, limit: int = 365) -> List[Dict[str, Any]]:
        try:
            pipeline = [
                {"$match": {"candidate_id": candidate_id, "paper_only": True}},
                {"$unwind": "$daily_progress"},
                {
                    "$project": {
                        "_id": 0,
                        "paper_trade_id": "$paper_trade_id",
                        "candidate_id": "$candidate_id",
                        "symbol": "$symbol",
                        "canonical_symbol": "$canonical_symbol",
                        "tradingview_symbol": "$tradingview_symbol",
                        "status": "$status",
                        "trade_status": "$trade_status",
                        "progress_date": "$daily_progress.progress_date",
                        "open": "$daily_progress.open",
                        "high": "$daily_progress.high",
                        "low": "$daily_progress.low",
                        "close": "$daily_progress.close",
                        "volume": "$daily_progress.volume",
                        "holding_days": "$daily_progress.holding_days",
                        "price_change_per_share": "$daily_progress.price_change_per_share",
                        "pnl_percent": "$daily_progress.pnl_percent",
                        "rr_progress": "$daily_progress.rr_progress",
                        "runup": "$daily_progress.runup",
                        "drawdown": "$daily_progress.drawdown",
                        "qty_remaining": "$daily_progress.qty_remaining",
                        "unrealized_position_pnl": "$daily_progress.unrealized_position_pnl",
                        "realized_position_pnl": "$daily_progress.realized_position_pnl",
                        "total_position_pnl": "$daily_progress.total_position_pnl"
                    }
                },
                {"$sort": {"progress_date": 1}},
                {"$limit": limit}
            ]
            cursor = cls.get_collection().aggregate(pipeline)
            return [doc async for doc in cursor]
        except Exception as e:
            logger.error(f"Error finding progress for candidate {candidate_id}: {e}")
            return []

    @classmethod
    async def get_progress_for_date(cls, progress_date: str, limit: int = 1000) -> List[Dict[str, Any]]:
        try:
            pipeline = [
                {"$match": {"daily_progress.progress_date": progress_date, "paper_only": True}},
                {"$unwind": "$daily_progress"},
                {"$match": {"daily_progress.progress_date": progress_date}},
                {
                    "$project": {
                        "_id": 0,
                        "paper_trade_id": "$paper_trade_id",
                        "candidate_id": "$candidate_id",
                        "symbol": "$symbol",
                        "canonical_symbol": "$canonical_symbol",
                        "tradingview_symbol": "$tradingview_symbol",
                        "status": "$status",
                        "trade_status": "$trade_status",
                        "progress_date": "$daily_progress.progress_date",
                        "open": "$daily_progress.open",
                        "high": "$daily_progress.high",
                        "low": "$daily_progress.low",
                        "close": "$daily_progress.close",
                        "volume": "$daily_progress.volume",
                        "holding_days": "$daily_progress.holding_days",
                        "price_change_per_share": "$daily_progress.price_change_per_share",
                        "pnl_percent": "$daily_progress.pnl_percent",
                        "rr_progress": "$daily_progress.rr_progress",
                        "runup": "$daily_progress.runup",
                        "drawdown": "$daily_progress.drawdown",
                        "qty_remaining": "$daily_progress.qty_remaining",
                        "unrealized_position_pnl": "$daily_progress.unrealized_position_pnl",
                        "realized_position_pnl": "$daily_progress.realized_position_pnl",
                        "total_position_pnl": "$daily_progress.total_position_pnl"
                    }
                },
                {"$limit": limit}
            ]
            cursor = cls.get_collection().aggregate(pipeline)
            return [doc async for doc in cursor]
        except Exception as e:
            logger.error(f"Error finding progress for date {progress_date}: {e}")
            return []
