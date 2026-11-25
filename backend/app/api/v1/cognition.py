"""认知模块已下线（v0.2.0-no-cognition）。

历史表 cognition.profile_* 可保留在数据库中，本路由不再对外暴露。
若误被 include_router，所有端点返回 410 Gone。
"""
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.api_route("/cognition/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@router.api_route("/cognition", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def cognition_gone(path: str = ""):
    raise HTTPException(
        status_code=410,
        detail="cognition module has been removed in v0.2.0-no-cognition",
    )


def generate_proposal_from_claim(claim_id: str) -> dict:
    """兼容旧调用点：不再生成候选。"""
    return {"skipped": "cognition_module_removed"}
