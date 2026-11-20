"""结构化接入：映射契约 → 确定性断言 → 现有事实底座。

与文档蒸馏平行的旁路，依赖方向为 ingestion 层：
不产生 Claim / Evidence / Chunk，不调用 LLM，不引入新的存储中间件。
所有事实写入仍统一收口到 ontology.write_fact（含信任护栏与双时间线）。
"""
