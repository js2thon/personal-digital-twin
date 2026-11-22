"""CI：类型体系一致性（#21）。python -m scripts.check_type_registry ；不一致则退出码 1。"""
import sys

from app.modules.ontology import cq_v2, type_registry


def main() -> int:
    eng = cq_v2.get_engine()
    type_registry.install(eng.registry)
    errs = type_registry.check(eng.registry)
    for e in errs:
        print("✗", e)
    print("OK" if not errs else f"{len(errs)} 项不一致")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
