"""数据库会话与查询助手（SQLAlchemy Core + psycopg3）。

事务边界：所有 db.fetch_*/execute/insert_returning_id 都自动感知当前事务。
在 `with db.transaction():` 内的多次写操作共用同一连接、同一事务，
任一环节抛异常则整体回滚；嵌套 transaction 复用外层事务，不提前提交。
ContextVar 天然按线程/协程隔离，蒸馏后台线程不会串进请求事务。
"""
import contextvars
import threading
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings

_engine = None
SessionLocal = None
_ENGINE_LOCK = threading.Lock()
_TX_CONN: contextvars.ContextVar = contextvars.ContextVar("twin_tx_conn", default=None)


def get_engine():
    """惰性创建引擎：导入期不要求安装数据库驱动，仅在真正访问数据库时连接。"""
    global _engine, SessionLocal
    if _engine is None:
        with _ENGINE_LOCK:  # 双检加锁：首个并发请求可能重复建引擎
            if _engine is None:
                _engine = create_engine(settings.database_url, pool_pre_ping=True,
                                        future=True)
                SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


@contextmanager
def get_session():
    get_engine()
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def transaction():
    """把一段业务写操作包进单个数据库事务；可嵌套。"""
    outer = _TX_CONN.get()
    if outer is not None:
        yield outer
        return
    with get_engine().begin() as conn:
        token = _TX_CONN.set(conn)
        try:
            yield conn
        finally:
            _TX_CONN.reset(token)


def in_transaction() -> bool:
    return _TX_CONN.get() is not None


def fetch_all(sql: str, params: dict | None = None) -> list[dict]:
    active = _TX_CONN.get()
    if active is not None:
        result = active.execute(text(sql), params or {})
        return [dict(row._mapping) for row in result]
    with get_engine().connect() as conn:
        result = conn.execute(text(sql), params or {})
        rows = [dict(row._mapping) for row in result]
        conn.commit()  # SQLAlchemy 2.0 需显式提交（含 INSERT ... RETURNING）
        return rows


def fetch_one(sql: str, params: dict | None = None) -> dict | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: dict | None = None) -> None:
    active = _TX_CONN.get()
    if active is not None:
        active.execute(text(sql), params or {})
        return
    with get_engine().begin() as conn:
        conn.execute(text(sql), params or {})


def executemany(sql: str, params_list: list[dict]) -> None:
    """批量执行同一 SQL（psycopg3 executemany 管道模式），自动感知当前事务。"""
    if not params_list:
        return
    active = _TX_CONN.get()
    if active is not None:
        active.execute(text(sql), params_list)
        return
    with get_engine().begin() as conn:
        conn.execute(text(sql), params_list)


def insert_returning_id(table: str, values: dict) -> str:
    keys = ", ".join(values.keys())
    binds = ", ".join(f":{k}" for k in values)
    return fetch_one(
        f"INSERT INTO {table} ({keys}) VALUES ({binds}) RETURNING id", values
    )["id"]
