-- 04: 结构化接入（映射契约 / 确定性断言 / 同步留痕 / 异常闭环）
-- 与文档蒸馏平行的一条旁路：不产生 documents / semantic_chunks / claims / evidence_spans。
-- 证据可追溯改由 knowledge.facts.source_ref 承担（库表主键 + 批次 + 行哈希 + 契约版本）。
-- 权威事实源仍是 PostgreSQL（ADR-001）；外部库/湖仓只是只读数据源。

-- ---------- A. 映射契约（本人审核并发布后才允许写入）----------
CREATE TABLE IF NOT EXISTS ingest.structured_contracts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(120) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(20) NOT NULL DEFAULT 'draft',   -- draft/published/deprecated
    source_ref JSONB NOT NULL DEFAULT '{}',         -- {system, table, data_source_id, ...} 不含凭据
    mapping JSONB NOT NULL DEFAULT '{}',            -- entity / fields / sensitivity / expect_columns
    contract_hash VARCHAR(128) NOT NULL,           -- sha256(规范化 mapping + source_ref)
    note TEXT DEFAULT '',
    created_by VARCHAR(50) NOT NULL,               -- 服务端注入，不接受请求体自报
    published_by VARCHAR(50),
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (name, version)
);
CREATE INDEX IF NOT EXISTS idx_structured_contracts_status
    ON ingest.structured_contracts (status, name);

-- ---------- B. 同步批次留痕（含 dry-run；不建外键，契约删除不丢审计链）----------
CREATE TABLE IF NOT EXISTS ingest.structured_sync_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contract_id UUID NOT NULL,
    contract_version INTEGER NOT NULL,
    contract_hash VARCHAR(128) NOT NULL,
    batch_id VARCHAR(128) NOT NULL,
    dry_run BOOLEAN NOT NULL DEFAULT TRUE,
    status VARCHAR(20) NOT NULL DEFAULT 'completed',  -- completed/rolled_back
    row_count INTEGER NOT NULL DEFAULT 0,
    counts JSONB NOT NULL DEFAULT '{}',               -- 各 write_fact action 分类汇总
    cursor_before JSONB DEFAULT '{}',
    cursor_after JSONB DEFAULT '{}',
    actor VARCHAR(50) NOT NULL,
    error_info JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_structured_sync_runs_contract
    ON ingest.structured_sync_runs (contract_id, created_at DESC);

-- ---------- C. 异常处置闭环（谁 / 在哪 / 怎么做 / 怎么验证）----------
CREATE TABLE IF NOT EXISTS ingest.structured_anomalies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sync_run_id UUID NOT NULL,
    contract_id UUID NOT NULL,
    reason VARCHAR(50) NOT NULL,
    pk VARCHAR(200) DEFAULT '',
    field VARCHAR(200) DEFAULT '',
    detail JSONB DEFAULT '{}',
    status VARCHAR(20) NOT NULL DEFAULT 'open',       -- open/resolved/dismissed
    actor VARCHAR(50),
    note TEXT DEFAULT '',
    verification VARCHAR(20) NOT NULL DEFAULT 'pending',
    -- pending -> recovered / still_failing；由下一次 preview/sync 自动回填
    verified_at TIMESTAMPTZ,
    verified_detail JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_structured_anomalies_open
    ON ingest.structured_anomalies (contract_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_structured_anomalies_verify
    ON ingest.structured_anomalies (contract_id, verification)
    WHERE verification = 'pending';

-- ---------- D. 上传快照（表格 / Markdown 表格 / JSON）----------
-- 文件只是「行的载体」：解析后与湖仓取数同构，仍走同一套契约与断言，
-- 不产生 Claim，也不做列名语义猜测。快照留存是为了按 upload_id 复用（含重试），
-- 因此提供显式删除入口，避免敏感列无期限留在库里。
CREATE TABLE IF NOT EXISTS ingest.structured_uploads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_name VARCHAR(255) NOT NULL,
    format VARCHAR(10) NOT NULL,                    -- csv/tsv/xlsx/md/json
    size_bytes INTEGER NOT NULL DEFAULT 0,
    sha256 VARCHAR(64) NOT NULL,                    -- 文件指纹，重复上传可识别
    columns JSONB NOT NULL DEFAULT '[]',
    row_count INTEGER NOT NULL DEFAULT 0,
    rows JSONB NOT NULL DEFAULT '[]',               -- 解析后的规范化行
    warnings JSONB NOT NULL DEFAULT '[]',           -- 多表格 / 嵌套值 / 重名列等
    contract_id UUID,                               -- 绑定契约时回填，便于追溯
    uploaded_by VARCHAR(50) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_structured_uploads_created
    ON ingest.structured_uploads (created_at DESC);
