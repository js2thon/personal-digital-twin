"""数据接入：文件 / 数据库 → 统一的 TableSet。

统一抽象：每张表 = pandas.DataFrame（采样）+ 声明的主键/外键/注释。
数据库侧只读、限行、超时；连接串不落日志。
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

SAMPLE_ROWS = 10_000
STATEMENT_TIMEOUT_MS = 15_000


@dataclass
class ForeignKey:
    column: str
    ref_table: str
    ref_column: str
    declared: bool = True          # False = 由取值包含率推断


@dataclass
class Table:
    name: str
    df: pd.DataFrame
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    comment: str = ""
    column_comments: dict[str, str] = field(default_factory=dict)
    row_count: int | None = None


@dataclass
class TableSet:
    source_id: str
    kind: str                      # file / database
    tables: dict[str, Table] = field(default_factory=dict)


_SAFE_NAME = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff]+")


def _table_name(raw: str) -> str:
    return _SAFE_NAME.sub("_", raw).strip("_").lower() or "table"


# ------------------------------------------------------------------ 文件
def from_files(paths: list[str | Path], source_id: str = "upload") -> TableSet:
    """CSV / TSV / Excel(每个 sheet 一张表) / JSON(记录数组) / Parquet。"""
    ts = TableSet(source_id=source_id, kind="file")
    for p in map(Path, paths):
        suf = p.suffix.lower()
        if suf in (".csv", ".tsv", ".txt"):
            frames = {p.stem: _read_csv(p, "\t" if suf == ".tsv" else None)}
        elif suf in (".xlsx", ".xls"):
            frames = pd.read_excel(p, sheet_name=None, nrows=SAMPLE_ROWS)
            frames = {f"{p.stem}_{k}" if len(frames) > 1 else p.stem: v for k, v in frames.items()}
        elif suf == ".json":
            frames = {p.stem: pd.read_json(p).head(SAMPLE_ROWS)}
        elif suf == ".parquet":
            frames = {p.stem: pd.read_parquet(p).head(SAMPLE_ROWS)}
        else:
            raise ValueError(f"不支持的文件类型：{p.name}")
        for name, df in frames.items():
            t = _table_name(name)
            df.columns = [_table_name(str(c)) for c in df.columns]
            ts.tables[t] = Table(name=t, df=df, row_count=len(df))
    return ts


def _read_csv(p: Path, sep: str | None) -> pd.DataFrame:
    for enc in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return pd.read_csv(p, sep=sep, engine="python", encoding=enc, nrows=SAMPLE_ROWS)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别文件编码：{p.name}")


# ------------------------------------------------------------------ 数据库
ALLOWED_DIALECTS = ("postgresql", "mysql", "mssql", "oracle", "sqlite")


def check_url(url: str, allow_private: bool = True, allow_hosts: set[str] | None = None) -> None:
    """拒绝非白名单方言；可选拒绝内网地址（SSRF）。"""
    u = urlparse(url)
    dialect = u.scheme.split("+")[0]
    if dialect not in ALLOWED_DIALECTS:
        raise ValueError(f"不支持的数据库类型：{dialect}")
    if dialect == "sqlite" or allow_private:
        return
    host = u.hostname or ""
    if allow_hosts and host in allow_hosts:
        return
    for info in socket.getaddrinfo(host, None):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError("目标地址位于内网，未在白名单内")


def from_database(url: str, schema: str | None = None, include: list[str] | None = None,
                  source_id: str = "db", sample_rows: int = SAMPLE_ROWS,
                  allow_private: bool = True) -> TableSet:
    """反射表/列/主外键/注释并采样。务必使用只读账号。"""
    from sqlalchemy import MetaData, create_engine, inspect, select, text

    check_url(url, allow_private=allow_private)
    engine = create_engine(url, pool_pre_ping=True)
    ts = TableSet(source_id=source_id, kind="database")
    insp = inspect(engine)
    names = include or insp.get_table_names(schema=schema)
    meta = MetaData()
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text(f"SET statement_timeout = {int(STATEMENT_TIMEOUT_MS)}"))
            conn.execute(text("SET TRANSACTION READ ONLY"))
        for raw in names:
            meta.reflect(bind=engine, schema=schema, only=[raw])
            tbl = meta.tables[f"{schema}.{raw}" if schema else raw]
            df = pd.read_sql(select(tbl).limit(int(sample_rows)), conn)
            pk = insp.get_pk_constraint(raw, schema=schema).get("constrained_columns") or []
            fks = []
            for fk in insp.get_foreign_keys(raw, schema=schema):
                for c, rc in zip(fk["constrained_columns"], fk["referred_columns"]):
                    fks.append(ForeignKey(c.lower(), _table_name(fk["referred_table"]), rc.lower()))
            try:
                comment = (insp.get_table_comment(raw, schema=schema) or {}).get("text") or ""
            except NotImplementedError:
                comment = ""
            col_comments = {c["name"].lower(): c.get("comment") or ""
                            for c in insp.get_columns(raw, schema=schema)}
            t = _table_name(raw)
            df.columns = [str(c).lower() for c in df.columns]
            ts.tables[t] = Table(name=t, df=df, primary_key=[c.lower() for c in pk],
                                 foreign_keys=fks, comment=comment,
                                 column_comments=col_comments)
    engine.dispose()
    return ts
