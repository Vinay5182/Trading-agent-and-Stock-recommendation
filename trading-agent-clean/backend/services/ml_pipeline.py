import logging
import asyncio
from typing import Dict, Any, Optional

from services.ml_candidate_repository import MLCandidateRepository
from services.ml_market_repository import MLMarketRepository
from services.ml_event_repository import MLEventRepository
from services.ml_outcome_repository import MLOutcomeRepository
from services.ml_decision_repository import MLDecisionRepository
from services.ml_progress_repository import MLProgressRepository

logger = logging.getLogger("uvicorn.error")

class MLPipeline:
    """
    Central orchestration service for ML Data Acquisition.
    Responsible for ingesting events and persisting state into the ML schemas.
    """
    
    @classmethod
    async def initialize_indexes(cls):
        """Creates all required indexes for ML collections."""
        try:
            await asyncio.gather(
                MLCandidateRepository.create_indexes(),
                MLMarketRepository.create_indexes(),
                MLEventRepository.create_indexes(),
                MLOutcomeRepository.create_indexes(),
                MLDecisionRepository.create_indexes(),
                MLProgressRepository.create_indexes()
            )
            logger.info("Successfully initialized ML collection indexes.")
        except Exception as e:
            logger.error(f"Failed to initialize ML indexes: {e}")

    # Stub methods to be implemented in Phase 2.2
    
    @classmethod
    async def record_candidate_setup(cls, candidate_data: Dict[str, Any]):
        """Called when a candidate passes precheck."""
        try:
            from datetime import datetime
            import uuid
            from database import get_database

            symbol = candidate_data.get("canonical_symbol") or candidate_data.get("symbol")
            if not symbol:
                return
                
            db = get_database()
            
            # Fetch 90d history directly from MongoDB
            history = []
            try:
                cursor = db.historical_ohlcv.find({
                    "exchange": candidate_data.get("exchange") or "NSE",
                    "canonical_symbol": symbol,
                    "timeframe": "1d",
                    "is_closed": True
                }).sort("candle_open_at", -1).limit(90)
                
                history_docs = [doc async for doc in cursor]
                history_docs.sort(key=lambda x: x.get("candle_open_at", ""))
                
                for doc in history_docs:
                    doc.pop("_id", None)
                    history.append(doc)
            except Exception as e:
                logger.error(f"Failed to fetch warmup history for {symbol}: {e}")
                
            # Extract basic identifiers
            market_date = candidate_data.get("updated_at", datetime.utcnow().isoformat())[:10]
            
            from services.ml_models import MLMarketContext, MLSectorContext, MLCandidate
            
            # Create Sector Context
            sector = candidate_data.get("sector", "UNKNOWN")
            sector_context = MLSectorContext(
                sector=sector,
                market_date=market_date,
                created_at=datetime.utcnow()
            ).model_dump()
            await MLMarketRepository.insert_sector_context(sector_context)
            
            candidate_types = []
            if candidate_data.get("swing_candidate"):
                candidate_types.append("SWING")
            if candidate_data.get("momentum_candidate"):
                candidate_types.append("MOMENTUM")
                
            for c_type in candidate_types:
                import hashlib
                strategy_version = candidate_data.get("score_version", "v1.0")
                unique_str = f"{symbol}_{market_date}_{c_type}_{strategy_version}"
                candidate_id = f"cand_{hashlib.md5(unique_str.encode()).hexdigest()[:12]}"
                
                candidate = {
                    "candidate_id": candidate_id,
                    "paper_trade_id": None,
                    "candidate_type": c_type,
                    "strategy_version": strategy_version,
                    "analysis_version": "v1.0",
                    "setup_date": market_date,
                    "symbol": symbol,
                    "status": "PRECHECK_PASSED",
                    "pre_setup_ohlcv_90d": history,
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow()
                }
                
                for k, v in candidate_data.items():
                    if k not in candidate:
                        candidate[k] = v
                        
                # Sanitize nulls to pass strict Pydantic numeric validation
                for k in ["score", "momentum_score"]:
                    if candidate.get(k) is None:
                        candidate[k] = 0.0
                        
                candidate_model = MLCandidate(**candidate)
                candidate_dict = candidate_model.model_dump()
                
                # Preserve extra feature fields not defined in the schema
                for k, v in candidate.items():
                    if k not in candidate_dict:
                        candidate_dict[k] = v
                        
                existing = await MLCandidateRepository.find_candidate(candidate_id)
                if existing:
                    update_doc = {k: v for k, v in candidate_dict.items() if k not in ["candidate_id", "created_at", "paper_trade_id", "pre_setup_ohlcv_90d"]}
                    update_doc["updated_at"] = datetime.utcnow()
                    await MLCandidateRepository.update_candidate(candidate_id, update_doc)
                else:
                    await MLCandidateRepository.insert_candidate(candidate_dict)
                    
                    event_doc = {
                        "event_id": f"evt_{candidate_id}_CANDIDATE_CREATED",
                        "event_type": "CANDIDATE_CREATED",
                        "event_time": datetime.utcnow(),
                        "candidate_id": candidate_id,
                        "symbol": symbol,
                        "timestamp": datetime.utcnow(),
                        "created_at": datetime.utcnow(),
                        "transition": {
                            "from_state": "NONE",
                            "to_state": "PRECHECK_PASSED"
                        },
                        "metadata": {
                            "strategy_version": candidate_dict.get("score_version", "v1.0"),
                            "candidate_type": c_type,
                            "confirmation_status": "PRECHECK_PASSED",
                            "score": candidate_dict.get("score")
                        }
                    }
                    try:
                        await MLEventRepository.insert_event(event_doc)
                    except Exception as ev_err:
                        logger.error(f"Failed to insert ML event for new candidate {candidate_id}: {ev_err}")
        except Exception as e:
            logger.error(f"ML Pipeline record_candidate_setup failed: {e}")

    @classmethod
    async def record_tv_confirmation(cls, confirmation_row: Dict[str, Any], candidate_data: Dict[str, Any]):
        """Called when a TV confirmation completes."""
        try:
            from datetime import datetime
            import hashlib
            from database import get_database

            symbol = candidate_data.get("canonical_symbol") or candidate_data.get("symbol")
            if not symbol:
                return

            # Determine candidate types for this symbol (could be both)
            candidate_types = []
            if candidate_data.get("swing_candidate"):
                candidate_types.append("SWING")
            if candidate_data.get("momentum_candidate"):
                candidate_types.append("MOMENTUM")

            if not candidate_types:
                # Fallback if boolean flags are missing
                c_type = "MOMENTUM" if "momentum" in str(confirmation_row.get("strategy_type", "")).lower() else "SWING"
                candidate_types.append(c_type)

            market_date = candidate_data.get("setup_date") or candidate_data.get("updated_at", datetime.utcnow().isoformat())[:10]
            strategy_version = candidate_data.get("score_version", "v1.0")

            # Extract targets array from individual paper_target_1/2/3 keys if targets list is missing
            targets_list = confirmation_row.get("targets")
            if not targets_list:
                t_items = [confirmation_row.get("paper_target_1"), confirmation_row.get("paper_target_2"), confirmation_row.get("paper_target_3")]
                valid_t = [t for t in t_items if t is not None]
                if valid_t:
                    targets_list = valid_t

            stop_loss_val = confirmation_row.get("stop_loss") or confirmation_row.get("paper_stop_loss")
            entry_val = confirmation_row.get("entry_price") or confirmation_row.get("paper_entry_price") or candidate_data.get("entry")
            rr_val = confirmation_row.get("paper_rr_1") or confirmation_row.get("risk_reward")
            conf_val = confirmation_row.get("confidence_score") or confirmation_row.get("tv_confidence") or candidate_data.get("score")
            grade_val = confirmation_row.get("trade_quality_grade")
            if grade_val == "NO_TRADE" and confirmation_row.get("paper_plan_valid") is True:
                grade_val = "B"

            fields_to_update = {
                "tradingview_status": confirmation_row.get("tv_status"),
                "confidence": conf_val,
                "quality_grade": grade_val,
                "trap_status": confirmation_row.get("trap_status") or "CLEAN",
                "strategy_decision": confirmation_row.get("strategy_decision") or confirmation_row.get("tv_status"),
                "paper_trade_valid": confirmation_row.get("paper_plan_valid"),
                "entry": entry_val,
                "stop_loss": stop_loss_val,
                "targets": targets_list,
                "risk_reward": rr_val,
                "rejection_reason": confirmation_row.get("rejection_reason") or confirmation_row.get("reason"),
                "diagnostics": confirmation_row.get("diagnostics") or confirmation_row.get("risk_diagnostics"),
                "observer_version": confirmation_row.get("analysis_version", "v1.0"),
                "timestamp": datetime.utcnow()
            }
            # Clean out None values to avoid overwriting existing valid data with nulls accidentally
            update_doc = {k: v for k, v in fields_to_update.items() if v is not None}
            if not update_doc:
                return

            for c_type in candidate_types:
                unique_str = f"{symbol}_{market_date}_{c_type}_{strategy_version}"
                candidate_id = f"cand_{hashlib.md5(unique_str.encode()).hexdigest()[:12]}"

                existing = await MLCandidateRepository.find_candidate(candidate_id)
                if not existing:
                    logger.warning(f"TV Observer: No ML candidate found for {symbol} ({c_type}) id={candidate_id}, skipping update.")
                    continue

                previous_state = existing.get("status", "PRECHECK_PASSED")
                new_state = "TV_CONFIRMED" if "CONFIRMED" in str(update_doc.get("tradingview_status", "")).upper() else ("TV_REJECTED" if "REJECTED" in str(update_doc.get("tradingview_status", "")).upper() else previous_state)

                update_doc["status"] = new_state
                update_doc["confirmation_status"] = new_state
                await MLCandidateRepository.update_candidate(candidate_id, update_doc)

                # Record immutable audit event for candidate TV confirmation / rejection state transition
                if previous_state != new_state:
                    event_type = "TV_CONFIRMED" if new_state == "TV_CONFIRMED" else ("TV_REJECTED" if new_state == "TV_REJECTED" else "LIFECYCLE_TRANSITION")
                    event_doc = {
                        "event_id": f"evt_{candidate_id}_{new_state}",
                        "event_type": event_type,
                        "event_time": datetime.utcnow(),
                        "candidate_id": candidate_id,
                        "symbol": symbol,
                        "timestamp": datetime.utcnow(),
                        "created_at": datetime.utcnow(),
                        "transition": {
                            "from_state": previous_state,
                            "to_state": new_state
                        },
                        "metadata": {
                            "strategy_version": strategy_version,
                            "candidate_type": c_type,
                            "confirmation_status": new_state,
                            "rejection_reason": update_doc.get("rejection_reason"),
                            "tradingview_status": update_doc.get("tradingview_status")
                        }
                    }
                    try:
                        await MLEventRepository.insert_event(event_doc)
                    except Exception as ev_err:
                        logger.error(f"Failed to insert ML event for candidate {candidate_id}: {ev_err}")

        except Exception as e:
            logger.error(f"ML Pipeline record_tv_confirmation failed: {e}")
            
    @classmethod
    async def record_trade_creation(cls, trade_data: Dict[str, Any]):
        """Called when a paper trade is successfully created."""
        try:
            from datetime import datetime
            import uuid

            symbol = trade_data.get("symbol")
            if not symbol:
                return

            # Derive candidate_type
            source = str(trade_data.get("source", "")).lower()
            source_collection = str(trade_data.get("source_collection", "")).lower()
            source_signal_type = str(trade_data.get("source_signal_type", "")).lower()

            c_type = None
            if "momentum" in source or "momentum" in source_collection or "momentum" in source_signal_type:
                c_type = "MOMENTUM"
            elif "swing" in source or "swing" in source_collection or "swing" in source_signal_type:
                c_type = "SWING"
            if not c_type:
                c_type = "MOMENTUM" if "momentum" in str(trade_data.get("strategy_type", "")).lower() else "SWING"

            import hashlib
            setup_date = trade_data.get("setup_date") or trade_data.get("updated_at", datetime.utcnow().isoformat())[:10]
            strategy_version = trade_data.get("strategy_version") or trade_data.get("score_version", "v1.0")

            unique_str = f"{symbol}_{setup_date}_{c_type}_{strategy_version}"
            candidate_id = f"cand_{hashlib.md5(unique_str.encode()).hexdigest()[:12]}"

            # Find the existing ML candidate (we don't create it if missing)
            existing = await MLCandidateRepository.find_candidate(candidate_id)
            if not existing:
                logger.warning(f"Paper Observer: No ML candidate found for {symbol} ({c_type}) id={candidate_id}, skipping.")
                return

            previous_state = existing.get("status", "PRECHECK_PASSED")

            targets = [t for t in [trade_data.get("target_1"), trade_data.get("target_2"), trade_data.get("target_3")] if t is not None]

            fields_to_update = {
                "paper_trade_id": str(trade_data["_id"]) if trade_data.get("_id") is not None else str(trade_data.get("paper_trade_id") or ""),
                "paper_status": trade_data.get("status"),
                "paper_sync_time": datetime.utcnow(),
                "paper_sync_version": trade_data.get("state_version") or "v1.0",
                "entry": trade_data.get("entry_price") or trade_data.get("paper_entry_price"),
                "stop_loss": trade_data.get("stop_loss") or trade_data.get("paper_stop_loss"),
                "targets": targets if targets else None,
                "quantity": trade_data.get("quantity"),
                "position_size": trade_data.get("position_size"),
                "risk_amount": trade_data.get("risk_amount"),
                "expected_rr": trade_data.get("risk_reward") or trade_data.get("paper_rr_1"),
                "paper_trade_valid": trade_data.get("paper_plan_valid") or trade_data.get("projected_plan_valid"),
            }

            update_doc = {k: v for k, v in fields_to_update.items() if v is not None}
            if not update_doc:
                return

            # Determine lifecycle state (WAITING_FOR_ENTRY vs ACTIVE)
            paper_status = update_doc.get("paper_status", "")
            new_state = "ACTIVE" if paper_status == "ACTIVE" else ("WAITING_FOR_ENTRY" if paper_status in ("PENDING", "PLANNED", "WAITING") else "PAPER_READY")

            update_doc["status"] = new_state
            update_doc["updated_at"] = datetime.utcnow()

            await MLCandidateRepository.update_candidate(candidate_id, update_doc)

            # Link paper trade back to the candidate using the canonical identity mapping
            setup_id = trade_data.get("setup_id")
            trade_id = trade_data.get("_id")
            if setup_id or trade_id:
                try:
                    from database import get_database
                    db = get_database()
                    if setup_id:
                        await db.paper_trades.update_one(
                            {"setup_id": setup_id, "paper_only": True},
                            {"$set": {"candidate_id": candidate_id}}
                        )
                    elif trade_id:
                        await db.paper_trades.update_one(
                            {"_id": trade_id},
                            {"$set": {"candidate_id": candidate_id}}
                        )
                except Exception as e:
                    logger.error(f"Failed to update paper_trade with candidate_id {candidate_id}: {e}")

            # Record immutable event log for state transition
            if previous_state != new_state:
                event_doc = {
                    "event_id": f"evt_{uuid.uuid4().hex[:16]}",
                    "event_type": "LIFECYCLE_TRANSITION",
                    "event_time": datetime.utcnow(),
                    "candidate_id": candidate_id,
                    "symbol": symbol,
                    "timestamp": datetime.utcnow(),
                    "created_at": datetime.utcnow(),
                    "transition": {
                        "from_state": previous_state,
                        "to_state": new_state
                    },
                    "metadata": {
                        "paper_trade_id": update_doc.get("paper_trade_id"),
                        "paper_status": paper_status
                    }
                }
                try:
                    await MLEventRepository.insert_event(event_doc)
                except Exception as ev_err:
                    logger.error(f"Failed to insert ML event for {candidate_id}: {ev_err}")

        except Exception as e:
            logger.error(f"ML Pipeline record_trade_creation failed: {e}")
        
    @classmethod
    async def record_trade_transition(cls, trade_data: Dict[str, Any]):
        """Called when a paper trade outcome status changes or expires."""
        try:
            from datetime import datetime
            import uuid

            symbol = trade_data.get("symbol")
            if not symbol:
                return

            source = str(trade_data.get("source", "")).lower()
            source_collection = str(trade_data.get("source_collection", "")).lower()
            source_signal_type = str(trade_data.get("source_signal_type", "")).lower()

            c_type = None
            if "momentum" in source or "momentum" in source_collection or "momentum" in source_signal_type:
                c_type = "MOMENTUM"
            elif "swing" in source or "swing" in source_collection or "swing" in source_signal_type:
                c_type = "SWING"

            setup_date = trade_data.get("setup_date") or trade_data.get("updated_at", datetime.utcnow().isoformat())[:10]

            query = {"symbol": symbol, "setup_date": setup_date}
            if c_type:
                query["candidate_type"] = c_type

            collection = MLCandidateRepository.get_collection()
            existing = await collection.find_one(query)
            if not existing and trade_data.get("candidate_id"):
                existing = await collection.find_one({"candidate_id": trade_data.get("candidate_id")})
            if not existing and (trade_data.get("paper_trade_id") or trade_data.get("_id")):
                ptid = str(trade_data.get("paper_trade_id") or trade_data.get("_id"))
                existing = await collection.find_one({"paper_trade_id": ptid})
            if not existing:
                existing = await collection.find_one({"symbol": symbol}, sort=[("created_at", -1)])

            if not existing:
                logger.warning(f"Outcome Observer: No ML candidate found for {symbol} on {setup_date}, skipping.")
                return

            candidate_id = existing["candidate_id"]
            previous_state = existing.get("status", "PAPER_READY")

            fields_to_update = {
                "trade_status": trade_data.get("outcome_status") or trade_data.get("status"),
                "entry_triggered_at": trade_data.get("entry_triggered_at"),
                "entry_filled_at": trade_data.get("entry_triggered_at"), # Paper trades usually fill at trigger
                "exit_time": trade_data.get("exit_time") or trade_data.get("updated_at"),
                "exit_reason": trade_data.get("update_reason") or trade_data.get("paper_plan_reason"),
                "exit_price": trade_data.get("exit_price"),
                "holding_duration": trade_data.get("holding_duration"),
                "max_favorable_excursion": trade_data.get("max_favorable_move"),
                "max_adverse_excursion": trade_data.get("max_adverse_move"),
                "highest_price_seen": trade_data.get("highest_price_seen"),
                "lowest_price_seen": trade_data.get("lowest_price_seen"),
                "final_rr": trade_data.get("paper_rr_1"),
                "gross_pnl": trade_data.get("paper_pnl") or trade_data.get("gross_pnl"),
                "net_pnl": trade_data.get("paper_pnl") or trade_data.get("net_pnl"),
                "percent_return": trade_data.get("percent_return"),
                "trade_outcome": trade_data.get("outcome_status"),
            }

            update_doc = {k: v for k, v in fields_to_update.items() if v is not None}
            if not update_doc:
                return

            # We DO NOT update "status" because ML candidate status is now managed exclusively by the OHLC evaluator.
            # Only track the raw trade_status for observability if desired.
            
            update_doc["updated_at"] = datetime.utcnow()

            await MLCandidateRepository.update_candidate(candidate_id, update_doc)

            # --- AUTOMATIC TERMINAL OUTCOME & DECISION CREATION ---
            raw_status = str(trade_data.get("outcome_status") or trade_data.get("status") or "").upper()
            terminal_statuses = {"TARGET_1_HIT", "TARGET_2_HIT", "TARGET_3_HIT", "STOPLOSS_HIT", "MANUAL_EXIT", "EXPIRED", "WIN", "LOSS"}

            if any(term in raw_status for term in terminal_statuses):
                import hashlib
                result_label = "WIN" if "TARGET" in raw_status or raw_status == "WIN" else ("LOSS" if "STOPLOSS" in raw_status or raw_status == "LOSS" else raw_status)
                
                unique_str = f"{candidate_id}_{raw_status}_transition"
                outcome_id = f"out_{hashlib.md5(unique_str.encode('utf-8')).hexdigest()[:16]}"
                
                entry_p = float(trade_data.get("entry_price") or trade_data.get("entry") or 0.0)
                exit_p = float(trade_data.get("exit_price") or trade_data.get("current_price") or entry_p)
                stop_l = float(trade_data.get("stop_loss") or 0.0)
                
                risk = abs(entry_p - stop_l) if entry_p and stop_l else 1.0
                mfe = float(trade_data.get("max_favorable_move") or 0.0)
                mae = float(trade_data.get("max_adverse_move") or 0.0)
                max_rr = float(trade_data.get("paper_rr_1") or (mfe * entry_p / risk if risk > 0 else 0.0))
                
                sym = trade_data.get("symbol") or existing.get("symbol")
                csym = trade_data.get("canonical_symbol") or existing.get("canonical_symbol") or sym
                tvsym = trade_data.get("tradingview_symbol") or existing.get("tradingview_symbol") or (f"NSE:{sym}" if sym else None)

                outcome_doc = {
                    "outcome_id": outcome_id,
                    "candidate_id": candidate_id,
                    "paper_trade_id": str(trade_data.get("_id") or trade_data.get("paper_trade_id") or ""),
                    "symbol": sym,
                    "canonical_symbol": csym,
                    "tradingview_symbol": tvsym,
                    "entry_price": entry_p,
                    "exit_price": exit_p,
                    "strategy_version": trade_data.get("strategy_version", "v1.0"),
                    "analysis_version": trade_data.get("analysis_version", "v1.0"),
                    "max_favorable_excursion": mfe,
                    "max_adverse_excursion": mae,
                    "max_rr": max_rr,
                    "days_in_trade": int(trade_data.get("holding_days") or trade_data.get("holding_duration") or 0),
                    "planned_entry": entry_p,
                    "actual_entry": entry_p,
                    "slippage": 0.0,
                    "missed_entry_flag": False,
                    "status": raw_status,
                    "result": result_label,
                    "created_at": datetime.utcnow().isoformat()
                }
                await MLOutcomeRepository.insert_outcome(outcome_doc)
                
                decision_doc = {
                    "candidate_id": candidate_id,
                    "paper_trade_id": str(trade_data.get("_id") or trade_data.get("paper_trade_id") or ""),
                    "symbol": sym,
                    "canonical_symbol": csym,
                    "tradingview_symbol": tvsym,
                    "decision_type": "TRADE_CLOSURE",
                    "action": raw_status,
                    "result": result_label,
                    "entry_price": entry_p,
                    "exit_price": exit_p,
                    "exit_reason": trade_data.get("update_reason") or raw_status,
                    "created_at": datetime.utcnow().isoformat()
                }
                await MLDecisionRepository.insert_decision(decision_doc)

                event_doc = {
                    "event_id": f"evt_{uuid.uuid4().hex[:16]}",
                    "event_type": "TRADE_COMPLETED",
                    "event_time": datetime.utcnow(),
                    "candidate_id": candidate_id,
                    "paper_trade_id": str(trade_data.get("_id") or trade_data.get("paper_trade_id") or ""),
                    "symbol": sym,
                    "canonical_symbol": csym,
                    "tradingview_symbol": tvsym,
                    "timestamp": datetime.utcnow(),
                    "created_at": datetime.utcnow(),
                    "transition": {
                        "from_state": previous_state,
                        "to_state": raw_status
                    },
                    "metadata": {
                        "result": result_label,
                        "entry_price": entry_p,
                        "exit_price": exit_p,
                        "max_rr": max_rr
                    }
                }
                await MLEventRepository.insert_event(event_doc)

        except Exception as e:
            logger.error(f"ML Pipeline record_trade_transition failed: {e}")



    @classmethod
    async def record_daily_progress(cls, paper_trade: Optional[Dict[str, Any]] = None, market_row: Optional[Dict[str, Any]] = None, *, progress_data: Optional[Dict[str, Any]] = None):
        """Called daily to append progress for active candidates."""
        trade = paper_trade or progress_data
        if not trade or not isinstance(trade, dict):
            return

        try:
            from datetime import datetime
            import hashlib

            symbol = trade.get("symbol")
            if not symbol:
                return

            strategy_version = trade.get("strategy_version") or trade.get("score_version", "v1.0")
            candidate_id = trade.get("candidate_id")
            if not candidate_id:
                source = str(trade.get("source", "")).lower()
                source_collection = str(trade.get("source_collection", "")).lower()
                source_signal_type = str(trade.get("source_signal_type", "")).lower()

                c_type = None
                if "momentum" in source or "momentum" in source_collection or "momentum" in source_signal_type:
                    c_type = "MOMENTUM"
                elif "swing" in source or "swing" in source_collection or "swing" in source_signal_type:
                    c_type = "SWING"
                if not c_type:
                    c_type = "MOMENTUM" if "momentum" in str(trade.get("strategy_type", "")).lower() else "SWING"

                setup_date_str = trade.get("setup_date") or trade.get("updated_at", datetime.utcnow().isoformat())[:10]
                unique_str = f"{symbol}_{setup_date_str}_{c_type}_{strategy_version}"
                candidate_id = f"cand_{hashlib.md5(unique_str.encode()).hexdigest()[:12]}"

            paper_trade_id = str(trade["_id"]) if trade.get("_id") is not None else str(trade.get("paper_trade_id") or "")
            if not paper_trade_id:
                return

            setup_date = str(trade.get("setup_date") or trade.get("created_at") or datetime.utcnow().isoformat())[:10]

            if not market_row:
                try:
                    from database import get_database
                    from routes.paper import paper_market_latest_row
                    db = get_database()
                    latest_summary = await paper_market_latest_row(db, trade)
                    if latest_summary:
                        market_row = {
                            "open": latest_summary.get("open"),
                            "close": latest_summary.get("close"),
                            "high": latest_summary.get("high"),
                            "low": latest_summary.get("low"),
                            "volume": latest_summary.get("volume"),
                            "observed_at": latest_summary.get("time") or datetime.utcnow().isoformat()
                        }

                    if market_row and (market_row.get("volume") is None and market_row.get("traded_volume") is None):
                        try:
                            from routes.paper import find_market_data_for_trade
                            mdata = await find_market_data_for_trade(db, trade)
                            if mdata:
                                market_row["traded_volume"] = mdata.get("traded_volume") or mdata.get("volume")
                        except Exception as vol_err:
                            logger.debug(f"Could not load market_data for volume fallback: {vol_err}")
                except Exception as snap_err:
                    logger.debug(f"Could not load paper_market_latest_row: {snap_err}")

            progress_date = str((market_row.get("observed_at") if market_row and market_row.get("observed_at") else (trade.get("updated_at") or datetime.utcnow().isoformat())))[:10]

            progress_id = f"prog_{hashlib.md5(f'{candidate_id}_{progress_date}'.encode('utf-8')).hexdigest()[:16]}"

            current_price = 0.0
            if market_row:
                try:
                    current_price = float(market_row.get("close") or market_row.get("price") or market_row.get("current_price") or 0.0)
                except Exception:
                    current_price = 0.0
            if current_price <= 0:
                try:
                    current_price = float(trade.get("current_price") or trade.get("last_price") or trade.get("entry_price") or 0.0)
                except Exception:
                    current_price = 0.0

            entry_price = float(trade.get("entry_price") or trade.get("planned_entry_price") or current_price or 0.0)
            stop_loss = float(trade.get("stop_loss") or trade.get("planned_stop_loss") or 0.0)
            target1 = float(trade.get("target_1") or trade.get("target1") or 0.0)
            target2 = float(trade.get("target_2") or trade.get("target2") or 0.0)
            target3 = float(trade.get("target_3") or trade.get("target3") or 0.0)

            risk_per_share = (entry_price - stop_loss) if (entry_price > stop_loss > 0) else 0.0
            unrealized_pnl = (current_price - entry_price) if entry_price > 0 else 0.0
            pnl_percent = ((current_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0
            rr_progress = (unrealized_pnl / risk_per_share) if risk_per_share > 0 else 0.0

            runup = max(0.0, current_price - entry_price) if entry_price > 0 else 0.0
            drawdown = max(0.0, entry_price - current_price) if entry_price > 0 else 0.0

            price_change_per_share = (current_price - entry_price) if entry_price > 0 else 0.0
            qty_remaining = float(
                trade.get("quantity_remaining")
                if trade.get("quantity_remaining") is not None
                else (trade.get("quantity") or trade.get("final_quantity") or 0.0)
            )

            unrealized_position_pnl = price_change_per_share * qty_remaining
            realized_position_pnl = sum(
                float(trade.get(pe, {}).get("paper_pnl") or 0.0)
                for pe in ("partial_exit_1", "partial_exit_2", "partial_exit_3")
                if isinstance(trade.get(pe), dict)
            )
            total_position_pnl = realized_position_pnl + unrealized_position_pnl

            high_val = float(market_row.get("high") or current_price) if market_row else current_price
            low_val = float(market_row.get("low") or current_price) if market_row else current_price

            open_val = float(
                (market_row.get("open") if market_row and market_row.get("open") is not None else None)
                or trade.get("open_price")
                or trade.get("open")
                or current_price
                or 0.0
            )

            volume_val = float(
                (market_row.get("volume") if market_row and market_row.get("volume") is not None else None)
                or (market_row.get("traded_volume") if market_row and market_row.get("traded_volume") is not None else None)
                or trade.get("traded_volume")
                or trade.get("volume")
                or 0.0
            )

            entry_date_str = str(
                trade.get("entry_date")
                or trade.get("setup_date")
                or trade.get("source_trade_date")
                or trade.get("created_at")
                or ""
            )[:10]

            holding_days = 0
            if entry_date_str and len(entry_date_str) >= 10:
                try:
                    d_entry = datetime.strptime(entry_date_str[:10], "%Y-%m-%d").date()
                    d_prog = datetime.strptime(progress_date[:10], "%Y-%m-%d").date()
                    holding_days = max(0, (d_prog - d_entry).days)
                except Exception:
                    holding_days = 0

            progress_doc = {
                "progress_id": progress_id,
                "candidate_id": candidate_id,
                "paper_trade_id": paper_trade_id,
                "symbol": trade.get("symbol"),
                "canonical_symbol": trade.get("canonical_symbol"),
                "tradingview_symbol": trade.get("tradingview_symbol"),
                "progress_date": progress_date,
                "date": progress_date,
                "setup_date": setup_date,
                "strategy_version": strategy_version,
                "analysis_version": trade.get("analysis_version", "v1.0"),
                "current_price": current_price,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "target1": target1,
                "target2": target2,
                "target3": target3,
                "unrealized_pnl": unrealized_pnl,
                "pnl_percent": pnl_percent,
                "rr_progress": rr_progress,
                "runup": runup,
                "drawdown": drawdown,
                "price_change_per_share": price_change_per_share,
                "qty_remaining": qty_remaining,
                "unrealized_position_pnl": unrealized_position_pnl,
                "realized_position_pnl": realized_position_pnl,
                "total_position_pnl": total_position_pnl,
                "holding_days": holding_days,
                "status": trade.get("status", "ACTIVE"),
                "trade_status": trade.get("trade_status") or trade.get("status", "ACTIVE"),
                "paper_only": bool(trade.get("paper_only", True)),
                "open": open_val,
                "high": high_val,
                "low": low_val,
                "close": current_price,
                "volume": volume_val,
                "atr_14": float(market_row.get("atr_14", 0.0)) if market_row else 0.0,
                "ema_9": float(market_row.get("ema_9", 0.0)) if market_row else 0.0,
                "ema_21": float(market_row.get("ema_21", 0.0)) if market_row else 0.0,
                "ema_50": float(market_row.get("ema_50", 0.0)) if market_row else 0.0,
                "sma_200": float(market_row.get("sma_200", 0.0)) if market_row else 0.0,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            }

            await MLProgressRepository.upsert_daily_progress(progress_doc)

            event_doc = {
                "event_id": f"evt_{progress_id}",
                "event_type": "TRADE_DAILY_PROGRESS",
                "event_time": datetime.utcnow(),
                "candidate_id": candidate_id,
                "paper_trade_id": paper_trade_id,
                "symbol": trade.get("symbol"),
                "canonical_symbol": trade.get("canonical_symbol"),
                "tradingview_symbol": trade.get("tradingview_symbol"),
                "timestamp": datetime.utcnow(),
                "created_at": datetime.utcnow(),
                "metadata": {
                    "progress_date": progress_date,
                    "current_price": current_price,
                    "pnl_percent": pnl_percent,
                    "holding_days": holding_days
                }
            }
            await MLEventRepository.insert_event(event_doc)
        except Exception as e:
            logger.error(f"ML Pipeline record_daily_progress failed: {e}")

# Global instance for easy usage
ml_pipeline = MLPipeline()
