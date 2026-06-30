from fastapi import Header, Request
from fastapi.responses import JSONResponse
from services.error_contract import error_response


OPERATOR_INTENT_HEADER = "X-Trading-Agent-Intent"
OPERATOR_INTENT_VALUE = "operator-write-v1"
OPERATOR_INTENT_ERROR = {
    "code": "OPERATOR_INTENT_REQUIRED",
    "message": "Trusted operator intent is required for this operation.",
    "details": {},
}


class OperatorIntentRequired(Exception):
    pass


def require_operator_intent_value(value: str | None) -> None:
    if value != OPERATOR_INTENT_VALUE:
        raise OperatorIntentRequired()


async def require_operator_intent(
    x_trading_agent_intent: str | None = Header(default=None, alias=OPERATOR_INTENT_HEADER),
) -> None:
    require_operator_intent_value(x_trading_agent_intent)


async def operator_intent_exception_handler(_request: Request, _exc: OperatorIntentRequired) -> JSONResponse:
    return error_response(403, OPERATOR_INTENT_ERROR["code"], OPERATOR_INTENT_ERROR["message"], OPERATOR_INTENT_ERROR["details"])
