"""一次性迁移（#21）：把 graph.py 内置的 35 个关系类型并入 Registry 并发布新版本。

    cd backend && python -m scripts.migrate_type_registry          # 预览
    cd backend && python -m scripts.migrate_type_registry --apply  # 发布
"""
import json
import sys

from app.modules.ontology import cq_v2, type_registry


def main() -> int:
    eng = cq_v2.get_engine()
    eng.ensure_ready()
    print(json.dumps(type_registry.pending_patch(eng.registry), ensure_ascii=False, indent=2))
    if "--apply" in sys.argv:
        r = type_registry.publish_patch(eng, actor="migration")
        print(json.dumps({k: r.get(k) for k in ("published", "schema_version", "compatibility", "reason")},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
