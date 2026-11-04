import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from auth.middleware import get_current_user, is_admin, require_admin
from auth.models import AuthUser
from k8s.resources.ai_gateway_mcp import list_mcp_targets, remove_mcp_target, save_mcp_target
from k8s.resources.ai_gateway_models import list_model_configs, remove_model, save_model
from k8s.resources.ai_gateways import (
    MAX_MCP_ARGUMENTS_BYTES,
    MAX_MCP_TEXT_LENGTH,
    GatewayRequestRefused,
    call_mcp_tool,
    ensure_model_is_active,
    get_ai_gateways_summary,
    read_mcp_resource,
    stream_model_test,
    without_schema_values,
)

logger = logging.getLogger(__name__)
MAX_OPENAPI_SCHEMA_LENGTH = 256 * 1024

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


class ModelParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, max_length=256)
    baseUrl: str | None = Field(default=None, max_length=MAX_MCP_TEXT_LENGTH)
    awsRegion: str | None = Field(default=None, max_length=64)
    bedrockEndpointPreference: Literal["runtimePreferred", "mantlePreferred", "mantleOnly", "runtimeOnly"] | None = None
    vertexRegion: str | None = Field(default=None, max_length=64)
    vertexProject: str | None = Field(default=None, max_length=256)
    azureResourceName: str | None = Field(default=None, max_length=256)
    azureResourceType: Literal["openAI", "foundry", "aiServices"] | None = None
    azureApiVersion: str | None = Field(default=None, max_length=64)
    azureProjectName: str | None = Field(default=None, max_length=256)
    tokenize: bool | None = None


def _limit_json(value: Any) -> None:
    try:
        size = len(json.dumps(value, allow_nan=False))
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError("This value can't be stored") from exc
    if size > MAX_MCP_ARGUMENTS_BYTES:
        raise ValueError("This value is too large")


def _clean_name(value: str) -> str:
    if value != value.strip() or not value.isprintable():
        raise ValueError("The name can't have surrounding spaces or control characters")
    return value


class ModelConfigRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    provider: str | dict[str, Any]
    visibility: Literal["public", "internal"] | None = None
    passthrough: Literal["detect", "opaque"] | None = None
    params: ModelParams = Field(default_factory=ModelParams)
    apiKey: str | None = Field(default=None, max_length=16384)
    removeApiKey: bool = False
    advanced: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def name_has_no_outer_space(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("provider", "advanced")
    @classmethod
    def limit_size(cls, value: Any) -> Any:
        _limit_json(value)
        return value


class ModelConfigUpdate(ModelConfigRequest):
    originalName: str = Field(min_length=1, max_length=256)
    revision: str = Field(min_length=1, max_length=64)


class McpTargetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    transport: Literal["mcp", "sse", "openapi", "stdio"]
    host: str | None = Field(default=None, max_length=MAX_MCP_TEXT_LENGTH)
    port: int | None = Field(default=None, ge=0, le=65535)
    path: str | None = Field(default=None, max_length=MAX_MCP_TEXT_LENGTH)
    schema_: str | None = Field(default=None, alias="schema", max_length=MAX_OPENAPI_SCHEMA_LENGTH)
    condition: str | None = Field(default=None, max_length=MAX_MCP_TEXT_LENGTH)
    authToken: str | None = Field(default=None, max_length=16384)
    removeAuthToken: bool = False
    policies: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def name_has_no_outer_space(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("policies")
    @classmethod
    def limit_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        _limit_json(value)
        return value

    def fields(self) -> dict[str, Any]:
        exclude = {"schema_", "originalName", "revision"}
        return {**self.model_dump(exclude=exclude), "schema": self.schema_}


class McpTargetUpdate(McpTargetRequest):
    originalName: str = Field(min_length=1, max_length=256)
    revision: str = Field(min_length=1, max_length=64)


def _refused_as_http(run):
    try:
        return run()
    except GatewayRequestRefused as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get("")
def get_ai_gateways(user: AuthUser = Depends(get_current_user)):
    summary = get_ai_gateways_summary()
    return summary if is_admin(user) else without_schema_values(summary)


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


@router.get("/models/config")
def get_model_configs(_: AuthUser = Depends(require_admin)):
    return _refused_as_http(list_model_configs)


@router.post("/models/config")
def add_model(body: ModelConfigRequest, user: AuthUser = Depends(require_admin)):
    logger.info("AI gateway model added by %s", user.username)
    return _refused_as_http(lambda: save_model(body.model_dump()))


@router.put("/models/config")
def update_model(body: ModelConfigUpdate, user: AuthUser = Depends(require_admin)):
    logger.info("AI gateway model changed by %s", user.username)
    fields = body.model_dump(exclude={"originalName", "revision"})
    return _refused_as_http(lambda: save_model(fields, body.originalName, body.revision))


@router.delete("/models/config")
def delete_model(
    name: str = Query(min_length=1, max_length=256), user: AuthUser = Depends(require_admin)
):
    logger.info("AI gateway model removed by %s", user.username)
    return _refused_as_http(lambda: remove_model(name))


@router.get("/mcp/targets")
def get_mcp_targets(_: AuthUser = Depends(require_admin)):
    return _refused_as_http(list_mcp_targets)


@router.post("/mcp/targets")
def add_mcp_target(body: McpTargetRequest, user: AuthUser = Depends(require_admin)):
    logger.info("AI gateway MCP server added by %s", user.username)
    return _refused_as_http(lambda: save_mcp_target(body.fields()))


@router.put("/mcp/targets")
def update_mcp_target(body: McpTargetUpdate, user: AuthUser = Depends(require_admin)):
    logger.info("AI gateway MCP server changed by %s", user.username)
    return _refused_as_http(lambda: save_mcp_target(body.fields(), body.originalName, body.revision))


@router.delete("/mcp/targets")
def delete_mcp_target(name: str = Query(min_length=1, max_length=256), user: AuthUser = Depends(require_admin)):
    logger.info("AI gateway MCP server removed by %s", user.username)
    return _refused_as_http(lambda: remove_mcp_target(name))


# JSONResponse skips FastAPI's recursive encoder, which deeply nested upstream data could overflow.
@router.post("/mcp/tools/call")
def call_tool(body: McpToolCallRequest, _: AuthUser = Depends(require_admin)):
    try:
        return JSONResponse(call_mcp_tool(body.name, body.arguments))
    except GatewayRequestRefused as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.post("/mcp/resources/read")
def read_resource(body: McpResourceReadRequest, _: AuthUser = Depends(require_admin)):
    try:
        return JSONResponse(read_mcp_resource(body.uri))
    except GatewayRequestRefused as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
