"""本体问答会话：一张 rdflib 图 + 来源清单。

进程内缓存 + 落盘（backend/.runtime/kg/<id>/），服务重启后按 id 惰性载回，
这样「导入—灌数—提问」不会因为一次重启全部丢失。落盘用 Turtle，
人类可读，出问题能直接打开看。
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import Graph

from app.modules.ontology.kg import model as kg_model

_STORE = Path(__file__).resolve().parents[4] / ".runtime" / "kg"
_LOCK = threading.RLock()
_MAX_SESSIONS = 20


@dataclass
class KgSession:
    id: str
    name: str
    created_at: float
    graph: Graph
    sources: list[dict] = field(default_factory=list)
    _model: dict | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def model(self) -> dict:
        with self._lock:
            if self._model is None:
                self._model = kg_model.build_model(self.graph)
            return self._model

    def invalidate(self) -> None:
        with self._lock:
            self._model = None

    def public(self) -> dict:
        m = self.model()
        return {
            "id": self.id, "name": self.name, "created_at": self.created_at,
            "ontology": m["ontology"], "stats": m["stats"],
            "sources": self.sources,
        }


_SESSIONS: dict[str, KgSession] = {}


def _dir(sid: str) -> Path:
    return _STORE / sid


def _persist(sess: KgSession) -> None:
    d = _dir(sess.id)
    d.mkdir(parents=True, exist_ok=True)
    with sess.lock:
        sess.graph.serialize(destination=str(d / "graph.ttl"), format="turtle")
    meta = {"id": sess.id, "name": sess.name, "created_at": sess.created_at,
            "sources": sess.sources}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _load(sid: str) -> KgSession | None:
    d = _dir(sid)
    meta_path = d / "meta.json"
    graph_path = d / "graph.ttl"
    if not meta_path.exists() or not graph_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    g = Graph()
    g.parse(str(graph_path), format="turtle")
    return KgSession(id=meta["id"], name=meta.get("name") or sid,
                     created_at=meta.get("created_at") or time.time(),
                     graph=g, sources=meta.get("sources") or [])


def create(name: str, graph: Graph, source: dict | None = None) -> KgSession:
    sid = uuid.uuid4().hex[:10]
    sess = KgSession(id=sid, name=name or "未命名本体", created_at=time.time(),
                     graph=graph, sources=[source] if source else [])
    with _LOCK:
        _SESSIONS[sid] = sess
        _evict()
    _persist(sess)
    return sess


def get(sid: str) -> KgSession | None:
    with _LOCK:
        sess = _SESSIONS.get(sid)
        if sess:
            return sess
        sess = _load(sid)
        if sess:
            _SESSIONS[sid] = sess
        return sess


def add_data(sess: KgSession, graph: Graph, source: dict) -> None:
    with sess.lock:
        for tri in graph:
            sess.graph.add(tri)
        sess.sources.append(source)
    sess.invalidate()
    _persist(sess)


def remove(sid: str) -> bool:
    with _LOCK:
        _SESSIONS.pop(sid, None)
    d = _dir(sid)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False


def listing() -> list[dict]:
    with _LOCK:
        out = []
        if _STORE.exists():
            for d in _STORE.iterdir():
                if not d.is_dir():
                    continue
                sess = get(d.name)
                if sess:
                    out.append(sess.public())
        return sorted(out, key=lambda s: -s["created_at"])


def _evict() -> None:
    """只清内存、不删盘：会话太多时把最旧的移出缓存，再访问时惰性载回。"""
    if len(_SESSIONS) <= _MAX_SESSIONS:
        return
    for sid, _ in sorted(_SESSIONS.items(), key=lambda kv: kv[1].created_at)[: len(_SESSIONS) - _MAX_SESSIONS]:
        _SESSIONS.pop(sid, None)
