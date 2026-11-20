"""事实可见性白名单（唯一定义处）。

`facts.visibility` 此前是「写入了但检索没消费」的假实现：列在
`ontology/service.py::_MIGRATIONS` 补齐，`write_fact` 也会存值，但
`retrieval._facts` / `knowledge.list_facts` / `entity_profile` 都只过滤
status，导致 restricted 事实一旦落库就能被通用问答召回。

这里集中定义白名单，三处共用同一判定，避免规则漂移：
* owner（默认，本人可见）/ organization（组织内可见）→ 可进检索、列表、画像；
* restricted（敏感）/ 任何未知值 / 异常值 → fail-closed，一律不可召回。

本模块位于 app 顶层，不依赖任何领域模块，因此不会造成 ADR-002 的反向依赖。
详见 docs/structured-ingestion-assessment.md §3.3。
"""
RETRIEVABLE = ("owner", "organization")
RESTRICTED = "restricted"
VALID = set(RETRIEVABLE) | {RESTRICTED}


def retrievable_clause(alias: str = "") -> str:
    """返回可直接拼进 WHERE 的 SQL 片段（不含用户输入，拼接安全）。"""
    col = f"{alias}.visibility" if alias else "visibility"
    values = ", ".join(f"'{v}'" for v in RETRIEVABLE)
    return f"COALESCE({col}, 'owner') IN ({values})"


def is_retrievable(value) -> bool:
    v = str(value or "owner").strip().lower()
    return v in RETRIEVABLE
