#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ontology Studio V2 核心逻辑
严格复用 ontology_studio_v2.py 中的数据模型、自动建模、质量检查、
路径查询、LLM 工具与导出逻辑；去除全部 Streamlit / PyVis 页面依赖。

图谱接口返回 JSON 节点/边，供前端（automodel.html 等）自行渲染。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any, Dict, Generator, List, Optional, Tuple

import networkx as nx
import pandas as pd
from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore


# ---------------------------------------------------------------------
# 工具函数（来自 ontology_studio_v2.py）
# ---------------------------------------------------------------------
def json_safe(value: Any) -> Any:
    """将 Pandas / NumPy 类型转换为可 JSON 序列化类型。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def safe_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    return [
        {str(k): json_safe(v) for k, v in row.items()}
        for row in df.to_dict(orient="records")
    ]


def make_node_id(type_name: str, pk: Any) -> str:
    return f"{type_name}:{pk}"


# ---------------------------------------------------------------------
# 本体数据模型（来自 ontology_studio_v2.py :: Ontology）
# ---------------------------------------------------------------------
class Ontology:
    def __init__(self) -> None:
        self.object_types: Dict[str, Dict[str, Any]] = {}
        self.objects: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.links = nx.MultiDiGraph()
        self.link_types: Dict[str, Dict[str, Any]] = {}
        self.data_mapping: Dict[str, Dict[str, Any]] = {}
        self.actions_log: List[Dict[str, Any]] = []

    def add_object_type(
        self,
        name: str,
        primary_key: str,
        properties: List[str],
        description: str = "",
    ) -> None:
        self.object_types[name] = {
            "primary_key": primary_key,
            "properties": list(properties),
            "description": description,
        }
        self.objects.setdefault(name, {})

    def add_objects(self, type_name: str, records: List[Dict[str, Any]]) -> None:
        if type_name not in self.object_types:
            raise ValueError(f"ObjectType '{type_name}' 不存在")
        pk_field = self.object_types[type_name]["primary_key"]

        for record in records:
            clean = {str(k): json_safe(v) for k, v in record.items()}
            pk_value = clean.get(pk_field)
            if pk_value is None or str(pk_value).strip() == "":
                continue

            key = str(pk_value)
            self.objects[type_name][key] = clean
            self.links.add_node(
                make_node_id(type_name, key),
                type=type_name,
                pk=key,
                label=f"{type_name}:{key}",
                **{str(k): str(v) for k, v in clean.items() if v is not None},
            )

    def add_link_type(
        self,
        name: str,
        source_type: str,
        target_type: str,
        description: str = "",
        source_property: str = "",
        target_property: str = "",
    ) -> None:
        self.link_types[name] = {
            "source": source_type,
            "target": target_type,
            "description": description,
            "source_property": source_property,
            "target_property": target_property,
        }

    # ---------------- Schema 编辑（图编辑器用） ----------------
    def update_object_type(
        self, type_name: str, label: str = "", description: str = ""
    ) -> bool:
        """编辑类的中文名 / 描述。"""
        info = self.object_types.get(type_name)
        if not info:
            return False
        if label:
            info["label"] = label
        if description is not None:
            info["description"] = description
        return True

    def add_property(self, type_name: str, property_name: str, *,
                     source_column: str = "", property_type: str = "string",
                     description: str = "") -> bool:
        """Add a schema property and optionally bind it to an existing source column."""
        info = self.object_types.get(type_name)
        if not info or not property_name or property_name in info.get("properties", []):
            return False
        columns = (self.data_mapping.get(type_name) or {}).get("columns", [])
        if source_column and source_column not in columns:
            raise ValueError(f"源字段不存在：{type_name}.{source_column}")
        info.setdefault("properties", []).append(property_name)
        info.setdefault("property_definitions", {})[property_name] = {
            "type": property_type or "string", "description": description or "",
        }
        info.setdefault("property_mappings", {})[property_name] = {
            "source_table": type_name, "source_column": source_column,
        }
        if source_column:
            for obj in self.objects.get(type_name, {}).values():
                obj[property_name] = obj.get(source_column)
        return True

    def update_property(self, type_name: str, property_name: str, *,
                        new_name: str = "", source_column: str = "",
                        property_type: str = "", description: str = "",
                        clear_source_mapping: bool = False) -> bool:
        info = self.object_types.get(type_name)
        props = info.get("properties", []) if info else []
        if not info or property_name not in props:
            return False
        columns = (self.data_mapping.get(type_name) or {}).get("columns", [])
        if source_column and source_column not in columns:
            raise ValueError(f"源字段不存在：{type_name}.{source_column}")
        target_name = new_name or property_name
        if target_name != property_name and target_name in props:
            raise ValueError(f"属性已存在：{target_name}")
        if target_name != property_name:
            props[props.index(property_name)] = target_name
            for obj in self.objects.get(type_name, {}).values():
                if property_name in obj:
                    obj[target_name] = obj.pop(property_name)
            defs = info.setdefault("property_definitions", {})
            defs[target_name] = defs.pop(property_name, {})
            maps = info.setdefault("property_mappings", {})
            if property_name in maps:
                maps[target_name] = maps.pop(property_name)
        definition = info.setdefault("property_definitions", {}).setdefault(target_name, {})
        if property_type:
            definition["type"] = property_type
        if description is not None:
            definition["description"] = description
        if source_column:
            info.setdefault("property_mappings", {})[target_name] = {
                "source_table": type_name, "source_column": source_column,
            }
            for obj in self.objects.get(type_name, {}).values():
                obj[target_name] = obj.get(source_column)
        elif clear_source_mapping:
            info.setdefault("property_mappings", {})[target_name] = {
                "source_table": type_name, "source_column": "",
            }
        return True

    def remove_property(self, type_name: str, property_name: str) -> bool:
        info = self.object_types.get(type_name)
        if not info or property_name not in info.get("properties", []):
            return False
        if property_name == info.get("primary_key"):
            raise ValueError("不能删除对象主键属性")
        info["properties"].remove(property_name)
        info.get("property_definitions", {}).pop(property_name, None)
        info.get("property_mappings", {}).pop(property_name, None)
        for obj in self.objects.get(type_name, {}).values():
            obj.pop(property_name, None)
        return True

    def remove_object_type(self, type_name: str) -> bool:
        """删除类：同时删除其全部实例节点、属性与关联边。"""
        if type_name not in self.object_types:
            return False
        del self.object_types[type_name]
        self.objects.pop(type_name, None)
        # 删除该类的全部节点（含其参与的边）
        dead = [n for n, d in self.links.nodes(data=True) if d.get("type") == type_name]
        self.links.remove_nodes_from(dead)
        # 删除以该类为端点的关系定义
        for name in [
            n
            for n, i in list(self.link_types.items())
            if i["source"] == type_name or i["target"] == type_name
        ]:
            del self.link_types[name]
        return True

    def update_link_type(
        self,
        name: str,
        description: str = "",
        source_property: str = "",
        source: str = "",
        target: str = "",
        target_property: str = "",
        new_name: str = "",
        clear_source_property: bool = False,
        clear_target_property: bool = False,
    ) -> bool:
        """编辑关系：名称 / 描述 / 起点 / 终点 / 起点属性 / 终点属性。

        起点/终点改为业务对象（类）名；起点属性即源对象上用于匹配的列，
        终点属性即目标对象上被引用的列（通常是主键）。
        修改后调用方需 rebuild_links() 重建实例边。
        """
        info = self.link_types.get(name)
        if not info:
            return False
        if source and source in self.object_types:
            info["source"] = source
        if target and target in self.object_types:
            info["target"] = target
        if source_property:
            info["source_property"] = source_property
        elif clear_source_property:
            info["source_property"] = ""
        if target_property:
            info["target_property"] = target_property
        elif clear_target_property:
            info["target_property"] = ""
        if description is not None:
            info["description"] = description
        if new_name and new_name != name and new_name not in self.link_types:
            # 改名：迁移定义 + 边上的 relation_type/key
            self.link_types[new_name] = info
            del self.link_types[name]
            for u, v, k, d in list(self.links.edges(keys=True, data=True)):
                if d.get("relation_type") == name or k == name:
                    self.links.remove_edge(u, v, k)
                    d2 = dict(d)
                    d2["relation_type"] = new_name
                    d2["label"] = new_name
                    self.links.add_edge(u, v, key=new_name, **d2)
        return True

    def remove_link_type(self, name: str) -> bool:
        """删除关系定义及其全部边。"""
        if name not in self.link_types:
            return False
        del self.link_types[name]
        dead = [
            (u, v, k)
            for u, v, k, d in self.links.edges(keys=True, data=True)
            if d.get("relation_type") == name or k == name
        ]
        self.links.remove_edges_from(dead)
        return True

    def rebuild_links(self) -> None:
        """按 objects + link_types 重建全部实例边（编辑后调用）。"""
        self.links.clear()
        for type_name, objs in self.objects.items():
            info = self.object_types.get(type_name) or {}
            pk = info.get("primary_key", "")
            for pk_value, obj in objs.items():
                node = make_node_id(type_name, pk_value)
                self.links.add_node(
                    node, type=type_name, pk=str(pk_value), label=f"{type_name}:{pk_value}", data=obj
                )
                if pk and pk not in obj:
                    self.links.nodes[node]["data"] = {pk: pk_value, **obj}
        for name, info in self.link_types.items():
            src_type, tgt_type = info["source"], info["target"]
            src_col = info.get("source_property", "")
            target_info = self.object_types.get(tgt_type) or {}
            target_pk = target_info.get("primary_key", "")
            target_col = info.get("target_property") or target_pk
            target_index: Dict[str, str] = {}
            ambiguous: set[str] = set()
            for target_id, target_obj in self.objects.get(tgt_type, {}).items():
                target_value = target_obj.get(target_col) if target_col else target_id
                if target_value is None:
                    continue
                key = str(target_value)
                if key in target_index:
                    ambiguous.add(key)
                else:
                    target_index[key] = str(target_id)
            for pk_value, obj in self.objects.get(src_type, {}).items():
                if not src_col:
                    continue
                value = obj.get(src_col)
                if value is None:
                    continue
                value = str(value)
                target_id = target_index.get(value)
                if target_id is None or value in ambiguous:
                    continue  # 悬空或非唯一目标引用不生成实例边
                self.links.add_edge(
                    make_node_id(src_type, pk_value),
                    make_node_id(tgt_type, target_id),
                    key=name,
                    label=name,
                    relation_type=name,
                    source_type=src_type,
                    target_type=tgt_type,
                    source_pk=str(pk_value),
                    target_pk=target_id,
                    description=info.get("description", ""),
                )

    def add_links(
        self,
        link_name: str,
        pairs: List[Tuple[str, str]],
    ) -> None:
        if link_name not in self.link_types:
            return

        info = self.link_types[link_name]
        src_type, tgt_type = info["source"], info["target"]

        for src_pk, tgt_pk in pairs:
            src_node = make_node_id(src_type, src_pk)
            tgt_node = make_node_id(tgt_type, tgt_pk)
            if src_node in self.links and tgt_node in self.links:
                self.links.add_edge(
                    src_node,
                    tgt_node,
                    key=link_name,
                    label=link_name,
                    relation_type=link_name,
                    source_type=src_type,
                    target_type=tgt_type,
                    source_pk=str(src_pk),
                    target_pk=str(tgt_pk),
                    description=info.get("description", ""),
                )

    def create_object(self, type_name: str, data: Dict[str, Any]) -> Optional[str]:
        if type_name not in self.object_types:
            return None

        pk_field = self.object_types[type_name]["primary_key"]
        clean = {str(k): json_safe(v) for k, v in data.items()}
        if clean.get(pk_field) is None or str(clean.get(pk_field)).strip() == "":
            clean[pk_field] = str(uuid.uuid4())[:8]

        key = str(clean[pk_field])
        if key in self.objects[type_name]:
            raise ValueError(f"主键 {key} 已存在")

        self.objects[type_name][key] = clean
        self.links.add_node(
            make_node_id(type_name, key),
            type=type_name,
            pk=key,
            label=f"{type_name}:{key}",
            **{str(k): str(v) for k, v in clean.items() if v is not None},
        )
        self.actions_log.append({
            "action": "create", "type": type_name, "pk": key,
            "data": clean, "timestamp": datetime.now().isoformat(),
        })
        return key

    def update_object(
        self, type_name: str, primary_key: str, updates: Dict[str, Any]
    ) -> bool:
        key = str(primary_key)
        if type_name not in self.objects or key not in self.objects[type_name]:
            return False

        pk_field = self.object_types[type_name]["primary_key"]
        if pk_field in updates and str(updates[pk_field]) != key:
            raise ValueError("不允许通过更新操作修改主键")

        clean_updates = {k: json_safe(v) for k, v in updates.items()}
        self.objects[type_name][key].update(clean_updates)
        node_id = make_node_id(type_name, key)

        if node_id in self.links:
            for k, v in clean_updates.items():
                self.links.nodes[node_id][k] = str(v) if v is not None else None

        self.actions_log.append({
            "action": "update", "type": type_name, "pk": key,
            "updates": clean_updates, "timestamp": datetime.now().isoformat(),
        })
        return True

    def delete_object(self, type_name: str, primary_key: str) -> bool:
        key = str(primary_key)
        if type_name not in self.objects or key not in self.objects[type_name]:
            return False

        del self.objects[type_name][key]
        node_id = make_node_id(type_name, key)
        if node_id in self.links:
            self.links.remove_node(node_id)

        self.actions_log.append({
            "action": "delete", "type": type_name, "pk": key,
            "timestamp": datetime.now().isoformat(),
        })
        return True

    def summary_dict(self) -> Dict[str, Any]:
        """结构化摘要（替代 Streamlit markdown summary）。"""
        types = []
        for type_name, info in self.object_types.items():
            types.append({
                "name": type_name,
                "count": len(self.objects.get(type_name, {})),
                "primary_key": info["primary_key"],
                "properties": info["properties"],
                "description": info.get("description", ""),
            })
        return {
            "object_types": types,
            "link_types": [
                {"name": n, **info} for n, info in self.link_types.items()
            ],
            "node_count": self.links.number_of_nodes(),
            "edge_count": self.links.number_of_edges(),
            "action_count": len(self.actions_log),
        }

    def summary(self) -> str:
        lines = ["### 本体摘要"]
        lines.append(f"- **Object Types**：{list(self.object_types.keys())}")
        for type_name, info in self.object_types.items():
            count = len(self.objects.get(type_name, {}))
            lines.append(
                f"  - `{type_name}`：**{count}** 个对象；"
                f"主键：`{info['primary_key']}`；"
                f"属性数：{len(info['properties'])}"
            )
        lines.append(f"- **Link Types**：{list(self.link_types.keys())}")
        lines.append(f"- **实体节点数**：{self.links.number_of_nodes()}")
        lines.append(f"- **关系边数**：{self.links.number_of_edges()}")
        lines.append(f"- **Action 次数**：{len(self.actions_log)}")
        return "\n".join(lines)

    def _extract_question_tokens(self, question: str) -> List[str]:
        """从问题中提取可用于匹配实体的关键词（数字 ID、英文词、中文词）。"""
        text = (question or "").strip().lower()
        if not text:
            return []

        tokens: List[str] = []
        # 连续数字 / 字母数字 ID（如 1001、emp_1001）
        tokens.extend(re.findall(r"[a-z0-9][a-z0-9_\-]{1,}", text))
        # 中文连续词（2 字以上）
        tokens.extend(re.findall(r"[\u4e00-\u9fff]{2,}", text))
        # 去重并过滤过短 token
        seen = set()
        result = []
        for t in tokens:
            t = t.strip()
            if len(t) < 2 or t in seen:
                continue
            # 过滤常见停用词（中英）
            if t in {
                "哪个", "哪些", "什么", "多少", "如何", "怎么", "是否",
                "属于", "相关", "关系", "查询", "显示", "列出", "所有",
                "the", "and", "or", "for", "with", "from", "that", "this",
                "what", "which", "where", "when", "how", "many", "list",
            }:
                continue
            seen.add(t)
            result.append(t)
        return result

    def retrieve_relevant_objects(
        self,
        question: str,
        max_per_type: int = 30,
        max_total: int = 120,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        按问题关键词检索相关实例，并补充其一跳邻居。
        无命中时回退为每种类型少量样本，保证问答仍可用。
        """
        tokens = self._extract_question_tokens(question)
        selected: Dict[str, Dict[str, Dict[str, Any]]] = {
            t: {} for t in self.object_types
        }
        matched_node_ids: List[str] = []

        def _record_matches(obj: Dict[str, Any], tokens: List[str]) -> bool:
            if not tokens:
                return False
            blob = " ".join(str(v).lower() for v in obj.values() if v is not None)
            return any(tok in blob for tok in tokens)

        # 1) 关键词命中
        if tokens:
            for type_name, objects in self.objects.items():
                for pk, obj in objects.items():
                    if _record_matches(obj, tokens):
                        selected[type_name][pk] = obj
                        matched_node_ids.append(make_node_id(type_name, pk))
                        if len(selected[type_name]) >= max_per_type:
                            break

        # 2) 一跳邻居扩展（关系路径问答更完整）
        if matched_node_ids and self.links.number_of_edges() > 0:
            neighbor_ids = set()
            for nid in matched_node_ids[:80]:
                if nid not in self.links:
                    continue
                neighbor_ids.update(self.links.predecessors(nid))
                neighbor_ids.update(self.links.successors(nid))
            for nid in neighbor_ids:
                attrs = self.links.nodes.get(nid) or {}
                t = attrs.get("type")
                pk = attrs.get("pk")
                if not t or pk is None or t not in selected:
                    continue
                if pk in selected[t]:
                    continue
                obj = self.objects.get(t, {}).get(str(pk))
                if obj is None:
                    continue
                if len(selected[t]) >= max_per_type:
                    continue
                selected[t][str(pk)] = obj

        # 3) 无命中则回退抽样
        total = sum(len(v) for v in selected.values())
        if total == 0:
            for type_name, objects in self.objects.items():
                for pk, obj in list(objects.items())[: min(10, max_per_type)]:
                    selected[type_name][pk] = obj

        # 4) 全局上限裁剪（保持类型尽量均衡）
        total = sum(len(v) for v in selected.values())
        if total > max_total:
            # 按类型轮询截断
            result: Dict[str, List[Dict[str, Any]]] = {t: [] for t in selected}
            keys = {t: list(objs.keys()) for t, objs in selected.items()}
            while sum(len(v) for v in result.values()) < max_total:
                progressed = False
                for t in list(keys.keys()):
                    if not keys[t]:
                        continue
                    pk = keys[t].pop(0)
                    result[t].append(selected[t][pk])
                    progressed = True
                    if sum(len(v) for v in result.values()) >= max_total:
                        break
                if not progressed:
                    break
            return result

        return {
            t: list(objs.values())
            for t, objs in selected.items()
            if objs
        }

    def to_context_for_llm(
        self,
        max_objects: Optional[int] = None,
        question: Optional[str] = None,
        mode: str = "retrieve",
        max_chars: int = 60000,
    ) -> str:
        """
        构建 LLM 上下文。

        mode:
          - retrieve: 按问题检索相关对象（推荐，默认）
          - sample:   每种类型最多 max_objects 条
          - full:     尽量全量，但仍受 max_chars 硬限制

        max_chars 为序列化后字符串的软上限，防止超出模型上下文。
        """
        context: Dict[str, Any] = {
            "object_types": {},
            "link_types": self.link_types,
            "sample_objects": {},
            "recent_actions": self.actions_log[-5:],
            "context_mode": mode,
            "note": "仅包含与问题相关或抽样的实例；完整数据见本体工作台。",
        }

        for type_name, info in self.object_types.items():
            count = len(self.objects.get(type_name, {}))
            context["object_types"][type_name] = {
                "primary_key": info["primary_key"],
                "properties": info["properties"],
                "description": info.get("description", ""),
                "count": count,
            }

        if mode == "retrieve" and question:
            context["sample_objects"] = self.retrieve_relevant_objects(
                question,
                max_per_type=max_objects or 30,
                max_total=(max_objects or 30) * max(1, len(self.object_types)),
            )
            context["context_mode"] = "retrieve"
        else:
            limit = max_objects
            if mode == "full":
                # 全量模式仍设安全上限，避免一次序列化百万行
                limit = None
            for type_name in self.object_types:
                records = list(self.objects.get(type_name, {}).values())
                if limit is not None:
                    records = records[:limit]
                context["sample_objects"][type_name] = records

        # 紧凑 JSON，并按字符预算逐步收缩实例
        def _dump(ctx: Dict[str, Any]) -> str:
            return json.dumps(ctx, ensure_ascii=False, separators=(",", ":"), default=str)

        payload = _dump(context)
        if len(payload) <= max_chars:
            return payload

        # 超限：按类型均匀裁剪 sample_objects
        samples = context.get("sample_objects") or {}
        if samples:
            # 每轮减半
            for _ in range(8):
                for t in list(samples.keys()):
                    arr = samples[t]
                    if not isinstance(arr, list) or len(arr) <= 1:
                        continue
                    samples[t] = arr[: max(1, len(arr) // 2)]
                context["sample_objects"] = samples
                context["note"] = (
                    "上下文已按大小自动裁剪；若答案不完整，请缩小问题范围或提高检索上限。"
                )
                payload = _dump(context)
                if len(payload) <= max_chars:
                    return payload

            # 仍超限：只保留 schema
            context["sample_objects"] = {}
            context["note"] = (
                "实例数据过大已全部省略，仅保留类型与关系定义。"
                "请改用「智能检索」或减少数据量后重试。"
            )
            payload = _dump(context)

        return payload

    def export_json(self) -> str:
        data = {
            "object_types": self.object_types,
            "objects": self.objects,
            "link_types": self.link_types,
            "links": [
                {"source": u, "target": v, "type": d.get("label")}
                for u, v, d in self.links.edges(data=True)
            ],
            "data_mapping": self.data_mapping,
            "actions_log": self.actions_log,
            "exported_at": datetime.now().isoformat(),
        }
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)

    def export_owl(self) -> str:
        graph = Graph()
        ex = Namespace("http://example.org/ontology#")
        graph.bind("ex", ex)
        graph.bind("owl", OWL)
        graph.bind("rdfs", RDFS)
        ontology_uri = URIRef("http://example.org/ontology")
        graph.add((ontology_uri, RDF.type, OWL.Ontology))

        for type_name, info in self.object_types.items():
            cls = ex[type_name]
            graph.add((cls, RDF.type, OWL.Class))
            graph.add((cls, RDFS.label, Literal(type_name)))
            if info.get("description"):
                graph.add((cls, RDFS.comment, Literal(info["description"])))

            for prop in info["properties"]:
                prop_uri = ex[f"{type_name}_{prop}"]
                graph.add((prop_uri, RDF.type, OWL.DatatypeProperty))
                graph.add((prop_uri, RDFS.domain, cls))
                graph.add((prop_uri, RDFS.label, Literal(prop)))

        # 导出全部实例，不再截断为固定 40 条
        for type_name, objects in self.objects.items():
            for pk, obj in objects.items():
                instance = ex[f"{type_name}_{pk}"]
                graph.add((instance, RDF.type, ex[type_name]))
                graph.add((instance, RDFS.label, Literal(f"{type_name}:{pk}")))
                for prop, value in obj.items():
                    if value is not None:
                        graph.add((
                            instance,
                            ex[f"{type_name}_{prop}"],
                            Literal(str(value)),
                        ))

        for relation_name, info in self.link_types.items():
            prop = ex[relation_name]
            graph.add((prop, RDF.type, OWL.ObjectProperty))
            graph.add((prop, RDFS.domain, ex[info["source"]]))
            graph.add((prop, RDFS.range, ex[info["target"]]))
            graph.add((prop, RDFS.label, Literal(relation_name)))
            if info.get("description"):
                graph.add((prop, RDFS.comment, Literal(info["description"])))

        for source, target, data in self.links.edges(data=True):
            relation_name = data.get("relation_type") or data.get("label")
            if relation_name:
                graph.add((
                    ex[source.replace(":", "_")],
                    ex[relation_name],
                    ex[target.replace(":", "_")],
                ))

        return graph.serialize(format="xml")


# ---------------------------------------------------------------------
# 自动建模（来自 ontology_studio_v2.py :: AutoOntologyBuilder）
# ---------------------------------------------------------------------
class AutoOntologyBuilder:
    def __init__(self) -> None:
        self.ontology = Ontology()

    @staticmethod
    def infer_primary_key(df: pd.DataFrame, table_name: str) -> str:
        if len(df.columns) == 0:
            raise ValueError(f"表 {table_name} 没有可用列")

        for col in df.columns:
            col_lower = str(col).lower()
            if (
                col_lower in ("id", "pk")
                or col_lower.endswith("_id")
                or col_lower == f"{table_name.lower()}_id"
            ):
                if df[col].is_unique and df[col].notna().all():
                    return str(col)

        for col in df.columns:
            if df[col].is_unique and df[col].notna().all():
                return str(col)

        return str(df.columns[0])

    def from_dataframes(
        self,
        data: Dict[str, pd.DataFrame],
        primary_keys: Optional[Dict[str, str]] = None,
        manual_links: Optional[List[Dict[str, Any]]] = None,
    ) -> Ontology:
        primary_keys = primary_keys or {}
        manual_links = manual_links or []

        normalized_data: Dict[str, pd.DataFrame] = {}
        actual_pks: Dict[str, str] = {}

        for table_name, original_df in data.items():
            df = original_df.copy()
            df.columns = [str(col).strip() for col in df.columns]
            df = df.loc[:, [c for c in df.columns if c != ""]]

            if df.empty and len(df.columns) == 0:
                continue

            pk = primary_keys.get(table_name) or self.infer_primary_key(df, table_name)
            if pk not in df.columns:
                pk = f"{table_name}_id"
                df[pk] = [str(i) for i in range(1, len(df) + 1)]

            # 如果指定主键有空值或重复，仍允许导入非空记录，但给出质量提示
            actual_pks[table_name] = pk
            normalized_data[table_name] = df

            self.ontology.add_object_type(
                table_name,
                pk,
                list(df.columns),
                f"来自数据源：{table_name}",
            )
            self.ontology.add_objects(table_name, safe_records(df))
            self.ontology.data_mapping[table_name] = {
                "source_name": table_name,
                "source_table": table_name,
                "primary_key": pk,
                "columns": list(df.columns),
                "column_types": {str(col): str(dtype) for col, dtype in df.dtypes.items()},
                "row_count": len(df),
                "loaded_count": len(self.ontology.objects[table_name]),
                "source_kind": "uploaded_table",
            }

        self._auto_detect_links(normalized_data, actual_pks)

        for link in manual_links:
            self._add_manual_link(normalized_data, link)

        return self.ontology

    def _auto_detect_links(
        self,
        data: Dict[str, pd.DataFrame],
        primary_keys: Dict[str, str],
    ) -> None:
        for source_table, source_df in data.items():
            source_pk = primary_keys[source_table]

            for target_table, target_df in data.items():
                if source_table == target_table:
                    continue

                target_pk = primary_keys[target_table]
                target_columns = {str(c).lower(): str(c) for c in target_df.columns}

                candidate_columns = []
                for col in source_df.columns:
                    col_name = str(col)
                    lower = col_name.lower()
                    if col_name == source_pk:
                        continue
                    if (
                        lower == target_pk.lower()
                        or lower == f"{target_table.lower()}_id"
                        or lower.endswith(f"_{target_table.lower()}_id")
                    ):
                        candidate_columns.append(col_name)

                for source_col in candidate_columns:
                    relation_name = f"{source_table}_to_{target_table}"
                    if relation_name not in self.ontology.link_types:
                        self.ontology.add_link_type(
                            relation_name,
                            source_table,
                            target_table,
                            f"自动识别：{source_table}.{source_col} → "
                            f"{target_table}.{target_pk}",
                            source_property=source_col,
                            target_property=target_pk,
                        )

                    pairs = [
                        (str(row[source_pk]), str(row[source_col]))
                        for _, row in source_df.iterrows()
                        if pd.notna(row[source_pk]) and pd.notna(row[source_col])
                    ]
                    self.ontology.add_links(relation_name, pairs)

    def _add_manual_link(
        self,
        data: Dict[str, pd.DataFrame],
        link: Dict[str, Any],
    ) -> None:
        source = link["source"]
        target = link["target"]
        source_col = link["source_col"]
        relation_name = link["name"]

        if source not in data or target not in data:
            raise ValueError(f"关系 {relation_name} 的源表或目标表不存在")
        if source_col not in data[source].columns:
            raise ValueError(f"源关联列 {source}.{source_col} 不存在")

        source_pk = self.ontology.object_types[source]["primary_key"]
        target_pk = self.ontology.object_types[target]["primary_key"]

        self.ontology.add_link_type(
            relation_name,
            source,
            target,
            link.get("description", "手动指定"),
            source_property=source_col,
            target_property=target_pk,
        )
        pairs = [
            (str(row[source_pk]), str(row[source_col]))
            for _, row in data[source].iterrows()
            if pd.notna(row.get(source_pk)) and pd.notna(row.get(source_col))
        ]
        self.ontology.add_links(relation_name, pairs)


# ---------------------------------------------------------------------
# 关系分析与质量检查（来自 ontology_studio_v2.py）
# ---------------------------------------------------------------------
def find_shortest_path(
    ontology: Ontology, source: str, target: str
) -> Optional[List[str]]:
    graph = nx.DiGraph(ontology.links)
    try:
        return nx.shortest_path(graph, source=source, target=target)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        # 业务探索时也尝试无向路径
        try:
            return nx.shortest_path(graph.to_undirected(), source, target)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None


def quality_report(ontology: Ontology) -> Dict[str, Any]:
    graph = ontology.links
    isolated = list(nx.isolates(graph))
    duplicate_edges = []
    edge_seen = set()

    for source, target, key in graph.edges(keys=True):
        signature = (source, target, key)
        if signature in edge_seen:
            duplicate_edges.append(signature)
        edge_seen.add(signature)

    dangling: List[Dict[str, str]] = []
    for relation_name, info in ontology.link_types.items():
        source_type = info["source"]
        target_type = info["target"]
        source_col = info.get("source_property")
        if not source_col or source_type not in ontology.objects:
            continue

        target_keys = set(ontology.objects.get(target_type, {}).keys())
        for source_pk, obj in ontology.objects[source_type].items():
            value = obj.get(source_col)
            if value is not None and str(value) not in target_keys:
                dangling.append({
                    "relation": relation_name,
                    "source": f"{source_type}:{source_pk}",
                    "target_value": str(value),
                })

    invalid_relation_types = [
        name for name, info in ontology.link_types.items()
        if info["source"] not in ontology.object_types
        or info["target"] not in ontology.object_types
    ]

    return {
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "isolated_nodes": isolated,
        "dangling_references": dangling,
        "invalid_relation_types": invalid_relation_types,
        "self_loops": list(nx.selfloop_edges(graph, keys=True)),
        "duplicate_edges": duplicate_edges,
    }


# ---------------------------------------------------------------------
# 图谱 JSON（替代 PyVis HTML，供前端渲染）
# ---------------------------------------------------------------------
def schema_graph_payload(ontology: Ontology) -> Dict[str, Any]:
    """Schema 图：Class / Property / Object Property。"""
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    for type_name, info in ontology.object_types.items():
        props = info.get("properties", [])
        source_meta = ontology.data_mapping.get(type_name) or {}
        nodes.append({
            "id": f"CLASS:{type_name}",
            "kind": "class",
            "label": info.get("label") or type_name,
            "primary_key": info["primary_key"],
            "properties": props,
            "description": info.get("description", ""),
            "source": ({
                "source_table": type_name,
                "source_kind": source_meta.get("source_kind", "unknown"),
                "dataset_id": source_meta.get("dataset_id"),
                "row_count": source_meta.get("row_count", 0),
                "columns": source_meta.get("columns", []),
            } if source_meta else None),
        })
        for prop in props:
            prop_id = f"PROP:{type_name}:{prop}"
            nodes.append({
                "id": prop_id,
                "kind": "property",
                "label": prop,
                "owner": type_name,
                "source": {
                    **(info.get("property_mappings", {}).get(prop) or {
                        "source_table": type_name, "source_column": prop,
                    }),
                    "dataset_id": (ontology.data_mapping.get(type_name) or {}).get("dataset_id"),
                    "mapped": (
                        info.get("property_mappings", {}).get(prop, {}).get("source_column", prop)
                        in (ontology.data_mapping.get(type_name) or {}).get("columns", [])
                        and bool(info.get("property_mappings", {}).get(prop, {}).get("source_column", prop))
                    ),
                },
                "definition": info.get("property_definitions", {}).get(prop, {}),
            })
            edges.append({
                "source": f"CLASS:{type_name}",
                "target": prop_id,
                "label": "hasProperty",
                "kind": "hasProperty",
            })

    for relation_name, info in ontology.link_types.items():
        source, target = info["source"], info["target"]
        if source not in ontology.object_types or target not in ontology.object_types:
            continue
        source_property = info.get("source_property", "")
        target_property = info.get("target_property", "")
        source_map = ontology.object_types[source].get("property_mappings", {}).get(source_property)
        target_map = ontology.object_types[target].get("property_mappings", {}).get(target_property)
        source_column = source_map.get("source_column") if source_map is not None else source_property
        target_column = target_map.get("source_column") if target_map is not None else target_property
        source_columns = (ontology.data_mapping.get(source) or {}).get("columns", [])
        target_columns = (ontology.data_mapping.get(target) or {}).get("columns", [])
        edges.append({
            "source": f"CLASS:{source}",
            "target": f"CLASS:{target}",
            "label": relation_name,
            "kind": "objectProperty",
            "description": info.get("description", ""),
            "source_binding": {
                "source_table": source,
                "source_column": source_column or "",
                "source_property": source_property,
                "target_table": target,
                "target_column": target_column or "",
                "target_property": target_property,
                "source_dataset_id": (ontology.data_mapping.get(source) or {}).get("dataset_id"),
                "target_dataset_id": (ontology.data_mapping.get(target) or {}).get("dataset_id"),
                "mapped": bool(source_column and target_column
                               and source_column in source_columns and target_column in target_columns),
            },
        })

    return {"nodes": nodes, "edges": edges}


def instance_graph_payload(
    ontology: Ontology,
    selected_types: Optional[List[str]] = None,
    max_nodes: int = 500,
    search_text: str = "",
    focus_node: Optional[str] = None,
    hops: int = 1,
) -> Dict[str, Any]:
    """实例子图 JSON；逻辑对齐 create_instance_graph。"""
    graph = ontology.links
    allowed_types = set(selected_types or list(ontology.object_types.keys()))
    candidates: List[str] = []

    search_text = (search_text or "").strip().lower()
    for node_id, attrs in graph.nodes(data=True):
        node_type = attrs.get("type")
        if node_type not in allowed_types:
            continue
        if search_text:
            searchable = " ".join(
                [node_id] + [str(v) for v in attrs.values()]
            ).lower()
            if search_text not in searchable:
                continue
        candidates.append(node_id)

    truncated = len(candidates) > max_nodes
    chosen = set(candidates[:max_nodes])

    if focus_node and focus_node in graph:
        neighborhood = {focus_node}
        frontier = {focus_node}
        for _ in range(max(1, hops)):
            next_frontier = set()
            for node in frontier:
                next_frontier.update(graph.predecessors(node))
                next_frontier.update(graph.successors(node))
            next_frontier -= neighborhood
            neighborhood.update(next_frontier)
            frontier = next_frontier

        neighborhood = {
            n for n in neighborhood
            if graph.nodes[n].get("type") in allowed_types
        }
        chosen = neighborhood
        if len(chosen) > max_nodes:
            chosen = set(list(chosen)[:max_nodes])
            truncated = True

    nodes = []
    for node_id in chosen:
        attrs = graph.nodes[node_id]
        node_type = attrs.get("type", "Unknown")
        pk = attrs.get("pk", "")
        obj = ontology.objects.get(node_type, {}).get(str(pk), {})
        nodes.append({
            "id": node_id,
            "type": node_type,
            "pk": pk,
            "label": f"{node_type}:{pk}",
            "data": obj,
        })

    edges = []
    for source, target, key, attrs in graph.edges(keys=True, data=True):
        if source not in chosen or target not in chosen:
            continue
        relation = attrs.get("relation_type", key)
        edges.append({
            "source": source,
            "target": target,
            "label": str(relation),
            "relation_type": str(relation),
            "description": attrs.get("description", ""),
        })

    return {
        "nodes": nodes,
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "truncated": truncated,
        "total_nodes": graph.number_of_nodes(),
    }


# ---------------------------------------------------------------------
# LLM 工具与问答（来自 ontology_studio_v2.py）
# ---------------------------------------------------------------------
ONTOLOGY_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_objects",
            "description": (
                "按关键词搜索实体实例。可限定类型。"
                "用于查找员工、部门、订单等业务对象。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词，如主键、姓名、编码",
                    },
                    "type_name": {
                        "type": "string",
                        "description": "可选，限定实体类型名称",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回条数，默认 20，最大 50",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_object",
            "description": "按类型与主键精确获取单个实体的全部属性。",
            "parameters": {
                "type": "object",
                "properties": {
                    "type_name": {"type": "string", "description": "实体类型名称"},
                    "primary_key": {"type": "string", "description": "主键值"},
                },
                "required": ["type_name", "primary_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_neighbors",
            "description": (
                "查询实体的直接关联邻居（入边与出边）。"
                "node_id 格式为 Type:pk，例如 Employee:1001。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "节点 ID，格式 Type:pk",
                    },
                    "type_name": {"type": "string"},
                    "primary_key": {"type": "string"},
                    "limit": {"type": "integer", "description": "最多返回邻居数，默认 30"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_path",
            "description": "查询两个实体之间的最短关系路径。节点 ID 格式 Type:pk。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_node": {"type": "string"},
                    "target_node": {"type": "string"},
                },
                "required": ["source_node", "target_node"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_objects",
            "description": "统计实体数量。可按类型统计，或按属性值过滤后计数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "type_name": {"type": "string"},
                    "property_name": {"type": "string"},
                    "property_value": {"type": "string"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_link_types",
            "description": "列出本体中已定义的关系类型及其源/目标实体类型。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def _make_llm_client(api_key: str, base_url: str):
    """统一创建 LLM 客户端：限制单次超时与重试次数，避免界面长时间无响应。"""
    endpoint = base_url.strip().rstrip("/") if base_url else None
    if endpoint and endpoint.endswith("/chat/completions"):
        endpoint = endpoint[:-len("/chat/completions")]
    return OpenAI(
        api_key=api_key,
        base_url=endpoint,
        timeout=90.0,
        max_retries=1,
    )


def ontology_schema_for_llm(ontology: Ontology) -> str:
    """仅序列化 Schema + 计数，不含实例，供 Tool Calling 系统提示使用。"""
    payload = {
        "object_types": {
            name: {
                "primary_key": info["primary_key"],
                "properties": info["properties"],
                "description": info.get("description", ""),
                "count": len(ontology.objects.get(name, {})),
            }
            for name, info in ontology.object_types.items()
        },
        "link_types": ontology.link_types,
        "graph_stats": {
            "nodes": ontology.links.number_of_nodes(),
            "edges": ontology.links.number_of_edges(),
        },
        "node_id_format": "Type:primary_key，例如 Employee:1001",
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def execute_ontology_tool(
    ontology: Ontology,
    name: str,
    arguments: Dict[str, Any],
) -> Any:
    """执行本体查询工具，返回可 JSON 序列化的结果。"""
    try:
        if name == "search_objects":
            keyword = str(arguments.get("keyword") or "").strip()
            type_name = arguments.get("type_name")
            limit = int(arguments.get("limit") or 20)
            limit = max(1, min(limit, 50))
            if not keyword:
                return {"error": "keyword 不能为空", "results": []}

            types = (
                [str(type_name)]
                if type_name and str(type_name) in ontology.object_types
                else list(ontology.object_types.keys())
            )
            kw = keyword.lower()
            results = []
            for t in types:
                for pk, obj in ontology.objects.get(t, {}).items():
                    blob = " ".join(
                        str(v).lower() for v in obj.values() if v is not None
                    )
                    # 主键精确优先
                    if str(pk).lower() == kw or kw in blob:
                        results.append({
                            "type": t,
                            "primary_key": pk,
                            "node_id": make_node_id(t, pk),
                            "data": obj,
                        })
                        if len(results) >= limit:
                            return {"count": len(results), "results": results}
            return {"count": len(results), "results": results}

        if name == "get_object":
            type_name = str(arguments.get("type_name") or "")
            pk = str(arguments.get("primary_key") or "")
            if type_name not in ontology.object_types:
                return {
                    "error": f"未知类型 {type_name}",
                    "available_types": list(ontology.object_types.keys()),
                }
            obj = ontology.objects.get(type_name, {}).get(pk)
            if obj is None:
                # 尝试大小写不敏感匹配主键
                found = None
                for k, v in ontology.objects.get(type_name, {}).items():
                    if k.lower() == pk.lower():
                        found = (k, v)
                        break
                if found is None:
                    return {"error": f"未找到 {type_name}:{pk}"}
                pk, obj = found
            return {
                "type": type_name,
                "primary_key": pk,
                "node_id": make_node_id(type_name, pk),
                "data": obj,
            }

        if name == "get_neighbors":
            node_id = arguments.get("node_id")
            if not node_id:
                t = arguments.get("type_name")
                pk = arguments.get("primary_key")
                if t and pk is not None:
                    node_id = make_node_id(str(t), pk)
            node_id = str(node_id or "")
            limit = int(arguments.get("limit") or 30)
            limit = max(1, min(limit, 100))
            if node_id not in ontology.links:
                return {"error": f"节点不存在: {node_id}"}

            neighbors = []
            for pred in ontology.links.predecessors(node_id):
                edge_data = ontology.links.get_edge_data(pred, node_id) or {}
                relations = [
                    str(d.get("relation_type") or k)
                    for k, d in edge_data.items()
                ]
                neighbors.append({
                    "neighbor": pred,
                    "type": ontology.links.nodes[pred].get("type"),
                    "direction": "incoming",
                    "relations": relations,
                })
            for succ in ontology.links.successors(node_id):
                edge_data = ontology.links.get_edge_data(node_id, succ) or {}
                relations = [
                    str(d.get("relation_type") or k)
                    for k, d in edge_data.items()
                ]
                neighbors.append({
                    "neighbor": succ,
                    "type": ontology.links.nodes[succ].get("type"),
                    "direction": "outgoing",
                    "relations": relations,
                })
            return {
                "node_id": node_id,
                "neighbor_count": len(neighbors),
                "neighbors": neighbors[:limit],
            }

        if name == "find_path":
            source = str(arguments.get("source_node") or "")
            target = str(arguments.get("target_node") or "")
            path = find_shortest_path(ontology, source, target)
            if not path:
                return {
                    "found": False,
                    "source": source,
                    "target": target,
                    "message": "未找到路径",
                }
            steps = []
            for left, right in zip(path[:-1], path[1:]):
                edge_data = ontology.links.get_edge_data(left, right) or {}
                if not edge_data:
                    # 无向回退时可能边方向相反
                    edge_data = ontology.links.get_edge_data(right, left) or {}
                relations = [
                    str(d.get("relation_type") or k)
                    for k, d in edge_data.items()
                ]
                steps.append({
                    "from": left,
                    "to": right,
                    "relations": relations,
                })
            return {
                "found": True,
                "length": len(path) - 1,
                "path": path,
                "steps": steps,
            }

        if name == "count_objects":
            type_name = arguments.get("type_name")
            prop = arguments.get("property_name")
            prop_val = arguments.get("property_value")
            if type_name:
                type_name = str(type_name)
                if type_name not in ontology.object_types:
                    return {
                        "error": f"未知类型 {type_name}",
                        "available_types": list(ontology.object_types.keys()),
                    }
                objects = ontology.objects.get(type_name, {})
                if prop is not None and prop_val is not None:
                    pv = str(prop_val).lower()
                    matched = [
                        o for o in objects.values()
                        if str(o.get(str(prop), "")).lower() == pv
                    ]
                    return {
                        "type": type_name,
                        "filter": {str(prop): prop_val},
                        "count": len(matched),
                    }
                return {"type": type_name, "count": len(objects)}
            return {
                "counts": {
                    t: len(ontology.objects.get(t, {}))
                    for t in ontology.object_types
                },
                "total": ontology.links.number_of_nodes(),
            }

        if name == "list_link_types":
            return {
                "link_types": [
                    {"name": n, **info}
                    for n, info in ontology.link_types.items()
                ]
            }

        return {"error": f"未知工具: {name}"}
    except Exception as exc:
        return {"error": f"工具执行失败: {exc}"}


def _tool_result_json(data: Any, max_chars: int = 12000) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > max_chars:
        return text[: max_chars - 80] + '..."truncated":true}'
    return text


def _friendly_llm_error(exc: Exception) -> str:
    """将常见 API 错误转成可读提示。"""
    msg = str(exc)
    lower = msg.lower()
    if any(
        k in lower
        for k in (
            "context_length", "maximum context", "too many tokens",
            "token limit", "context window", "payload too large",
            "request too large",
        )
    ):
        return (
            "调用失败：上下文过长。\n"
            "建议使用「工具调用（推荐）」模式，仅按需查询实体，不要传全量对象。"
            f"\n原始错误：{msg}"
        )
    if "timeout" in lower or "timed out" in lower:
        return (
            "调用失败：请求超时。\n"
            "请检查 Base URL / 网络，或改用「工具调用」减少单次上下文。"
            f"\n原始错误：{msg}"
        )
    if any(
        k in lower
        for k in ("tool", "function calling", "tools are not supported",
                  "does not support", "unknown parameter")
    ):
        return (
            "调用失败：当前模型或接口可能不支持 Tool Calling。\n"
            "请在侧边栏切换为「智能检索」降级模式，或更换支持 function calling 的模型。"
            f"\n原始错误：{msg}"
        )
    return f"调用失败：{msg}"


def _system_prompt_for_tools(ontology: Ontology) -> str:
    schema = ontology_schema_for_llm(ontology)
    return (
        "你是企业本体问答助手。你只能依据工具返回的真实数据回答，禁止编造实体或关系。\n"
        "可用工具：search_objects、get_object、get_neighbors、find_path、"
        "count_objects、list_link_types。\n"
        "节点 ID 格式为 Type:primary_key（例如 Employee:1001）。\n"
        "回答流程建议：先 search_objects 或 get_object 定位实体，"
        "再 get_neighbors / find_path 查关系；统计类问题用 count_objects。\n"
        "若工具结果不足，请说明还缺什么，不要猜测。\n"
        "最终用简洁中文回答用户。\n\n"
        f"当前本体 Schema（无实例数据）：\n{schema}"
    )


def _build_llm_messages_legacy(
    question: str,
    ontology: Ontology,
    max_objects: Optional[int],
    context_mode: str = "retrieve",
) -> List[Dict[str, str]]:
    """降级模式：把检索/抽样实例直接塞进 prompt。"""
    context = ontology.to_context_for_llm(
        max_objects=max_objects,
        question=question,
        mode=context_mode,
        max_chars=60000,
    )
    system_prompt = (
        "你是企业本体问答助手。请严格依据给出的本体结构和业务数据回答，"
        "不要编造不存在的实体、属性或关系。若数据不足，请明确说明。"
        "\n\n当前本体数据（检索/抽样，可能非全量）：\n" + context
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]


def ask_llm_with_tools(
    question: str,
    ontology: Ontology,
    api_key: str,
    base_url: str,
    model: str,
    max_rounds: int = 6,
):
    """
    Tool Calling 代理：多轮调用本体查询工具后给出最终回答。
    yield ("status", 文本) 或 ("token", 文本) 或 ("error", 文本)。
    """
    if not api_key:
        yield ("error", "请先配置 API Key。")
        return
    if OpenAI is None:
        yield ("error", "缺少 openai 依赖，请执行：pip install openai")
        return

    try:
        client = _make_llm_client(api_key, base_url)
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _system_prompt_for_tools(ontology)},
            {"role": "user", "content": question},
        ]

        for round_idx in range(max_rounds):
            yield ("status", f"思考中（第 {round_idx + 1}/{max_rounds} 轮）…")
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=ONTOLOGY_TOOLS,
                tool_choice="auto",
                temperature=0.1,
                max_tokens=1500,
            )
            choice = response.choices[0]
            message = choice.message
            tool_calls = getattr(message, "tool_calls", None) or []

            # 无工具调用 → 最终回答
            if not tool_calls:
                content = (message.content or "").strip()
                if content:
                    yield ("token", content)
                else:
                    yield ("token", "模型没有返回文本。")
                return

            # 记录 assistant 工具调用消息
            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            }
            messages.append(assistant_msg)

            for tc in tool_calls:
                fn_name = tc.function.name
                raw_args = tc.function.arguments or "{}"
                try:
                    args = json.loads(raw_args) if raw_args else {}
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                    result = {"error": f"工具参数不是合法 JSON: {raw_args[:200]}"}
                else:
                    yield (
                        "status",
                        f"调用工具 `{fn_name}`"
                        + (f"（{json.dumps(args, ensure_ascii=False)[:80]}）" if args else ""),
                    )
                    result = execute_ontology_tool(ontology, fn_name, args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _tool_result_json(result),
                })

        # 超出轮次：强制再要一次纯文本答案
        yield ("status", "工具轮次已用尽，正在汇总答案…")
        messages.append({
            "role": "user",
            "content": "请根据已获取的工具结果给出最终中文回答，不要再调用工具。",
        })
        final = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.1,
            max_tokens=1500,
        )
        content = (final.choices[0].message.content or "").strip()
        yield ("token", content or "未能在限定轮次内完成回答，请缩小问题范围后重试。")
    except Exception as exc:
        yield ("error", _friendly_llm_error(exc))


def ask_llm(
    question: str,
    ontology: Ontology,
    api_key: str,
    base_url: str,
    model: str,
    max_objects: Optional[int] = None,
    context_mode: str = "retrieve",
) -> str:
    if not api_key:
        return "请先配置 API Key。"
    if OpenAI is None:
        return "缺少 openai 依赖，请执行：pip install openai"

    try:
        client = _make_llm_client(api_key, base_url)
        response = client.chat.completions.create(
            model=model,
            messages=_build_llm_messages_legacy(
                question, ontology, max_objects, context_mode=context_mode
            ),
            temperature=0.2,
            max_tokens=1500,
        )
        return response.choices[0].message.content or "模型没有返回文本。"
    except Exception as exc:
        return _friendly_llm_error(exc)
