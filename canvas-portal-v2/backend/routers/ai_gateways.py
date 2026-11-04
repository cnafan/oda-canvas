import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from auth.middleware import get_current_user, require_admin
from auth.models import AuthUser
from k8s.resources.ai_gateways import (
    MAX_MCP_ARGUMENTS_BYTES,
    MAX_MCP_TEXT_LENGTH,
    GatewayRequestRefused,
    call_mcp_tool,
    ensure_model_is_active,
    get_ai_gateways_summary,
    read_mcp_resource,
    stream_model_test,
)

router = APIRouter(prefix="/api/ai-gateways", tags=["ai-gateways"])


class ModelTestRequest(BaseModel):
    model: str = Field(min_length=1, max_length=256)


class McpToolCallRequest(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_MCP_TEXT_LENGTH)
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("arguments")
    @classmethod
    def limit_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            size = len(json.dumps(value))
        except (RecursionError, TypeError, ValueError) as exc:
            raise ValueError("Arguments can't be sent") from exc
        if size > MAX_MCP_ARGUMENTS_BYTES:
            raise ValueError("Arguments are too large")
        return value


class McpResourceReadRequest(BaseModel):
    uri: str = Field(min_length=1, max_length=MAX_MCP_TEXT_LENGTH)


@router.get("")
def get_ai_gateways(_: AuthUser = Depends(get_current_user)):
    return get_ai_gateways_summary()


@router.post("/models/test")
async def test_model(body: ModelTestRequest, _: AuthUser = Depends(get_current_user)):
    try:
        await ensure_model_is_active(body.model)
    except GatewayRequestRefused as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return StreamingResponse(
        stream_model_test(body.model),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )


# JSONResponse skips FastAPI's recursive encoder, which deeply nested upstream data could overflow.
@router.post("/mcp/tools/call")
def call_tool(body: McpToolCallRequest, _: AuthUser = Depends(require_admin)):
    try:
        return JSONResponse(call_mcp_tool(body.name, body.arguments))
    except GatewayRequestRefused as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.post("/mcp/resources/read")
def read_resource(body: McpResourceReadRequest, _: AuthUser = Depends(get_current_user)):
    try:
        return JSONResponse(read_mcp_resource(body.uri))
    except GatewayRequestRefused as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
