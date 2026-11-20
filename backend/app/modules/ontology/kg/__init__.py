"""本体驱动的知识图谱运行时（导入 → 灌数 → 只读 SPARQL → 语义问答）。

四个子模块各管一件事，互相之间只有单向依赖：

    model.py   任意 RDF 图 → 前端可画的设计模型（类/属性/个体/边/统计）
    store.py   问答/查询会话：一张 rdflib 图 + 来源清单，内存缓存 + 落盘
    query.py   只读 SPARQL 执行（PREFIX 自动补齐、禁止更新语句）
    qa.py      自然语言 → SPARQL → 结果 → 中文回答（模型不可用时模板兜底）

对外只暴露这四个模块，调用方（api/v1/kg.py）不需要感知内部细节。
"""
from app.modules.ontology.kg import model, query, qa, store  # noqa: F401  (按依赖顺序：model ← store ← query ← qa)

__all__ = ["model", "store", "query", "qa"]
