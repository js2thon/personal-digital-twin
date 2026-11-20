"""检索自证端点（向量本体 P0）：验证向量是独立召回通道，而非重排因子。

selftest 用固定 10 条语义型 query，强制 terms=[]（词法 0 命中），
只看向量通道能否独立召回。标注集未建，不输出 Recall@k 数值。
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.modules.retrieval import service as retrieval

router = APIRouter()

_SELFTEST_QUERIES = (
    "数据治理由哪个部门牵头负责",
    "文档资料归档留存的规定是什么",
    "项目立项审批需要经过哪些环节",
    "新员工转正述职要准备哪些内容",
    "业务系统访问权限如何申请开通",
    "制度文件的合规性审查流程是怎样的",
    "跨部门协作时数据分类分级如何执行",
    "数据安全管理的相关要求有哪些",
    "技术方案评审的组织与决策方式",
    "历史数据保留与清除的处理原则",
)


@router.get("/retrieval/vector/selftest")
def vector_selftest(request: Request):
    """纯语义 query 的向量独立召回自检（只读，不改任何状态）。"""
    from app.modules.retrieval import vector_store as vs

    store = vs.get_vector_store()
    info = store.stats()
    p = getattr(getattr(request, "state", None), "principal", None)
    principal = p if isinstance(p, dict) else {"tenant_id": "default"}

    out: list[dict] = []
    for q in _SELFTEST_QUERIES:
        diagnostics: dict = {}
        # terms=[] 强制词法 0 命中：能返回结果只可能来自向量独立召回
        docs = retrieval._documents(q, [], 5, diagnostics, principal)
        out.append({
            "query": q,
            "vector_recall_used": bool(diagnostics.get("vector_recall_used")),
            "vector_candidates": int(diagnostics.get("vector_candidates") or 0),
            "vector_top_sim": diagnostics.get("vector_top_sim"),
            "top_title": (docs[0].get("title") if docs else None),
            "top_via": (docs[0].get("via") if docs else None),
        })

    hits = sum(1 for r in out if r["vector_recall_used"])
    return {
        "available": hits > 0,
        "backend": store.backend,
        "coverage": info["chunks"]["coverage"],
        "summary": {
            "total": len(out),
            "vector_recall_hit": hits,
            "avg_candidates": round(
                sum(r["vector_candidates"] for r in out) / max(len(out), 1), 2
            ),
        },
        "queries": out,
        "recall_at_5": {
            "available": False,
            "reason": "本自检端点不携带标注，不在此计算 Recall@5；"
                      "文档级质量指标请用 backend/scripts/eval_doc_retrieval.py "
                      "与 golden-doc-v1.1（方案 §9）",
        },
    }
