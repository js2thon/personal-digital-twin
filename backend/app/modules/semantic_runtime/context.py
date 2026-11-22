"""SIL AC-01：统一语义上下文。

蓝图 9.3 要求「所有 API 必须包含 Tenant / User / Request / Trace / Permission /
Schema Version」六项上下文。原文并未规定必须由客户端传 header，因此本实现采用
「服务端统一注入 + 自动推导」：任何语义响应都必然带齐六项，且可追溯到 actor。

严格模式（SEMANTIC_STRICT_CONTEXT=1 或请求头 X-Semantic-Strict: 1）下，
六项必须由客户端显式提供，缺失即 400 —— 用于联调与 SIL-A01 验收，不影响
现有控制台与三张 LangGraph 图的调用。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from app.auth import allow_identity_context_headers, current_actor

CONTEXT_FIELDS = ("tenant_id", "user_id", "request_id", "trace_id",
                  "permission_context", "schema_version")
HEADER_MAP = {
    "tenant_id": "x-tenant-id",
    "user_id": "x-user-id",
    "request_id": "x-request-id",
    "trace_id": "x-trace-id",
    "permission_context": "x-permission-context",
    "schema_version": "x-schema-version",
}

# 已发布本体版本只需 30s 级新鲜度；避免每个语义请求都打一次库
_VERSION_CACHE: dict = {"value": "unversioned", "ts": 0.0}


def resolve_published_version() -> str:
    """取当前 published 本体版本；数据库不可用时不阻断语义服务。"""
    now = time.monotonic()
    if _VERSION_CACHE["ts"] and now - _VERSION_CACHE["ts"] < 30.0:
        return _VERSION_CACHE["value"]
    value = "unversioned"
    try:
        from app.db import session as db

        row = db.fetch_one(
            "SELECT version FROM ontology.ontology_versions "
            "WHERE status = 'published' ORDER BY created_at DESC LIMIT 1"
        )
        if row and row.get("version"):
            value = str(row["version"])
    except Exception:  # noqa: BLE001  版本解析失败只允许降级，不能让 API 500
        pass
    _VERSION_CACHE.update(value=value, ts=now)
    return value


@dataclass
class SemanticContext:
    tenant_id: str
    user_id: str
    request_id: str
    trace_id: str
    permission_context: dict
    schema_version: str
    actor: str = "owner"
    strict: bool = False
    derived: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "permission_context": self.permission_context,
            "schema_version": self.schema_version,
            "actor": self.actor,
            "context_source": "derived" if self.derived else "explicit",
            "derived_fields": list(self.derived),
            "strict": self.strict,
        }


def _strict_enabled(request: Request) -> bool:
    flag = (request.headers.get("x-semantic-strict") or "").strip().lower()
    if flag in ("1", "true", "yes"):
        return True
    return os.getenv("SEMANTIC_STRICT_CONTEXT", "0").strip().lower() in ("1", "true", "yes")


def from_request(request: Request) -> SemanticContext:
    """从请求构造上下文：缺失项自动推导；严格模式下缺失即 400。

    P2 收口（AC-01 全局中间件）：优先复用 `request.state.semantic_context`。
    中间件已在所有 /api/v1 请求上构造过一次，端点层复用它可保证六项上下文
    全局一致（含 request_id/trace_id），严格模式也因此对所有 API 生效，而不再
    只覆盖显式调用本函数的语义端点。无中间件时（进程内直接调用 / 单测伪造
    Request）回落原解析逻辑，行为不变。
    """
    cached = getattr(getattr(request, "state", None), "semantic_context", None)
    if isinstance(cached, SemanticContext):
        return cached
    actor = current_actor(request)
    strict = _strict_enabled(request)
    derived: list[str] = []
    h = request.headers
    trusted_identity = allow_identity_context_headers(request)

    def pick(name: str, fallback, *, trusted_only: bool = False) -> str:
        value = (h.get(HEADER_MAP[name]) or "").strip() if (not trusted_only or trusted_identity) else ""
        if value:
            return value
        derived.append(name)
        return fallback() if callable(fallback) else fallback

    tenant_id = pick("tenant_id", "default", trusted_only=True)
    user_id = pick("user_id", actor, trusted_only=True)
    request_id = pick("request_id", lambda: "req_" + uuid.uuid4().hex[:16])
    trace_id = pick("trace_id", lambda: "trc_" + uuid.uuid4().hex[:16])

    raw_perm = ((h.get(HEADER_MAP["permission_context"]) or "").strip()
                if trusted_identity else "")
    if raw_perm:
        try:
            permission = json.loads(raw_perm)
            if not isinstance(permission, dict):
                raise ValueError("permission context must be an object")
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, f"X-Permission-Context 不是合法 JSON 对象: {exc}") from None
    else:
        derived.append("permission_context")
        permission = {"role": actor, "scope": ["owner", "organization"],
                      "data_classification": "internal"}

    schema_version = pick("schema_version", resolve_published_version, trusted_only=True)

    if strict and derived:
        raise HTTPException(400, {
            "code": "MISSING_SEMANTIC_CONTEXT",
            "message": "严格模式下六项语义上下文必须由客户端显式提供",
            "missing": derived,
            "headers": {k: HEADER_MAP[k] for k in derived},
        })
    ctx = SemanticContext(tenant_id=tenant_id, user_id=user_id,
                          request_id=request_id, trace_id=trace_id,
                          permission_context=permission, schema_version=schema_version,
                          actor=actor, strict=strict, derived=derived)
    try:
        request.state.semantic_context = ctx
    except Exception:  # noqa: BLE001  伪造 Request 无 state 时不影响解析结果
        pass
    return ctx


def system_context() -> SemanticContext:
    """进程内调用（三张 LangGraph 图）使用的上下文，不经过 HTTP 头。"""
    return SemanticContext(
        tenant_id="default", user_id="system:agent",
        request_id="req_" + uuid.uuid4().hex[:16],
        trace_id="trc_" + uuid.uuid4().hex[:16],
        permission_context={"role": "owner", "scope": ["owner", "organization"],
                            "data_classification": "internal", "internal_call": True},
        schema_version=resolve_published_version(), actor="owner",
        strict=False, derived=["internal_call"],
    )
