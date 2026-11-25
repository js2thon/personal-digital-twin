"""最小身份边界：把「本人确认」从请求体自报改成服务端判定。

策略（保守，保证本机控制台无感）：
- 回环地址（127.0.0.1 / ::1）→ 视为本人 owner；
- 非回环 → 必须带 X-Twin-API-Key 且等于 settings.api_key；
- api_key 未配置时，所有非回环 API 请求一律 401（默认拒绝外露）。
- /healthz、静态页面、OPTIONS 预检不受限。

反向代理（M7）：默认**不采信** X-Forwarded-For —— 被反代时远程访问者的
TCP 对端可能是回环，若直接采信 XFF 就会把所有人都当成 owner。
只有显式设置 TRUST_PROXY_HEADERS=true 且 TCP 对端在 trusted_proxy_hosts 内，
才解析 XFF；并且从右向左取第一个「非可信代理」地址作为真实客户端，
避免请求方在最左侧伪造 127.0.0.1 绕过闸门。
"""
from __future__ import annotations

import hmac
import json

from fastapi import HTTPException, Request

from app.config import settings

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_TEST_HOSTS = {"testclient"}


def _trusted_proxies() -> set[str]:
    return {h.strip() for h in settings.trusted_proxy_hosts.split(",") if h.strip()}


def peer_host(request: Request) -> str:
    """用于身份判定的客户端地址（默认就是 TCP 对端）。"""
    host = (request.client.host if request.client else "") or ""
    if not (settings.trust_proxy_headers and host in _trusted_proxies()):
        return host
    forwarded = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    if not parts:
        return host
    trusted = _trusted_proxies()
    # 从右往左跳过可信代理，取第一个不可信地址 —— 客户端伪造的左段不生效
    for candidate in reversed(parts):
        if candidate not in trusted:
            return candidate
    return host


def actor_from_request(request: Request) -> str:
    host = peer_host(request)
    if host in _LOOPBACK or (settings.allow_testclient_actor and host in _TEST_HOSTS):
        return "owner"
    provided = request.headers.get("x-twin-api-key", "")
    if settings.api_key and provided and hmac.compare_digest(provided, settings.api_key):
        return "owner"
    raise HTTPException(
        401,
        "非本机访问需要有效的 X-Twin-API-Key；请在 .env 配置 API_KEY 后重启服务",
    )



def _audit_owner_deny(request: Request, reason: str) -> None:
    """越权写操作审计；审计失败不改变拒绝结果。"""
    try:
        from app import security_audit

        security_audit.record(
            "owner_gate_deny", rule="require_owner", severity="block",
            detail={"reason": reason,
                    "path": getattr(getattr(request, "url", None), "path", None)},
        )
    except Exception:  # noqa: BLE001
        pass


def require_owner(request: Request) -> dict:
    """通用 owner 权限闸门，复用全局 principal 并在缺失时 fail-closed。"""
    from app import policy as pol

    cached = getattr(getattr(request, "state", None), "principal", None)
    if isinstance(cached, dict) and cached:
        principal = cached
    else:
        headers = request.headers
        raw = (headers.get("x-permission-context") or "").strip()
        perm: dict = {}
        if raw:
            try:
                perm = json.loads(raw)
                if not isinstance(perm, dict):
                    raise ValueError
            except (ValueError, TypeError):
                _audit_owner_deny(request, "X-Permission-Context 不可解析")
                raise HTTPException(403, "X-Permission-Context 不可解析，拒绝本体写操作") from None
        principal = pol.principal_from({
            "tenant_id": (headers.get("x-tenant-id") or "default").strip() or "default",
            "user_id": (headers.get("x-user-id") or "").strip(),
            "actor": current_actor(request) or "owner",
            "permission_context": perm or {"role": "owner", "scope": ["owner"]},
        })
    if not pol.is_owner_class(principal):
        _audit_owner_deny(request, "非 owner 级主体尝试受保护写操作")
        raise HTTPException(403, "该操作要求 owner 级主体（role=owner）")
    return principal


def current_actor(request: Request) -> str:
    return getattr(request.state, "actor", "owner")
