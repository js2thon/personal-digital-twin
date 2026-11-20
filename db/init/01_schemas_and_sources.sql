-- 01: Schema 与数据来源/原始文档
-- 权威事实源原则：PostgreSQL 是个人知识、认知、规则和决策记录的唯一权威存储。
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE SCHEMA IF NOT EXISTS source;
CREATE SCHEMA IF NOT EXISTS ingest;
CREATE SCHEMA IF NOT EXISTS knowledge;
CREATE SCHEMA IF NOT EXISTS ontology;
CREATE SCHEMA IF NOT EXISTS memory;
CREATE SCHEMA IF NOT EXISTS cognition;
CREATE SCHEMA IF NOT EXISTS decision;
CREATE SCHEMA IF NOT EXISTS workflow;
CREATE SCHEMA IF NOT EXISTS audit;

-- ---------- A. 数据来源和原始文档 ----------
CREATE TABLE IF NOT EXISTS source.data_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type VARCHAR(50) NOT NULL,          -- chat_export/email/file/note/meeting/web
    source_name VARCHAR(200) NOT NULL,
    connector_config JSONB DEFAULT '{}',       -- 仅存非敏感配置，凭据入密钥管理
    sync_cursor JSONB DEFAULT '{}',
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS source.documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID REFERENCES source.data_sources(id),
    external_id VARCHAR(500),
    content_hash VARCHAR(128) NOT NULL,
    object_uri TEXT,                            -- MinIO 对象地址
    mime_type VARCHAR(100),
    title TEXT,
    metadata JSONB DEFAULT '{}',
    document_status VARCHAR(30) NOT NULL DEFAULT 'pending',  -- pending/parsed/failed/archived
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON source.documents(content_hash);

-- ---------- B. 蒸馏流水线任务（状态机）----------
CREATE TABLE IF NOT EXISTS ingest.pipeline_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES source.documents(id),
    pipeline_version VARCHAR(50) NOT NULL DEFAULT 'v1',
    idempotency_key VARCHAR(128) NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'PENDING',
    -- PENDING/INGESTING/PARSING/CHUNKING/EXTRACTING/VALIDATING/
    -- ENTITY_RESOLUTION/KNOWLEDGE_REVIEW/PUBLISHING/COMPLETED/
    -- RETRY_WAIT/FAILED/CANCELLED/NEEDS_REVIEW/REVIEW_DISMISSED
    -- NEEDS_REVIEW 由本人处置；处置动作与验证结论见 ingest.pipeline_reviews。
    retry_count INTEGER DEFAULT 0,
    error_info JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON ingest.pipeline_runs(status);

CREATE TABLE IF NOT EXISTS ingest.pipeline_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES ingest.pipeline_runs(id),
    step_name VARCHAR(50) NOT NULL,
    input_refs JSONB DEFAULT '{}',
    output_refs JSONB DEFAULT '{}',
    model_version VARCHAR(50),
    prompt_version VARCHAR(50),
    duration_ms INTEGER,
    error_info TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Outbox 事件表：任务创建与状态变更经 Outbox 分发
-- ---------- B2. NEEDS_REVIEW 人工处置留痕 ----------
-- 故意不建 run_id 外键：级联删除任务时保留本人处置记录，审计链不断。
CREATE TABLE IF NOT EXISTS ingest.pipeline_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL,
    action VARCHAR(20) NOT NULL,             -- retry / dismiss
    reason VARCHAR(50),                      -- 原始原因码
    note TEXT DEFAULT '',                    -- 本人填写的处置说明
    actor VARCHAR(50) NOT NULL,              -- 服务端注入，不接受请求体自报
    before_status VARCHAR(30) NOT NULL,
    after_status VARCHAR(30) NOT NULL,
    verification VARCHAR(20) NOT NULL DEFAULT 'not_applicable',
    -- pending -> recovered / still_failing；由流水线结束时自动回填
    verified_at TIMESTAMPTZ,
    verified_detail JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pipeline_reviews_run
    ON ingest.pipeline_reviews (run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_reviews_pending
    ON ingest.pipeline_reviews (run_id) WHERE verification = 'pending';

-- SIL-EC-01：Outbox 事件账本。结构由「事实 + 事件同事务」这一硬约束决定，
-- 列与约束的运行时幂等升级在 backend/app/modules/ontology/outbox.py。
CREATE TABLE IF NOT EXISTS ingest.outbox_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type VARCHAR(100) NOT NULL,
    aggregate_type VARCHAR(50) NOT NULL DEFAULT 'Fact',
    aggregate_id UUID NOT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    ontology_version VARCHAR(50),
    payload JSONB NOT NULL,
    schema_version VARCHAR(10) NOT NULL DEFAULT '1',
    trace_id VARCHAR(64),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    processed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT outbox_event_type_enum CHECK (event_type IN (
        'fact.created', 'fact.updated', 'fact.withdrawn',
        'entity.merged', 'ontology.published', 'document.changed',
        'embedding.model_upgraded', 'permission.changed', 'source.synced')),
    CONSTRAINT outbox_status_enum CHECK (status IN (
        'pending', 'processing', 'processed', 'failed', 'dead')),
    CONSTRAINT outbox_attempts_nonnegative CHECK (attempts >= 0)
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON ingest.outbox_events (occurred_at)
    WHERE status IN ('pending', 'failed');
CREATE INDEX IF NOT EXISTS idx_outbox_aggregate
    ON ingest.outbox_events (aggregate_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_outbox_type
    ON ingest.outbox_events (event_type, occurred_at DESC);

-- `processed` 是 V1.2 的旧列（sources.py 仍在写），用触发器保证它永远等于
-- `status = 'processed'`，不允许两份状态漂移。
CREATE OR REPLACE FUNCTION ingest.fn_outbox_sync_processed() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status = 'pending' AND NEW.processed IS TRUE THEN
            NEW.status := 'processed';
            NEW.processed_at := COALESCE(NEW.processed_at, NOW());
            NEW.attempts := GREATEST(NEW.attempts, 1);
        ELSE
            NEW.processed := (NEW.status = 'processed');
        END IF;
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        NEW.processed := (NEW.status = 'processed');
    END IF;
    RETURN NEW;
END;
$fn$;
DROP TRIGGER IF EXISTS trg_outbox_sync_processed ON ingest.outbox_events;
CREATE TRIGGER trg_outbox_sync_processed
    BEFORE INSERT OR UPDATE ON ingest.outbox_events
    FOR EACH ROW EXECUTE FUNCTION ingest.fn_outbox_sync_processed();
