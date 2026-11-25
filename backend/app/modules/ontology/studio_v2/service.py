"""
Ontology Studio V2 会话仓储。

内存实现，与 automodel 任务解耦；接入正式项目时可换成 Redis / DB。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

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

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat()

    def summary(self) -> Dict[str, Any]:
        s = self.ontology.summary_dict()
        return {
            "session_id": self.session_id,
            "name": self.name or self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_tables": self.source_tables,
            "primary_keys": self.primary_keys,
            **s,
        }


class StudioV2Store:
    """进程内会话表。单例由路由层持有。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: Dict[str, StudioSession] = {}

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [s.summary() for s in self._sessions.values()]

    def get(self, session_id: str) -> Optional[StudioSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def require(self, session_id: str) -> StudioSession:
        sess = self.get(session_id)
        if sess is None:
            raise KeyError(f"会话不存在: {session_id}")
        return sess

    def delete(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                return True
            return False

    def build_from_dataframes(
        self,
        tables: Dict[str, pd.DataFrame],
        primary_keys: Optional[Dict[str, str]] = None,
        manual_links: Optional[List[Dict[str, Any]]] = None,
        name: str = "",
        session_id: Optional[str] = None,
    ) -> StudioSession:
        """
        严格走 AutoOntologyBuilder.from_dataframes（与 V2 Streamlit 同源）。
        """
        builder = AutoOntologyBuilder()
        ontology = builder.from_dataframes(
            tables,
            primary_keys=primary_keys,
            manual_links=manual_links,
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
        with self._lock:
            self._sessions[sid] = sess
        return sess

    def infer_primary_keys(
        self, tables: Dict[str, pd.DataFrame]
    ) -> Dict[str, str]:
        result = {}
        for name, df in tables.items():
            result[name] = AutoOntologyBuilder.infer_primary_key(df, name)
        return result


# 模块级默认仓储，路由可注入替换
default_store = StudioV2Store()
