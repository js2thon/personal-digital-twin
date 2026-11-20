"""本地演示环境初始化：执行 db/init DDL，对缺失的扩展优雅降级。
- pgvector 不可用时：semantic_chunks.embedding 用 TEXT 占位，语义检索走关键词降级
- pgcrypto 不可用时：主键默认值改用 gen_random_uuid() 的替代方案
"""
import re
from pathlib import Path

import psycopg

DDL_DIR = Path(__file__).resolve().parent.parent.parent / "db" / "init"
DSN = "postgresql://twin@127.0.0.1:5434/personal_twin"


def main():
    conn = psycopg.connect(DSN, autocommit=True)
    cur = conn.cursor()

    cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
    has_vector = bool(cur.fetchone())
    if not has_vector:
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            has_vector = True
        except psycopg.Error as e:
            print("pgvector 不可用，降级运行：", str(e).splitlines()[0])
    cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto'")
    if not cur.fetchone():
        try:
            cur.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
        except psycopg.Error as e:
            print("pgcrypto 不可用，降级运行：", str(e).splitlines()[0])

    for sql_file in sorted(DDL_DIR.glob("*.sql")):
        raw = sql_file.read_text(encoding="utf-8")
        # 按分号拆分逐条执行，失败跳过并记录
        statements = [s.strip() for s in re.split(r";\s*\n", raw) if s.strip()
                      and not all(l.strip().startswith("--") or not l.strip()
                                  for l in s.splitlines())]
        for stmt in statements:
            if "CREATE EXTENSION IF NOT EXISTS vector" in stmt:
                continue  # 已在上面尝试过
            if not has_vector and "VECTOR(" in stmt.upper():
                # 降级：embedding 用 TEXT 占位
                stmt = re.sub(r"embedding\s+VECTOR\(\d+\)", "embedding TEXT", stmt,
                              flags=re.IGNORECASE)
            body = "\n".join(
                l for l in stmt.splitlines() if not l.strip().startswith("--")
            ).strip()
            if not body:
                continue
            try:
                cur.execute(body)
            except psycopg.Error as e:
                msg = str(e).splitlines()[0]
                if "already exists" in msg or "duplicate" in msg:
                    continue
                print(f"[skip] {sql_file.name}: {msg[:100]}")
        print(f"[ok] {sql_file.name}")

    print("初始化完成。vector =", has_vector)
    conn.close()


if __name__ == "__main__":
    main()
