"""平台级契约端点：只读身份上下文。

设计约束（对应 Sprint 1 验收铁律与「禁止客户端伪造权限」）：
- `/me` 的身份**只**来自 AC-01 全局中间件算出的 `request.state.principal`
  与 `request.state.semantic_context`，不读请求体、不接受任何提权字段；
  响应是只读归一化投影，客户端改响应也不能反向影响服务端判定。
- `/health/cq-store` 把「当前是否真的在用 PostgreSQL」变成可查询事实，
  同时报出 production 判定与降级原因，避免「验收时 PG、上线后 memory」。
"""
from __future__ import annotations

import os

import re

from fastapi import APIRouter, Request

from app.modules.semantic_runtime.context import from_request
from app.policy import is_owner_class, principal_from

router = APIRouter()

_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _safe(value) -> str:
    """清洗控制字符：`principal_from(None)` 的 fail-closed 哨兵含 \\x00。"""
    return _CTRL.sub("", str(value if value is not None else ""))


@router.get("/me")
def me(request: Request):
    """返回当前 IdentityContext（只读，供前端展示与联调自证）。

    六项上下文由服务端统一注入；`source=global_middleware` 表示本次身份
    确实由 ASGI 全局闸门构造，而非端点自行解析请求头。
    """
    state = getattr(request, "state", None)
    ctx = getattr(state, "semantic_context", None)
    source = "global_middleware" if ctx is not None else "derived_fallback"
    if ctx is None:
        # 无中间件（进程内直接调用 / 伪造 Request）：仍按服务端规则构造，
        # fail-closed 回落最小主体，绝不信任请求体里的 is_admin 之类字段。
        ctx = from_request(request)
    principal = getattr(state, "principal", None) or principal_from(ctx)
    data = ctx.as_dict() if hasattr(ctx, "as_dict") else dict(ctx or {})
    perm = data.get("permission_context") or {}
    if not isinstance(perm, dict):
        perm = {}

    return {
        "identity": {
            "tenant_id": _safe(principal.get("tenant_id")),
            "user_id": _safe(principal.get("user_id")),
            "actor": _safe(principal.get("actor")),
            "role": _safe(principal.get("role")),
            "scope": sorted(_safe(s) for s in (principal.get("scope") or [])),
            "data_classification": _safe(principal.get("data_classification")),
            "is_owner_class": bool(is_owner_class(principal)),
        },
        "context": {
            "request_id": _safe(data.get("request_id")),
            "trace_id": _safe(data.get("trace_id")),
            "schema_version": _safe(data.get("schema_version")),
            "context_source": _safe(data.get("context_source")),
            "derived_fields": [_safe(x) for x in (data.get("derived_fields") or [])],
            "strict": bool(data.get("strict", False)),
        },
        "source": source,
        "writable": False,
        "request_body_trusted": False,
        "note": (
            "只读投影：身份由服务端 AC-01 全局中间件构造，请求体中的 "
            "tenant_id / is_admin / authorized_execution 一律不读取，"
            "修改本响应也不能反向影响服务端判定"
        ),
        "security_boundary": (
            "如实披露：X-Permission-Context 请求头目前仍参与主体构造，"
            "这是单主体部署的向后兼容契约；生产环境接入真实 IAM / 签名令牌前，"
            "「客户端无法伪造管理员权限」尚未达成，不得据此宣称达标"
        ),
        "environment": (
            "production" if _production() else "dev"
        ),
    }


def _production() -> bool:
    """平台环境判定，不依赖任何业务子系统。"""
    return os.getenv("APP_ENV", "dev").strip().lower() == "production"
