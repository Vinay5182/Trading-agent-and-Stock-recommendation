from backend.routes.paper import realized_partial_pnl

def test_realized_partial_pnl_extracts_sub_documents_properly():
    trade = {
        "status": "T2_PARTIAL",
        "symbol": "LAURUSLABS",
        "partial_exit_1": {
            "exit_stage": "T1",
            "paper_pnl": 1660.50,
            "exited_at": "2026-07-27T09:11:46Z"
        },
        "partial_exit_2": {
            "exit_stage": "T2",
            "paper_pnl": 3321.00,
            "exited_at": "2026-07-27T10:07:07Z"
        },
        "stop_exit": None,
    }
    
    total_pnl = realized_partial_pnl(trade)
    assert total_pnl == 4981.50
