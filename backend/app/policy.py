"""SIL GV-01 / P1-4：RBAC+ABAC 逐点权限判定（唯一定义处）。

与 `app/visibility.py` 的分工：
* visibility.py 只回答「这一行是不是公开白名单」，是数据侧的静态属性；
* policy.py 回答「这个请求者能不能看这一行」，把 租户 / 主体 / 角色范围 /
  可见性 四个维度合成一个判定，读路径不允许各端点自己拼规则。

设计原则（对齐规范 GV-01 七条安全原则）：
* **fail-closed**：租户不一致、可见性未知/受限、范围缺失 → 一律拒绝；
  判定器自己出错也按拒绝处理，绝不把异常当成允许。
* **默认上下文向后兼容**：本部署是单主体（actor=owner，回环地址判定），
  AC-01 自动推导的默认 permission_context（role=owner, scope=[owner,
  organization]）必须看到且仅看到与升级前相同的行——即 owner/organization
  可见、restricted 不可见。租户过滤对 default 租户同理不缩水。
* **八维收窄**：规范列了 8 个权限维度；本实现落地其中可被现有数据支撑的
  5 个（Tenant / User / Role / Data Classification / Resource 中的可见性），
  Organization/Department 归并进 tenant_id 与 scope。真实多组织矩阵接入时
  只扩 `principal_from` 与 `decide`，三个过滤点不动。
"""
from __future__ import annotations

import time
from collections import deque

from app.visibility import RESTRICTED, RETRIEVABLE, VALID

# 判定规则名（审计与测试断言用）
R_TENANT = "tenant_mismatch"
R_VIS_UNKNOWN = "visibility_unknown"
R_VIS_RESTRICTED = "visibility_restricted"
R_ORG_SCOPE = "organization_scope"
R_OWNER_SCOPE = "owner_scope"
R_ALLOW_ORG = "allow_organization"
R_ALLOW_OWNER = "allow_owner_row"
R_ALLOW_OWNER_CLASS = "allow_owner_class"
R_DENY_DEFAULT = "deny_default"

_AUDIT_MAX = 500
_DENY_LOG: deque = deque(maxlen=_AUDIT_MAX)
_STATS: dict = {"decisions": 0, "denied": 0, "by_rule": {}}


def principal_from(ctx) -> dict:
    """从 SemanticContext（或同形 dict / None）提取归一化主体。

    None / 异常 → fail-closed 的最小主体（tenant 不可知即拒绝跨租户行）。
    """
    try:
        if ctx is None:
            raise ValueError("no context")
        if isinstance(ctx, dict):
            tenant = str(ctx.get("tenant_id") or "default")
            user = str(ctx.get("user_id") or ctx.get("actor") or "owner")
            actor = str(ctx.get("actor") or user)
            perm = ctx.get("permission_context") or {}
        else:
            tenant = str(getattr(ctx, "tenant_id", None) or "default")
            user = str(getattr(ctx, "user_id", None)
                       or getattr(ctx, "actor", None) or "owner")
            actor = str(getattr(ctx, "actor", None) or user)
            perm = getattr(ctx, "permission_context", None) or {}
        if not isinstance(perm, dict):
            perm = {}
        role = str(perm.get("role") or "viewer")
        raw_scope = perm.get("scope")
        if isinstance(raw_scope, str):
            scope = {s.strip() for s in raw_scope.split(",") if s.strip()}
        elif isinstance(raw_scope, (list, tuple, set)):
            scope = {str(s).strip() for s in raw_scope if str(s).strip()}
        else:
            scope = set()
        return {
            "tenant_id": tenant,
            "user_id": user,
            "actor": actor,
            "role": role,
            "scope": scope,
            "data_classification": str(perm.get("data_classification") or "internal"),
        }
    except Exception:  # noqa: BLE001  上下文不可用即按最小权限处理
        return {"tenant_id": "\x00__unknown__", "user_id": "", "actor": "",
                "role": "anonymous", "scope": set(),
                "data_classification": "public"}


def is_owner_class(principal: dict) -> bool:
    """owner 级主体：单主体部署的历史默认，等价于升级前的隐式授权。"""
    return principal.get("role") == "owner" and "owner" in (principal.get("scope") or set())


