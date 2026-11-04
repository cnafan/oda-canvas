from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from auth.middleware import get_current_user
from auth.models import AuthUser
from k8s.resources.ai_gateways import (
    ModelTestRefused,
    ensure_model_is_active,
    get_ai_gateways_summary,
    stream_model_test,
)

router = APIRouter(prefix="/api/ai-gateways", tags=["ai-gateways"])


class ModelTestRequest(BaseModel):
    model: str = Field(min_length=1, max_length=256)


@router.get("")
def get_ai_gateways(_: AuthUser = Depends(get_current_user)):
    return get_ai_gateways_summary()


@router.post("/models/test")
async def test_model(body: ModelTestRequest, _: AuthUser = Depends(get_current_user)):
    try:
        await ensure_model_is_active(body.model)
    except ModelTestRefused as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return StreamingResponse(
        stream_model_test(body.model),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )
