"""全局配置：所有敏感凭据通过环境变量注入，不落入 connector_config。"""
from pathlib import Path

from pydantic import ConfigDict
from pydantic_settings import BaseSettings

# env_file 必须锚定仓库根：Settings 在 backend/ 下被导入时，相对 ".env" 会
# 找不到根配置而回落到 bge-m3@localhost:8001，与已落库向量不同源（实测：
# 同一 query 在进程内外得到完全不同的向量空间）。这里用绝对路径彻底消除 CWD 漂移。
_ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = ConfigDict(env_file=str(_ROOT_ENV_FILE), extra="ignore")
    # 数据域
    # 本地内嵌 PostgreSQL 使用 5434（5433 会被 WSL 端口转发占用）
    database_url: str = "postgresql+psycopg://twin@127.0.0.1:5434/personal_twin"
    redis_url: str = "redis://localhost:6379/0"

    # MinIO：L14 不在代码里放默认口令。未显式配置 MINIO_ACCESS_KEY /
    # MINIO_SECRET_KEY 时，storage.get_minio() 直接降级为原文内联 DB 存储。
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "twin-raw"

    # LLM / Embedding（OpenAI 兼容端点；本机 .env 实际指向企业 OneAPI 网关）
    llm_base_url: str = "http://localhost:8001/v1"
    llm_api_key: str = "empty"
    llm_model: str = "qwen"
    embedding_base_url: str = "http://localhost:8001/v1"
    embedding_model: str = "bge-m3"
    embedding_dim: int = 1024
    # Rerank（OneAPI 兼容 /v1/rerank；模型名为空表示禁用，网关异常自动降级）
    rerank_model: str = ""
    rerank_candidates: int = 20
    rerank_min_score: float = 0.05
    # 向量后端：auto=探测扩展后选择 / text=P0 零依赖 TEXT-JSON 余弦 / pgvector=P1
    # 当前 P0 阶段 pgvector 尚未实现，即使显式配置也会静默降级到 text 并记 fallback
    vector_backend: str = "auto"

    # RAGFlow 1.0（经官方 HTTP API，不改内部数据库）
    ragflow_base_url: str = ""
    ragflow_api_key: str = ""

    # 治理
    pipeline_version: str = "v1"
    prompt_version: str = "v1"
    # 运行环境（V1.3 Sprint 1 验收铁律）：dev=本地/单测，允许 CQ 降级为
    # memory；production=CQ V2 强制 PostgreSQL，任何 MemoryStore 降级都在
    # 启动时 fail-fast。由 APP_ENV 覆盖，缺省 dev 保证既有 486 测试零影响。
    app_env: str = "dev"
    # CQ V2 状态存储：postgres=落库并可跨重启水合；memory=仅进程内（单测用）。
    # 库不可达时自动降级为 memory 并在持久化端点里显示 degraded_reason。
    # production 下该降级被禁止，见 store.assert_production_store()。
    cq_v2_store: str = "postgres"
    # SHACL 写入侧校验模式：warn=记录不拦截（默认）/ strict=拦截非法写入 / off=关闭
    shacl_mode: str = "warn"
    d3_auto_exec_enabled: bool = False  # MVP 固定禁止自动执行
    # L15：低于该置信度的 Claim 标记为「优先复核」，进入人工审核队列前排
    claim_review_threshold: float = 0.7

    # 访问控制：仅监听 127.0.0.1 时回环请求视为本人；端口外露后，
    # 非回环请求必须带 X-Twin-API-Key（api_key 为空则一律拒绝非回环访问）。
    api_key: str = ""
    allowed_origins: str = "http://127.0.0.1:8101,http://localhost:8101"

    # 反向代理信任边界（M7）：默认关闭 —— 此时 X-Forwarded-For 一律不采信，
    # 以 TCP 对端为准。只有显式开启且对端属于 trusted_proxy_hosts 时才解析 XFF，
    # 并从右向左取第一个「非可信代理」地址，避免客户端伪造最左侧地址绕过闸门。
    trust_proxy_headers: bool = False
    trusted_proxy_hosts: str = "127.0.0.1,::1"
    # 仅测试侧显式开启：允许 TestClient 的虚拟主机名 "testclient" 视为本人。
    allow_testclient_actor: bool = False

    # DWS 在线表格读取上限（L7）：按工作表元数据动态计算区域，不再固定 A1:Z200
    dws_sheet_max_rows: int = 2000
    dws_sheet_max_cols: int = 50

    # 上传大小上限（L8）：默认 25 MiB，超出直接 413，避免大文件吃满内存
    max_upload_bytes: int = 25 * 1024 * 1024

    # 企业 IM消息接管：真实外发总开关（默认关闭 = 一律 dry-run 预览，不外发）
    # 开启后仍需本人在控制台对每条草稿逐条确认，不存在自动发送路径。
    reply_send_enabled: bool = False
    # 发送角标（钉钉固定文案「通过AI发送」）：true 显示，false 不带角标。
    # 平台不支持自定义角标文案，只能开/关。
    reply_ai_tag: bool = True


settings = Settings()
