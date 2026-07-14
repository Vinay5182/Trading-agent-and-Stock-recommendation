import asyncio
import json
from motor.motor_asyncio import AsyncIOMotorClient
from collections import defaultdict

async def main():
    client = AsyncIOMotorClient('mongodb://localhost:27017')
    db = client['trading_agent_clean']
    
    async def get_stats(collection_name):
        trap_status_counts = defaultdict(int)
        quality_counts = defaultdict(int)
        tv_status_counts = defaultdict(int)
        crosstab = defaultdict(lambda: defaultdict(int))
        no_trade_reasons = defaultdict(int)
        
        cursor = db[collection_name].find({})
        
        async for doc in cursor:
            trap = doc.get("trap_status")
            if not trap: trap = "Missing / NULL"
            trap_status_counts[trap] += 1
            
            grade = doc.get("trade_quality_grade")
            if not grade: grade = "Missing / NULL"
            quality_counts[grade] += 1
            
            tv_status = doc.get("tv_status")
            if not tv_status: tv_status = "Missing / NULL"
            tv_status_counts[tv_status] += 1
            
            crosstab[trap][grade] += 1
            
            if tv_status in ("MOMENTUM_CONFIRMED", "CONFIRMED_SIGNAL") and grade == "NO_TRADE":
                rr = doc.get("paper_rr_1")
                try:
                    rr = float(rr) if rr is not None else None
                except:
                    rr = None
                    
                fake = str(doc.get("fake_breakout_risk") or "").upper()
                over = str(doc.get("overextended_risk") or "").upper()
                trap_stat = str(doc.get("trap_status") or "").upper()
                trap_reason = doc.get("trap_reason") or ""
                
                # Check based on user prompt examples:
                if trap_stat == "DANGER":
                    if "low_volume_breakout" in trap_reason:
                        no_trade_reasons["Low volume breakout"] += 1
                    else:
                        no_trade_reasons["trap_status = DANGER (Other)"] += 1
                elif rr is not None and rr < 2:
                    no_trade_reasons["RR < 2"] += 1
                elif fake == "HIGH":
                    no_trade_reasons["Fake breakout"] += 1
                elif over == "HIGH":
                    no_trade_reasons["Overextended"] += 1
                else:
                    no_trade_reasons["Any other blocker"] += 1
                    
        return {
            "trap_status_counts": dict(trap_status_counts),
            "quality_counts": dict(quality_counts),
            "tv_status_counts": dict(tv_status_counts),
            "crosstab": {k: dict(v) for k, v in crosstab.items()},
            "no_trade_reasons": dict(no_trade_reasons)
        }

    momentum_stats = await get_stats('momentum_tv_confirmations')
    swing_stats = await get_stats('swing_tv_confirmations')
    
    print("=== MOMENTUM ===")
    print(json.dumps(momentum_stats, indent=2))
    print("\n=== SWING ===")
    print(json.dumps(swing_stats, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
