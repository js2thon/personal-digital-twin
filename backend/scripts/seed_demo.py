"""示例种子数据：模拟一次蒸馏后的候选知识、记忆与认知候选，便于启动后端后立即体验审批闭环。

用法：docker compose exec twin-api python scripts/seed_demo.py
（或本地：PYTHONPATH=backend python backend/scripts/seed_demo.py）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import session as db  # noqa: E402
from app.modules.audit import service as audit  # noqa: E402


def seed():
    # 实体
    person = db.insert_returning_id(
        "ontology.entities",
        {"entity_type": "Person", "canonical_name": "本人",
         "aliases": json.dumps(["我"]),
         "ontology_version_id": "00000000-0000-0000-0000-000000000001"},
    )
    proj = db.insert_returning_id(
        "ontology.entities",
        {"entity_type": "Project", "canonical_name": "知识中台项目",
         "aliases": json.dumps([]),
         "ontology_version_id": "00000000-0000-0000-0000-000000000001"},
    )
    db.execute(
        "INSERT INTO ontology.relations (source_entity_id, relation_type, "
        "target_entity_id, status) VALUES (:s, 'participated_in', :t, 'active')",
        {"s": person, "t": proj},
    )

    # Claim（候选，待人工审核转 Fact）
    db.execute(
        "INSERT INTO knowledge.claims (claim_type, subject, predicate, object_value, "
        "confidence, status, extraction_version) VALUES "
        "('preference', '{\"name\": \"本人\"}', 'prefers', "
        "'{\"value\": \"私有化部署企业个人数据\"}', 0.92, 'candidate', 'seed/v1'), "
        "('experience', '{\"name\": \"本人\"}', 'completed', "
        "'{\"value\": \"知识中台项目一期交付\"}', 0.95, 'candidate', 'seed/v1')"
    )

    # 记忆
    db.execute(
        "INSERT INTO memory.memory_items (memory_type, content, confidence, status) "
        "VALUES ('preference', '{\"preference\": \"private_deployment\", "
        "\"scope\": \"enterprise_personal_data\", \"evidence_count\": 4}', 0.9, 'confirmed')"
    )

    # 认知候选（待确认）
    db.execute(
        "INSERT INTO cognition.profile_proposals (profile_type, proposal, "
        "evidence_count, status) VALUES ('preference', "
        "'{\"preference\": \"private_deployment\", \"scope\": "
        "\"enterprise_personal_data\", \"status\": \"candidate\", "
        "\"effective_from\": \"2026-01-01\"}', 4, 'pending')"
    )

    # 决策记录 + 证据 + 审计快照 + 审批任务（完整演示闭环）
    # 早期版本只插 decision_records，留下一条既无 evidence 又无 snapshot 的孤立演示决策，
    # 会把 /lineage/overview 的「决策落库 / 审计快照」两项永久拖成 false。
    # 现在演示数据也走完整链路：证据行明确标注 demo 且不伪造 reference_id，
    # 快照如实记录决策内容并由 audit 服务加 HMAC 防篡改。
    input_context = {"project_id": "project_001", "budget": 800000,
                     "expected_duration": 90, "_demo": True}
    reasoning = ["历史项目预算范围一致", "与个人技术选型偏好一致"]
    did = db.insert_returning_id(
        "decision.decision_records",
        {
            "task_type": "project_approval",
            "input_context": json.dumps(input_context, ensure_ascii=False),
            "recommendation": "approve",
            "reasoning": json.dumps(reasoning, ensure_ascii=False),
            "risk_level": "medium",
            "confidence": 0.88,
            "policy_result": "approval_required",
            "status": "waiting_approval",
        },
    )
    db.execute(
        "INSERT INTO decision.decision_evidence "
        "(decision_id, evidence_type, reference_id, quote) "
        "VALUES (:d, 'demo_seed', NULL, :q)",
        {"d": did,
         "q": "演示种子决策：无真实证据引用（seed_demo.py），仅用于闭环演示"},
    )
    audit.save_decision_snapshot(
        str(did),
        {"_demo": True, "_source": "seed_demo.py", "input_context": input_context,
         "recommendation": "approve", "reasoning": reasoning,
         "risk_level": "medium", "confidence": 0.88,
         "policy_result": "approval_required", "status": "waiting_approval",
         "evidence": [], "profile_version": None},
    )
    db.insert_returning_id(
        "workflow.approval_tasks",
        {"decision_id": did, "approval_status": "pending"},
    )
    print("Seed data inserted. 决策 ID:", did)


if __name__ == "__main__":
    seed()