def decide(principal: dict, *, tenant_id=None, visibility=None,
           owner=None) -> dict:
    """单资源判定。返回 {allowed, reason, rule}，不抛异常。"""
    _STATS["decisions"] += 1
    out = {"allowed": False, "reason": "", "rule": R_DENY_DEFAULT}
    try:
        vis = str(visibility or "owner").strip().lower()
        # 1) 租户隔离：资源带租户且与主体不一致 → 拒绝（跨租户隔离第一闸）
        rt = str(tenant_id).strip() if tenant_id not in (None, "") else ""
        if rt and rt != str(principal.get("tenant_id")):
            out.update(reason=f"资源租户 {rt} 与请求者租户 "
                              f"{principal.get('tenant_id')} 不一致", rule=R_TENANT)
        # 2) 可见性：未知值 fail-closed；restricted 一律不进通用读路径
        elif vis not in VALID:
            out.update(reason=f"未知可见性 {visibility!r}，fail-closed", rule=R_VIS_UNKNOWN)
        elif vis == RESTRICTED:
            out.update(reason="restricted 仅限显式 include_restricted 的 owner 通道",
                       rule=R_VIS_RESTRICTED)
        # 3) organization：需要 organization 范围
        elif vis == "organization" and "organization" not in (principal.get("scope") or set()):
            out.update(reason="主体 scope 不含 organization", rule=R_ORG_SCOPE)
        elif vis == "organization":
            out.update(reason="组织内可见且主体具备 organization 范围",
                       rule=R_ALLOW_ORG, allowed=True)
        # 4) owner：owner 级主体全量可见；他人仅限本人写入的行
        elif is_owner_class(principal):
            out.update(reason="owner 级主体，可见全部白名单行",
                       rule=R_ALLOW_OWNER_CLASS, allowed=True)
        else:
            row_owner = str(owner).strip() if owner not in (None, "") else ""
            uid = {str(principal.get("user_id") or ""),
                   str(principal.get("actor") or "")}
            if not row_owner:
                out.update(reason="owner 行无归属人，非 owner 级主体不可见",
                           rule=R_OWNER_SCOPE)
            elif row_owner in uid:
                out.update(reason="本人写入的 owner 行", rule=R_ALLOW_OWNER, allowed=True)
            else:
                out.update(reason=f"owner 行归属 {row_owner}，非本人", rule=R_OWNER_SCOPE)
    except Exception as exc:  # noqa: BLE001  判定器出错按拒绝处理
        out.update(reason=f"判定异常 fail-closed: {exc}", rule=R_DENY_DEFAULT)
    if not out["allowed"]:
        _STATS["denied"] += 1
        _STATS["by_rule"][out["rule"]] = _STATS["by_rule"].get(out["rule"], 0) + 1
        _DENY_LOG.append({"ts": time.time(), "user": principal.get("user_id"),
                          "tenant": principal.get("tenant_id"), "rule": out["rule"],
                          "reason": out["reason"],
                          "resource_tenant": str(tenant_id or ""),
                          "visibility": str(visibility or "")})
        # P2 收口：拒判不再只留进程内环形缓冲。审计模块内部做聚合落库，
        # 且任何异常都被吞掉——绝不因审计失败让一次权限判定抛错。
        try:
            from app import security_audit

            security_audit.record(
                "policy_deny", rule=out["rule"], severity="block",
                detail={"reason": out["reason"],
                        "resource_tenant": str(tenant_id or ""),
                        "visibility": str(visibility or ""),
                        "row_owner": str(owner or "")},
                ctx={"tenant_id": principal.get("tenant_id"),
                     "user_id": principal.get("user_id"),
                     "actor": principal.get("actor")},
            )
        except Exception:  # noqa: BLE001  审计不可用不影响判定结果
            pass
    return out


def _visibility_condition(alias: str, p: dict, params: dict,
                          owner_col: str = "created_by") -> str:
    """可见性白名单 + owner 行归属条件（不含租户，供组合复用）。"""
    col = lambda c: f"{alias}.{c}" if alias else c  # noqa: E731
    vis_values = ", ".join(f"'{v}'" for v in RETRIEVABLE)
    if is_owner_class(p):
        return f"COALESCE({col('visibility')}, 'owner') IN ({vis_values})"
    org_ok = "TRUE" if "organization" in (p.get("scope") or set()) else "FALSE"
    params.setdefault("p_user2", str(p.get("user_id") or ""))
    params.setdefault("p_actor", str(p.get("actor") or ""))
    return (
        f"COALESCE({col('visibility')}, 'owner') IN ({vis_values})"
        f" AND (COALESCE({col('visibility')}, 'owner') = 'organization' AND {org_ok}"
        f"      OR COALESCE({col('visibility')}, 'owner') = 'owner'"
        f"          AND ({col(owner_col)} = :p_user2 OR {col(owner_col)} = :p_actor))"
    )


def read_clause_visibility_only(alias: str = "", principal: dict | None = None,
                                params: dict | None = None,
                                *, owner_col: str = "created_by") -> str:
    """仅可见性 + 归属条件（租户条件由调用方自行拼接时用）。"""
    p = principal or principal_from(None)
    binds = params if params is not None else {}
    return _visibility_condition(alias, p, binds, owner_col=owner_col)


