from fastapi import APIRouter, Depends

from auth.middleware import get_current_user
from auth.models import AuthUser
from k8s.resources.ai_gateways import get_ai_gateways_summary

router = APIRouter(prefix="/api/ai-gateways", tags=["ai-gateways"])


@router.get("")
def get_ai_gateways(_: AuthUser = Depends(get_current_user)):
    return get_ai_gateways_summary()
