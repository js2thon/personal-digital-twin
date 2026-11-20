-- Ontology 语义层增强（全新增量 DDL，不改动任何既有表）。
-- 设计：PostgreSQL 为权威存储；所有语句幂等可重复执行；新表一律带 tenant_id，
-- 供 app/policy.read_clause 逐点过滤。规则/推理产物单独标记 derived，绝不回写权威事实。
-- 版本：SIL-Semantic v1.0（对齐 V1.3 语义层方案 Phase A/B/C 最小闭环）。

CREATE SCHEMA IF NOT EXISTS ontology;

-- 1. 语义 Schema 版本快照（Schema-as-Code 单一建模源的落库副本）
CREATE TABLE IF NOT EXISTS ontology.semantic_schemas (
    schema_version    text PRIMARY KEY,
    ontology_version  text NOT NULL,
    tenant_id         text NOT NULL DEFAULT 'default',
    definition        jsonb NOT NULL,
    status            text NOT NULL DEFAULT 'published',
    created_by        text NOT NULL DEFAULT 'system',
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- 2. 本体实例（与 knowledge.facts 双向对齐；source_fact_ids 显式关联）
CREATE TABLE IF NOT EXISTS ontology.entity_instances (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         text NOT NULL DEFAULT 'default',
    space_id          text NOT NULL DEFAULT 'default',
    type_name         text NOT NULL,
    canonical_name    text NOT NULL,
    properties        jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_fact_ids   jsonb NOT NULL DEFAULT '[]'::jsonb,
    ontology_version  text NOT NULL DEFAULT 'v1.0.0',
    valid_from        timestamptz,
    valid_to          timestamptz,
    status            text NOT NULL DEFAULT 'active',
    created_by        text NOT NULL DEFAULT 'system',
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_entity_instances_tenant_type
    ON ontology.entity_instances (tenant_id, type_name);

-- 3. 实例关系（写入侧强制约束后的权威边 + 推理衍生边 derived=true）
CREATE TABLE IF NOT EXISTS ontology.instance_relations (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         text NOT NULL DEFAULT 'default',
    space_id          text NOT NULL DEFAULT 'default',
    source_id         text NOT NULL,
    relation_type     text NOT NULL,
    target_id         text NOT NULL,
    source_type       text,
    target_type       text,
    derived           boolean NOT NULL DEFAULT false,
    derived_by        text,
    ontology_version  text NOT NULL DEFAULT 'v1.0.0',
    properties        jsonb NOT NULL DEFAULT '{}'::jsonb,
    status            text NOT NULL DEFAULT 'active',
    created_by        text NOT NULL DEFAULT 'system',
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_instance_relations_tenant
    ON ontology.instance_relations (tenant_id, relation_type);

-- 4. 业务规则（integrity / derivation / authorization；条件→动作，结果只作证据）
CREATE TABLE IF NOT EXISTS ontology.business_rules (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         text NOT NULL DEFAULT 'default',
    version_id        text,
    name              text NOT NULL,
    rule_type         text NOT NULL DEFAULT 'integrity',
    condition         jsonb NOT NULL,
    action            jsonb NOT NULL,
    priority          integer NOT NULL DEFAULT 100,
    enabled           boolean NOT NULL DEFAULT true,
    created_by        text NOT NULL DEFAULT 'system',
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

-- 5. 任务启动时的版本冻结快照（决策/查询可还原到具体 ontology_version）
CREATE TABLE IF NOT EXISTS ontology.runtime_task_snapshots (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id           text NOT NULL,
    tenant_id         text NOT NULL DEFAULT 'default',
    ontology_version  text NOT NULL,
    schema_version    text NOT NULL,
    schema_snapshot   jsonb NOT NULL,
    server_context    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by        text NOT NULL DEFAULT 'system',
    frozen_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, task_id)
);

-- 6. CQ 执行：幂等键（服务端上下文强制注入，同键同结果，重启不丢）
CREATE TABLE IF NOT EXISTS ontology.cq_idempotency (
    idempotency_key   text NOT NULL,
    tenant_id         text NOT NULL DEFAULT 'default',
    cq_id             text NOT NULL,
    run_id            text NOT NULL,
    result            jsonb NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, cq_id, idempotency_key)
);

-- 7. CQ 执行记录（权威持久化，含服务端注入上下文与应用的规则/CQ）
CREATE TABLE IF NOT EXISTS ontology.cq_semantic_runs (
    run_id            text PRIMARY KEY,
    tenant_id         text NOT NULL DEFAULT 'default',
    cq_id             text NOT NULL,
    ontology_version  text NOT NULL,
    schema_version    text NOT NULL,
    space_id          text NOT NULL DEFAULT 'default',
    parameters        jsonb NOT NULL DEFAULT '{}'::jsonb,
    server_context    jsonb NOT NULL DEFAULT '{}'::jsonb,
    state             text NOT NULL,
    answer            jsonb,
    evidence_refs     jsonb NOT NULL DEFAULT '[]'::jsonb,
    applied_rules     jsonb NOT NULL DEFAULT '[]'::jsonb,
    version_bound     boolean NOT NULL DEFAULT false,
    idempotency_key   text,
    created_by        text NOT NULL DEFAULT 'system',
    created_at        timestamptz NOT NULL DEFAULT now()
);
-- 存量表幂等补列（CREATE TABLE IF NOT EXISTS 不会为已有表加列）
ALTER TABLE ontology.cq_semantic_runs
    ADD COLUMN IF NOT EXISTS version_bound boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS ix_cq_semantic_runs_tenant
    ON ontology.cq_semantic_runs (tenant_id, cq_id);
-- 关系证据回链：关系派生自哪些 facts（jsonb 数组，与实体表同款）
ALTER TABLE ontology.instance_relations
    ADD COLUMN IF NOT EXISTS source_fact_ids jsonb NOT NULL DEFAULT '[]'::jsonb;
-- 状态机流转（P0-1）：实例更新留痕时间
ALTER TABLE ontology.entity_instances
    ADD COLUMN IF NOT EXISTS updated_at timestamptz;

-- 8. CQ 回归集 + 回归运行（结果指纹可重复比对，稳定即通过）
CREATE TABLE IF NOT EXISTS ontology.cq_regression_sets (
    set_id            text PRIMARY KEY,
    tenant_id         text NOT NULL DEFAULT 'default',
    name              text NOT NULL,
    cases             jsonb NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ontology.cq_regression_runs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    set_id            text NOT NULL,
    tenant_id         text NOT NULL DEFAULT 'default',
    ontology_version  text NOT NULL,
    result_fingerprint text NOT NULL,
    stable            boolean NOT NULL,
    case_count        integer NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- 9. 决策 Trace（展示「应用了哪些本体规则 / CQ 结果」；候选不改写权威 Fact）
CREATE TABLE IF NOT EXISTS ontology.decision_rule_trace (
    run_id            text PRIMARY KEY,
    tenant_id         text NOT NULL DEFAULT 'default',
    cq_id             text,
    ontology_version  text NOT NULL,
    applied_rules     jsonb NOT NULL DEFAULT '[]'::jsonb,
    cq_results        jsonb NOT NULL DEFAULT '[]'::jsonb,
    derived_candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- 10. 语义写入审计（约束拒绝留痕，供约束违反率指标）
CREATE TABLE IF NOT EXISTS ontology.semantic_write_audit (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         text NOT NULL DEFAULT 'default',
    operation         text NOT NULL,
    target_ref        text,
    outcome           text NOT NULL,
    violations        jsonb NOT NULL DEFAULT '[]'::jsonb,
    ontology_version  text,
    created_by        text NOT NULL DEFAULT 'system',
    created_at        timestamptz NOT NULL DEFAULT now()
);