def read_clause(alias: str = "", principal: dict | None = None,
                params: dict | None = None, *, owner_col: str = "created_by",
                tenant_col: str = "tenant_id") -> str:
    """生成可直接 AND 进 WHERE 的逐点过滤 SQL（参数绑定，无拼接注入面）。

    与 `decide()` 同一套规则：租户一致 + 可见性白名单 + owner 行归属。
    alias 为空时用裸列名（子查询里列名已唯一的场景）。
    """
    p = principal or principal_from(None)
    binds = params if params is not None else {}
    col = lambda c: f"{alias}.{c}" if alias else c  # noqa: E731
    binds.setdefault("p_tenant", str(p.get("tenant_id")))
    binds.setdefault("p_user", str(p.get("user_id") or ""))
    return (f"COALESCE({col(tenant_col)}, 'default') = :p_tenant"
            f" AND {_visibility_condition(alias, p, binds, owner_col=owner_col)}")


def filter_rows(rows: list[dict], principal: dict, *, tenant_key: str = "tenant_id",
                vis_key: str = "visibility", owner_key: str = "created_by") -> list[dict]:
    """内存逐行过滤（图遍历逐跳 / 装配层），与 decide() 同一套规则。"""
    kept = []
    for r in rows or []:
        v = decide(principal, tenant_id=(r or {}).get(tenant_key),
                   visibility=(r or {}).get(vis_key), owner=(r or {}).get(owner_key))
        if v["allowed"]:
            kept.append(r)
    return kept


def recent(limit: int = 50) -> list[dict]:
    """最近拒判记录（审计：谁在什么时候被挡了什么）。"""
    items = list(_DENY_LOG)[-max(1, min(int(limit), _AUDIT_MAX)):]
    return list(reversed(items))


def stats() -> dict:
    """判定统计：进程内累计，用于 meta 自证与评估。"""
    return {"decisions": _STATS["decisions"], "denied": _STATS["denied"],
            "by_rule": dict(_STATS["by_rule"]),
            "deny_log_size": len(_DENY_LOG)}


def reset_stats() -> None:
    _STATS.update(decisions=0, denied=0, by_rule={})
    _DENY_LOG.clear()


def conformance_cases() -> list[tuple[dict, dict, bool]]:
    """权限判定符合性矩阵：`permission_filter_accuracy` 与回归测试共用。

    每项 = (主体上下文, 资源属性, 期望允许)。覆盖：跨租户隔离、restricted
    fail-closed、未知可见性 fail-closed、organization 范围、owner 行归属、
    owner 级主体、匿名上下文 fail-closed。改 `decide()` 规则必须先过这里。
    """
    owner_p = {"tenant_id": "default", "user_id": "boss",
               "permission_context": {"role": "owner",
                                      "scope": ["owner", "organization"]}}
    org_p = {"tenant_id": "default", "user_id": "u1",
             "permission_context": {"role": "member", "scope": ["organization"]}}
    bare_p = {"tenant_id": "default", "user_id": "u2",
              "permission_context": {"role": "member", "scope": []}}
    return [
        # owner 级主体：白名单可见性全量可见，restricted/未知/跨租户仍拒绝
        (owner_p, {"tenant_id": "default", "visibility": "owner"}, True),
        (owner_p, {"tenant_id": "default", "visibility": "organization"}, True),
        (owner_p, {"tenant_id": "default", "visibility": "restricted"}, False),
        (owner_p, {"tenant_id": "default", "visibility": "secret"}, False),
        (owner_p, {"tenant_id": "default", "visibility": None}, True),
        (owner_p, {"tenant_id": "tenant_b", "visibility": "owner"}, False),
        # organization 范围主体
        (org_p, {"tenant_id": "default", "visibility": "organization"}, True),
        (org_p, {"tenant_id": "default", "visibility": "owner", "owner": "u1"}, True),
        (org_p, {"tenant_id": "default", "visibility": "owner", "owner": "u2"}, False),
        (org_p, {"tenant_id": "default", "visibility": "owner", "owner": None}, False),
        (org_p, {"tenant_id": "default", "visibility": "restricted"}, False),
        (org_p, {"tenant_id": "tenant_b", "visibility": "organization"}, False),
        # 无 scope 主体：仅本人 owner 行，organization 行不可见
        (bare_p, {"tenant_id": "default", "visibility": "owner", "owner": "u2"}, True),
        (bare_p, {"tenant_id": "default", "visibility": "owner", "owner": "u1"}, False),
        (bare_p, {"tenant_id": "default", "visibility": "organization"}, False),
        (bare_p, {"tenant_id": "default", "visibility": "restricted"}, False),
        # 匿名 / 上下文缺失：fail-closed 全拒
        (None, {"tenant_id": "default", "visibility": "organization"}, False),
        (None, {"tenant_id": "default", "visibility": "owner"}, False),
    ]
