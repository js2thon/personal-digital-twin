"""执行仓库内权威基础 DDL，供首次部署和 CI 初始化共用。"""
from pathlib import Path

from app.db.session import get_engine


_REQUIRED_TABLES = (
    "source.documents",
    "ingest.pipeline_runs",
    "ingest.outbox_events",
    "knowledge.claims",
    "knowledge.facts",
    "knowledge.semantic_chunks",
    "ontology.entities",
    "ontology.relations",
    "memory.memory_items",
)


def initialize_base_schema() -> None:
    """按顺序幂等执行 db/init SQL，并验证核心表确实存在。

    基础 DDL 是新库初始化的唯一来源；模块 ensure_schema() 只负责后续增量升级。
    不吞掉 DDL 异常，避免应用在半初始化数据库上继续启动。
    """
    root = Path(__file__).resolve().parents[3]
    init_dir = root / "db" / "init"
    scripts = sorted(init_dir.glob("[0-9][0-9]_*.sql"))
    if not scripts:
        raise RuntimeError(f"database initialization scripts not found: {init_dir}")

    raw = get_engine().raw_connection()
    try:
        connection = raw.driver_connection
        with connection.transaction():
            for script in scripts:
                sql = script.read_text(encoding="utf-8")
                connection.execute(sql, prepare=False)

            missing = []
            for table in _REQUIRED_TABLES:
                found = connection.execute(
                    "SELECT to_regclass(%s)", (table,)
                ).fetchone()[0]
                if found is None:
                    missing.append(table)
            if missing:
                raise RuntimeError(
                    "database base schema is incomplete; missing tables: "
                    + ", ".join(missing)
                )
    finally:
        raw.close()
