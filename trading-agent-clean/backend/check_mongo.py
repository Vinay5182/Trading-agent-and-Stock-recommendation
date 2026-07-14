import asyncio
import json
from motor.motor_asyncio import AsyncIOMotorClient

async def main():
    client = AsyncIOMotorClient('mongodb://localhost:27017')
    db = client['trading_agent_clean']
    
    # Get MEDANTA
    row = await db['momentum_tv_confirmations'].find_one({"symbol": "MEDANTA", "tv_status": "MOMENTUM_CONFIRMED"})
    if not row:
        print("MEDANTA not found.")
        return
        
    print("--- Row ---")
    row['_id'] = str(row['_id'])
    print(json.dumps(row, indent=2, default=str))
    
    print("\n--- Logic trace ---")
    
    def _clean_grade(value: object) -> str:
        text = str(value or "").strip().upper().replace(" ", "_").replace("-", "_")
        return "A_PLUS" if text in {"A+", "A_PLUS"} else text

    def _risk_value(row: dict, field: str) -> str:
        risk_summary = row.get("risk_summary") if isinstance(row.get("risk_summary"), dict) else {}
        return str(row.get(field) if row.get(field) is not None else risk_summary.get(field) or "").upper()

    def _has_trade_ready_blocker(row: dict) -> bool:
        text = " ".join(
            str(row.get(field) or "").upper()
            for field in (
                "tv_status",
                "status",
                "final_status",
                "trade_quality_grade",
                "next_action",
                "next_action_for_paper_trade",
                "paper_plan_status",
                "plan_status",
            )
        )
        return any(blocker in text for blocker in ("WAIT_FOR_PULLBACK", "NO_TRADE", "REJECTED", "TECHNICAL_FAILED"))

    def _saved_plan_value(row: dict, field: str):
        aliases = {
            "entry_price": ("paper_entry_price", "entry_price", "entry"),
            "stop_loss": ("paper_stop_loss", "stop_loss", "sl"),
            "target_1": ("paper_target_1", "target_1", "t1"),
            "target_2": ("paper_target_2", "target_2", "t2"),
            "target_3": ("paper_target_3", "target_3", "t3"),
        }
        for key in aliases[field]:
            if row.get(key) not in (None, "", "-"):
                return row.get(key)
        return None

    def _number(value) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    def _has_plan_levels(row: dict) -> bool:
        values = [_number(_saved_plan_value(row, field)) for field in ("entry_price", "stop_loss", "target_1", "target_2", "target_3")]
        print(f"Plan levels: {values}")
        return all(value is not None for value in values) and values[0] > values[1]
        
    TRADE_READY_GRADES = {"A_PLUS", "A"}
    TRADE_READY_STATUSES = {"CONFIRMED_SIGNAL", "MOMENTUM_CONFIRMED"}
    HIGH_RISK_FIELDS = ("fake_breakout_risk", "retail_trap_risk", "overextended_risk")

    risk_summary = row.get("risk_summary") if isinstance(row.get("risk_summary"), dict) else {}
    trap_status = str(row.get("trap_status") or risk_summary.get("trap_status") or "").upper()
    print(f"trap_status: {trap_status}")
    
    grade = _clean_grade(row.get("trade_quality_grade"))
    print(f"trade_quality_grade: {grade} (original {row.get('trade_quality_grade')}) - in TRADE_READY_GRADES? {grade in TRADE_READY_GRADES}")
    
    status_str = str(row.get("tv_status") or row.get("status") or row.get("final_status") or "").upper()
    print(f"status: {status_str} - in TRADE_READY_STATUSES? {status_str in TRADE_READY_STATUSES}")
    
    print(f"paper_plan_valid: {row.get('paper_plan_valid')} - is True? {row.get('paper_plan_valid') is True}")
    
    print(f"has_trade_ready_blocker: {_has_trade_ready_blocker(row)} - not blocker? {not _has_trade_ready_blocker(row)}")
    
    print(f"'DANGER' not in trap_status: {'DANGER' not in trap_status}")
    
    print(f"HIGH risk fields:")
    for field in HIGH_RISK_FIELDS:
        val = _risk_value(row, field)
        print(f"  {field}: {val}")
    
    print(f"has_plan_levels: {_has_plan_levels(row)}")

asyncio.run(main())
