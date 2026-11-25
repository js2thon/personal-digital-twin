"""
Ontology Studio V2 会话仓储。

默认使用 PostgreSQL 持久化；StudioV2Store 保留为可注入的内存实现，
便于单元测试。Ontology 以显式 JSON 状态保存，不使用 pickle。
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from .core import AutoOntologyBuilder, Ontology


@dataclass
class StudioSession:
    session_id: str
    ontology: Ontology
    name: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    source_tables: List[str] = field(default_factory=list)
    primary_keys: Dict[str, str] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)
    _on_change: Optional[Callable[["StudioSession"], None]] = field(
        default=None, repr=False, compare=False
    )

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat()
        if self._on_change is not None:
            self._on_change(self)

    def summary(self) -> Dict[str, Any]:
        s = self.ontology.summary_dict()
        return {
            "session_id": self.session_id,
            "name": self.name or self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_tables": self.source_tables,
            "primary_keys": self.primary_keys,
            "source_info": self.meta.get("source_info", {}),
            **s,
        }


class StudioV2Store:
    """进程内会话仓储；测试或显式内存模式使用。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: Dict[str, StudioSession] = {}

    def _attach(self, sess: StudioSession) -> StudioSession:
        return sess

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [s.summary() for s in self._sessions.values()]

    def get(self, session_id: str) -> Optional[StudioSession]:
        with self._lock:
            sess = self._sessions.get(session_id)
            return self._attach(sess) if sess else None

    def require(self, session_id: str) -> StudioSession:
        sess = self.get(session_id)
        if sess is None:
            raise KeyError(f"会话不存在: {session_id}")
        return sess

    def save(self, sess: StudioSession) -> None:
        with self._lock:
            self._sessions[sess.session_id] = self._attach(sess)

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def build_from_dataframes(
        self,
        tables: Dict[str, pd.DataFrame],
        primary_keys: Optional[Dict[str, str]] = None,
        manual_links: Optional[List[Dict[str, Any]]] = None,
        name: str = "",
        session_id: Optional[str] = None,
    ) -> StudioSession:
        builder = AutoOntologyBuilder()
        ontology = builder.from_dataframes(
            tables, primary_keys=primary_keys, manual_links=manual_links
        )
        sid = session_id or str(uuid.uuid4())
        sess = StudioSession(
            session_id=sid,
            ontology=ontology,
            name=name or sid[:8],
            source_tables=list(tables.keys()),
            primary_keys=primary_keys or {
                t: ontology.object_types[t]["primary_key"]
                for t in ontology.object_types
            },
        )
        self.save(sess)
        return sess

    def infer_primary_keys(self, tables: Dict[str, pd.DataFrame]) -> Dict[str, str]:
        return {
            name: AutoOntologyBuilder.infer_primary_key(df, name)
            for name, df in tables.items()
        }


class PersistentStudioV2Store(StudioV2Store):
    """PostgreSQL-backed session repository with lazy loading and JSON snapshots."""

    def __init__(self) -> None:
        super().__init__()
        self._loaded = False
        self._db_lock = threading.RLock()

    @staticmethod
    def _db():
        from app.db import session as db
        return db

    def _ensure_table(self) -> None:
        db = self._db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS studio_v2_sessions (
                session_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

    @staticmethod
    def _dump(sess: StudioSession) -> str:
        onto = sess.ontology
        state = {
            "session_id": sess.session_id,
            "name": sess.name,
            "created_at": sess.created_at,
            "updated_at": sess.updated_at,
            "source_tables": sess.source_tables,
            "primary_keys": sess.primary_keys,
            "meta": sess.meta,
            "ontology": {
                "object_types": onto.object_types,
                "objects": onto.objects,
                "link_types": onto.link_types,
                "data_mapping": onto.data_mapping,
                "actions_log": onto.actions_log,
            },
        }
        return json.dumps(state, ensure_ascii=False, default=str)

    @staticmethod
    def _restore(payload: str) -> StudioSession:
        state = json.loads(payload)
        ontology = Ontology()
        raw = state.get("ontology", {})
        ontology.object_types = raw.get("object_types", {})
        ontology.objects = raw.get("objects", {})
        ontology.link_types = raw.get("link_types", {})
        ontology.data_mapping = raw.get("data_mapping", {})
        ontology.actions_log = raw.get("actions_log", [])
        ontology.rebuild_links()
        return StudioSession(
            session_id=state["session_id"],
            ontology=ontology,
            name=state.get("name", ""),
            created_at=state.get("created_at") or datetime.now().isoformat(),
            updated_at=state.get("updated_at") or datetime.now().isoformat(),
            source_tables=state.get("source_tables", []),
            primary_keys=state.get("primary_keys", {}),
            meta=state.get("meta", {}),
        )

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._db_lock:
            if self._loaded:
                return
            db = self._db()
            self._ensure_table()
            rows = db.fetch_all("SELECT session_id, payload FROM studio_v2_sessions")
            with self._lock:
                for row in rows:
                    sess = self._restore(row["payload"])
                    self._sessions[sess.session_id] = self._attach(sess)
                self._loaded = True

    def _attach(self, sess: StudioSession) -> StudioSession:
        sess._on_change = self.save
        return sess

    def save(self, sess: StudioSession) -> None:
        self._ensure_table()
        payload = self._dump(sess)
        self._db().execute("""
            INSERT INTO studio_v2_sessions (session_id, payload, updated_at)
            VALUES (:session_id, :payload, NOW())
            ON CONFLICT (session_id) DO UPDATE
            SET payload = EXCLUDED.payload, updated_at = NOW()
        """, {"session_id": sess.session_id, "payload": payload})
        with self._lock:
            self._sessions[sess.session_id] = self._attach(sess)

    def list_sessions(self) -> List[Dict[str, Any]]:
        self._ensure_loaded()
        return super().list_sessions()

    def get(self, session_id: str) -> Optional[StudioSession]:
        self._ensure_loaded()
        return super().get(session_id)

    def delete(self, session_id: str) -> bool:
        self._ensure_loaded()
        existed = super().delete(session_id)
        self._ensure_table()
        self._db().execute(
            "DELETE FROM studio_v2_sessions WHERE session_id = :session_id",
            {"session_id": session_id},
        )
        return existed

    def build_from_dataframes(
        self,
        tables: Dict[str, pd.DataFrame],
        primary_keys: Optional[Dict[str, str]] = None,
        manual_links: Optional[List[Dict[str, Any]]] = None,
        name: str = "",
        session_id: Optional[str] = None,
    ) -> StudioSession:
        self._ensure_loaded()
        return super().build_from_dataframes(
            tables, primary_keys=primary_keys, manual_links=manual_links,
            name=name, session_id=session_id,
        )


# 默认使用数据库仓储；测试可直接实例化 StudioV2Store。
default_store = PersistentStudioV2Store()
