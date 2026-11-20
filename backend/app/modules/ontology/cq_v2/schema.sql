-- =============================================================================
-- CQ 驱动的企业本体建设与持续演进 V2.0 —— PostgreSQL 增量 DDL（全量 P0+P1+P2）
--
-- 基线：ontology.competency_questions / ontology.cq_coverage_snapshots（V1.2）
-- 原则：全脚本幂等；不重建表、不删列、不改现有数据；V1.2 CQ API 语义不变。
--       结构化事实仍以 PostgreSQL 为唯一权威来源，图投影可重建。
-- 覆盖：P0 四层建模/标准答案/质量准入/版本；P1 Schema Registry/Query Plan/
--       五维覆盖/六类缺口/执行审计；P2 决策型 CQ/演进闭环。
-- =============================================================================

-- ---------------------------------------------------------------- P0：四层建模
CREATE TABLE IF NOT EXISTS ontology.business_capabilities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code            VARCHAR(40) NOT NULL UNIQUE,
    name            VARCHAR(120) NOT NULL,
    business_domain VARCHAR(40) NOT NULL,
    description     TEXT,
    owner           VARCHAR(60),
    priority        SMALLINT NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
    risk_level      VARCHAR(20) NOT NULL DEFAULT 'normal'
                    CHECK (risk_level IN ('normal','high','critical')),
    weight          NUMERIC(4,2) NOT NULL DEFAULT 1.0,
    status          VARCHAR(20) NOT NULL DEFAULT 'active'
                    CHECK (status IN ('draft','active','retired')),
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- CQ 与业务能力多对多：一个能力对应多条 CQ，一条 CQ 可服务多个能力
CREATE TABLE IF NOT EXISTS ontology.capability_cqs (
    capability_id   UUID NOT NULL REFERENCES ontology.business_capabilities(id) ON DELETE CASCADE,
    cq_id           UUID NOT NULL REFERENCES ontology.competency_questions(id) ON DELETE CASCADE,
    link_weight     NUMERIC(4,2) NOT NULL DEFAULT 1.0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (capability_id, cq_id)
);

-- 本体需求项：显式主键/外键/唯一约束（评审第三部分的直接修正）
CREATE TABLE IF NOT EXISTS ontology.cq_requirements (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cq_id           UUID NOT NULL REFERENCES ontology.competency_questions(id) ON DELETE CASCADE,
    cq_version      INTEGER NOT NULL DEFAULT 1,
    kind            VARCHAR(20) NOT NULL
                    CHECK (kind IN ('entity','relation','attribute','rule','evidence','source')),
    ref             VARCHAR(120) NOT NULL,
    schema_version  VARCHAR(40),
    mandatory       BOOLEAN NOT NULL DEFAULT TRUE,
    status          VARCHAR(20) NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','covered','proposed','waived')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (cq_id, cq_version, kind, ref)
);
CREATE INDEX IF NOT EXISTS idx_cqr_ref ON ontology.cq_requirements (kind, ref);

-- 现有 CQ 表增量扩展（A 问题定义 / B 语义与验收 / C 质量与评估 / 版本与权限）
ALTER TABLE ontology.competency_questions
    ADD COLUMN IF NOT EXISTS question_template       TEXT,
    ADD COLUMN IF NOT EXISTS business_domain        VARCHAR(40),
    ADD COLUMN IF NOT EXISTS business_purpose       TEXT,
    ADD COLUMN IF NOT EXISTS cq_kind                VARCHAR(20) NOT NULL DEFAULT 'business',
    ADD COLUMN IF NOT EXISTS target_type            VARCHAR(40),
    ADD COLUMN IF NOT EXISTS scope_constraints      JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS temporal_constraints   JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS expected_answer        JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS expected_entities      JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS expected_relations     JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS expected_evidence      JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS acceptance_criteria    JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS evidence_requirements  JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS evaluation_method      VARCHAR(30),
    ADD COLUMN IF NOT EXISTS test_dataset_id        UUID,
    ADD COLUMN IF NOT EXISTS quality_score          NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS quality_dimensions     JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS hard_gates             JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS quality_rubric_version VARCHAR(20),
    ADD COLUMN IF NOT EXISTS review_status          VARCHAR(20) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS layer_states           JSONB NOT NULL DEFAULT
        '{"business":"proposed","ontology":"mapped","evaluation":"drafted"}'::jsonb,
    ADD COLUMN IF NOT EXISTS version                INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS schema_version         VARCHAR(40),
    ADD COLUMN IF NOT EXISTS query_plan_version     VARCHAR(40),
    ADD COLUMN IF NOT EXISTS cq_weight              NUMERIC(4,2) NOT NULL DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS risk_level             VARCHAR(20) NOT NULL DEFAULT 'normal',
    ADD COLUMN IF NOT EXISTS admitted_at            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS admitted_by            VARCHAR(60),
    ADD COLUMN IF NOT EXISTS valid_from             TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS valid_to               TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='cq_v2_kind_chk'
                   AND conrelid='ontology.competency_questions'::regclass) THEN
        ALTER TABLE ontology.competency_questions ADD CONSTRAINT cq_v2_kind_chk
            CHECK (cq_kind IN ('business','test'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='cq_v2_score_chk'
                   AND conrelid='ontology.competency_questions'::regclass) THEN
        ALTER TABLE ontology.competency_questions ADD CONSTRAINT cq_v2_score_chk
            CHECK (quality_score IS NULL OR quality_score BETWEEN 0 AND 100);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='cq_v2_review_chk'
                   AND conrelid='ontology.competency_questions'::regclass) THEN
        ALTER TABLE ontology.competency_questions ADD CONSTRAINT cq_v2_review_chk
            CHECK (review_status IN ('pending','machine_passed','human_approved','rejected'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='cq_v2_risk_chk'
                   AND conrelid='ontology.competency_questions'::regclass) THEN
        ALTER TABLE ontology.competency_questions ADD CONSTRAINT cq_v2_risk_chk
            CHECK (risk_level IN ('normal','high','critical'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_cq_v2_admission
    ON ontology.competency_questions (review_status, quality_score DESC);
CREATE INDEX IF NOT EXISTS idx_cq_v2_domain ON ontology.competency_questions (business_domain);

-- 测试实例：CQ 定义 : 实例 = 1 : N；标准答案来源白名单
CREATE TABLE IF NOT EXISTS ontology.cq_test_instances (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cq_id               UUID NOT NULL REFERENCES ontology.competency_questions(id) ON DELETE CASCADE,
    cq_version          INTEGER NOT NULL DEFAULT 1,
    instance_code       VARCHAR(40) NOT NULL,
    params              JSONB NOT NULL DEFAULT '{}'::jsonb,
    question_text       TEXT NOT NULL,
    expected_answer     JSONB NOT NULL,
    expected_entities   JSONB NOT NULL DEFAULT '[]'::jsonb,
    expected_relations  JSONB NOT NULL DEFAULT '[]'::jsonb,
    expected_evidence   JSONB NOT NULL DEFAULT '{}'::jsonb,
    acceptance_criteria JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_refs       JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_type         VARCHAR(30) NOT NULL,
    source_ref          TEXT,
    annotated_by        VARCHAR(60),
    valid_time          JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              VARCHAR(20) NOT NULL DEFAULT 'draft',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (cq_id, instance_code),
    CONSTRAINT cti_source_chk CHECK (source_type IN (
        'authoritative_record','business_system','human_annotation','llm_draft_unverified')),
    CONSTRAINT cti_status_chk CHECK (status IN ('draft','validated','active','deprecated')),
    -- 红线：同一 LLM 生成的草稿答案未经人工确认不得成为正式标准答案
    CONSTRAINT cti_gold_chk CHECK (status <> 'active' OR source_type <> 'llm_draft_unverified')
);
CREATE INDEX IF NOT EXISTS idx_cti_cq ON ontology.cq_test_instances (cq_id, status);

-- 六维质量评分历史（rubric 版本化，历史不重算）
CREATE TABLE IF NOT EXISTS ontology.cq_quality_assessments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cq_id           UUID NOT NULL REFERENCES ontology.competency_questions(id) ON DELETE CASCADE,
    cq_version      INTEGER NOT NULL,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dimensions      JSONB NOT NULL,
    weights         JSONB NOT NULL,
    rubric_version  VARCHAR(20) NOT NULL,
    quality_score   NUMERIC(5,2) NOT NULL,
    hard_gates      JSONB NOT NULL,
    grade           VARCHAR(20) NOT NULL,
    admitted        BOOLEAN NOT NULL DEFAULT FALSE,
    stage_results   JSONB NOT NULL DEFAULT '[]'::jsonb,
    machine_notes   TEXT,
    assessed_by     VARCHAR(20) NOT NULL DEFAULT 'rule_engine',
    reviewed_by     VARCHAR(60)
);
CREATE INDEX IF NOT EXISTS idx_cqa_cq ON ontology.cq_quality_assessments (cq_id, run_at DESC);

-- 回答质量回归（版本三元组）
CREATE TABLE IF NOT EXISTS ontology.cq_evaluation_runs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id            UUID NOT NULL,
    run_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cq_id             UUID NOT NULL REFERENCES ontology.competency_questions(id) ON DELETE CASCADE,
    cq_version        INTEGER NOT NULL,
    testset_version   VARCHAR(40) NOT NULL,
    executor_version  VARCHAR(40) NOT NULL,
    instance_count    INTEGER NOT NULL DEFAULT 0,
    results           JSONB NOT NULL DEFAULT '[]'::jsonb,
    accuracy          NUMERIC(4,3),
    completeness      NUMERIC(4,3),
    evidence_rate     NUMERIC(4,3),
    calibration       NUMERIC(4,3),
    passed            BOOLEAN,
    notes             TEXT
);
CREATE INDEX IF NOT EXISTS idx_cer_run ON ontology.cq_evaluation_runs (run_id, cq_id);

-- CQ 集合质量（Portfolio Quality）
CREATE TABLE IF NOT EXISTS ontology.cq_portfolio_snapshots (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    admitted_total          INTEGER NOT NULL DEFAULT 0,
    capability_total        INTEGER,
    capability_coverage     NUMERIC(4,3),
    intent_distribution     JSONB NOT NULL DEFAULT '{}'::jsonb,
    intent_entropy          NUMERIC(6,3),
    entity_coverage         JSONB NOT NULL DEFAULT '{}'::jsonb,
    relation_coverage       JSONB NOT NULL DEFAULT '{}'::jsonb,
    difficulty_distribution JSONB NOT NULL DEFAULT '{}'::jsonb,
    coverage_matrix         JSONB NOT NULL DEFAULT '{}'::jsonb,
    findings                JSONB NOT NULL DEFAULT '[]'::jsonb,
    rubric_version          VARCHAR(20)
);
CREATE INDEX IF NOT EXISTS idx_cps_run ON ontology.cq_portfolio_snapshots (run_at DESC);

-- ---------------------------------------------------------------- P1：Schema 治理
-- 统一 Schema Registry：LinkML 为单一建模来源，运行时目录由此生成
CREATE TABLE IF NOT EXISTS ontology.schema_registry (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    schema_version      VARCHAR(40) NOT NULL UNIQUE,
    linkml_yaml         TEXT,
    entity_types        JSONB NOT NULL DEFAULT '{}'::jsonb,
    relation_types      JSONB NOT NULL DEFAULT '{}'::jsonb,
    attributes          JSONB NOT NULL DEFAULT '{}'::jsonb,
    constraints         JSONB NOT NULL DEFAULT '{}'::jsonb,
    validation_rules    JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              VARCHAR(20) NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','published','deprecated')),
    pinned_by_cq        JSONB NOT NULL DEFAULT '[]'::jsonb,
    published_by        VARCHAR(60),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Schema 迁移：Diff / Plan / 兼容性 / 数据迁移 / 回滚 / 版本锁定
CREATE TABLE IF NOT EXISTS ontology.schema_migrations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_version        VARCHAR(40),
    to_version          VARCHAR(40) NOT NULL,
    diff                JSONB NOT NULL DEFAULT '{}'::jsonb,
    migration_plan      JSONB NOT NULL DEFAULT '[]'::jsonb,
    compatibility       VARCHAR(20) NOT NULL DEFAULT 'unknown'
                        CHECK (compatibility IN ('compatible','breaking','unknown')),
    data_migration      JSONB NOT NULL DEFAULT '{}'::jsonb,
    rollback_sql        TEXT,
    rollback_version    VARCHAR(40),
    applied_at          TIMESTAMPTZ,
    applied_by          VARCHAR(60),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Schema Proposal：Reuse/Extend/Merge/Split/Create/Deprecate + 审核记录
CREATE TABLE IF NOT EXISTS ontology.schema_proposals (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code                VARCHAR(40) NOT NULL UNIQUE,
    cq_id               UUID REFERENCES ontology.competency_questions(id) ON DELETE SET NULL,
    term                VARCHAR(120) NOT NULL,
    operation           VARCHAR(20) NOT NULL
                        CHECK (operation IN ('reuse','extend','merge','split','create','deprecate')),
    target_ref          VARCHAR(120),
    reason              TEXT NOT NULL,
    examples            JSONB NOT NULL DEFAULT '[]'::jsonb,
    synonyms            JSONB NOT NULL DEFAULT '[]'::jsonb,
    necessity           JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','approved','rejected','published')),
    proposed_by         VARCHAR(30) NOT NULL DEFAULT 'llm',
    reviewed_by         VARCHAR(60),
    reviewed_at         TIMESTAMPTZ,
    schema_version      VARCHAR(40),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sp_status ON ontology.schema_proposals (status, operation);

-- Query Plan AST：结构化、可校验、版本化；禁止任意 SQL/Cypher
CREATE TABLE IF NOT EXISTS ontology.cq_query_plans (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_version        VARCHAR(40) NOT NULL UNIQUE,
    cq_id               UUID NOT NULL REFERENCES ontology.competency_questions(id) ON DELETE CASCADE,
    cq_version          INTEGER NOT NULL,
    schema_version      VARCHAR(40) NOT NULL,
    ast                 JSONB NOT NULL,
    executor_chain      JSONB NOT NULL DEFAULT '[]'::jsonb,
    complexity          JSONB NOT NULL DEFAULT '{}'::jsonb,
    permissions         JSONB NOT NULL DEFAULT '{}'::jsonb,
    checksum            VARCHAR(80) NOT NULL,
    validated           BOOLEAN NOT NULL DEFAULT FALSE,
    validation_notes    JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cqp_cq ON ontology.cq_query_plans (cq_id, created_at DESC);

-- 执行审计：Plan/Schema/数据快照/模型版本/证据/结果全留痕
CREATE TABLE IF NOT EXISTS ontology.cq_execution_runs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID NOT NULL,
    cq_id               UUID NOT NULL REFERENCES ontology.competency_questions(id) ON DELETE CASCADE,
    run_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    plan_version        VARCHAR(40),
    schema_version      VARCHAR(40),
    cq_version          INTEGER,
    data_snapshot_id    VARCHAR(80),
    model_version       VARCHAR(60),
    tenant_id           VARCHAR(60),
    actor               VARCHAR(60),
    permission_context  JSONB NOT NULL DEFAULT '{}'::jsonb,
    state               VARCHAR(20) NOT NULL,
    answer              JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_refs       JSONB NOT NULL DEFAULT '[]'::jsonb,
    conflict_refs       JSONB NOT NULL DEFAULT '[]'::jsonb,
    elapsed_ms          INTEGER,
    audit               JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_cxr_run ON ontology.cq_execution_runs (run_id, cq_id);
CREATE INDEX IF NOT EXISTS idx_cxr_time ON ontology.cq_execution_runs (run_at DESC);

-- 五维覆盖率 + 五态
CREATE TABLE IF NOT EXISTS ontology.cq_coverage_v2 (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID NOT NULL,
    cq_id               UUID NOT NULL REFERENCES ontology.competency_questions(id) ON DELETE CASCADE,
    run_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    schema_version      VARCHAR(40),
    cq_version          INTEGER,
    data_snapshot_id    VARCHAR(80),
    state               VARCHAR(20) NOT NULL
                        CHECK (state IN ('answered','partial','unanswerable','conflict','restricted')),
    schema_coverage     NUMERIC(4,3) NOT NULL DEFAULT 0,
    data_coverage       NUMERIC(4,3) NOT NULL DEFAULT 0,
    evidence_coverage   NUMERIC(4,3) NOT NULL DEFAULT 0,
    answer_coverage     NUMERIC(4,3) NOT NULL DEFAULT 0,
    temporal_coverage   NUMERIC(4,3) NOT NULL DEFAULT 0,
    weighted_score      NUMERIC(4,3) NOT NULL DEFAULT 0,
    cq_weight           NUMERIC(4,2) NOT NULL DEFAULT 1.0,
    detail              JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_ccv_run ON ontology.cq_coverage_v2 (run_id, cq_id);

-- 六类缺口 + GapPriority
CREATE TABLE IF NOT EXISTS ontology.cq_gaps (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID NOT NULL,
    cq_id               UUID NOT NULL REFERENCES ontology.competency_questions(id) ON DELETE CASCADE,
    kind                VARCHAR(24) NOT NULL
                        CHECK (kind IN ('entity','relation','attribute','evidence','schema','source_access')),
    ref                 VARCHAR(120),
    severity            NUMERIC(4,2) NOT NULL DEFAULT 1.0,
    business_impact     NUMERIC(4,2) NOT NULL DEFAULT 1.0,
    cq_weight           NUMERIC(4,2) NOT NULL DEFAULT 1.0,
    gap_priority        NUMERIC(8,2) NOT NULL DEFAULT 1.0,
    action              JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cg_priority ON ontology.cq_gaps (run_id, gap_priority DESC);

-- ---------------------------------------------------------------- P2：决策与演进
CREATE TABLE IF NOT EXISTS ontology.cq_decisions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cq_id               UUID NOT NULL REFERENCES ontology.competency_questions(id) ON DELETE CASCADE,
    run_id              UUID NOT NULL,
    inputs              JSONB NOT NULL DEFAULT '{}'::jsonb,
    rules_applied       JSONB NOT NULL DEFAULT '[]'::jsonb,
    suggestion          VARCHAR(40) NOT NULL,
    rationale           JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence          NUMERIC(4,3) NOT NULL DEFAULT 0,
    risk_flags          JSONB NOT NULL DEFAULT '[]'::jsonb,
    uncertainty         JSONB NOT NULL DEFAULT '{}'::jsonb,
    approval_status     VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (approval_status IN ('pending','approved','rejected','executed')),
    approved_by         VARCHAR(60),
    approved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ontology.ontology_evolution_runs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID NOT NULL UNIQUE,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMPTZ,
    steps               JSONB NOT NULL DEFAULT '[]'::jsonb,
    capability_coverage NUMERIC(4,3),
    accuracy_high_risk  NUMERIC(4,3),
    evidence_rate       NUMERIC(4,3),
    gap_summary         JSONB NOT NULL DEFAULT '{}'::jsonb,
    proposals_generated INTEGER NOT NULL DEFAULT 0,
    baseline_before     JSONB NOT NULL DEFAULT '{}'::jsonb,
    baseline_after      JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              VARCHAR(20) NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running','completed','failed'))
);

-- ---------------------------------------------------------------- 通用持久化
-- 非关系型 V2 对象的通用存取（JSONB）；关键结果同时写入上面的关系表供视图消费
CREATE TABLE IF NOT EXISTS ontology.cq_v2_records (
    table_name  VARCHAR(60) NOT NULL,
    id          VARCHAR(64) NOT NULL,
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (table_name, id)
);
CREATE INDEX IF NOT EXISTS idx_cqrec_table ON ontology.cq_v2_records (table_name);

-- ---------------------------------------------------------------- 视图
-- Gate A：质量准入门禁（硬门槛由 SQL 判定，不经过 LLM）
CREATE OR REPLACE VIEW ontology.v_cq_admission_v2 AS
SELECT c.id, c.code, c.question, c.cq_kind, c.business_domain,
       c.intent, c.status, c.review_status, c.quality_score,
       c.quality_dimensions, c.hard_gates, c.version, c.risk_level,
       CASE WHEN c.quality_score >= 90 THEN '优秀'
            WHEN c.quality_score >= 80 THEN '良好'
            WHEN c.quality_score >= 70 THEN '合格'
            WHEN c.quality_score >= 60 THEN '待优化'
            ELSE '不合格' END AS grade,
       (c.status <> 'retired'
        AND c.review_status = 'human_approved'
        AND c.quality_score >= 80
        AND COALESCE((c.quality_dimensions->>'target')::numeric, 0) <> 0
        AND COALESCE((c.quality_dimensions->>'clarity')::numeric, 0) >= 3
        AND COALESCE((c.quality_dimensions->>'verifiability')::numeric, 0) >= 2
       ) AS admitted
FROM ontology.competency_questions c;

-- 三指标分离：质量 / 可回答性 / 回答质量
CREATE OR REPLACE VIEW ontology.v_cq_metrics_v2 AS
SELECT c.id, c.code, c.question, c.version AS cq_version,
       c.quality_score, a.grade, a.admitted,
       v.state AS answerability_state, v.weighted_score AS answerability_score,
       v.run_at AS answerability_at,
       e.accuracy AS answer_accuracy, e.evidence_rate AS answer_evidence_rate,
       e.run_at AS evaluated_at
FROM ontology.competency_questions c
LEFT JOIN ontology.v_cq_admission_v2 a ON a.id = c.id
LEFT JOIN LATERAL (
    SELECT state, weighted_score, run_at FROM ontology.cq_coverage_v2
    WHERE cq_id = c.id ORDER BY run_at DESC LIMIT 1) v ON TRUE
LEFT JOIN LATERAL (
    SELECT accuracy, evidence_rate, run_at FROM ontology.cq_evaluation_runs
    WHERE cq_id = c.id ORDER BY run_at DESC LIMIT 1) e ON TRUE;

COMMENT ON TABLE ontology.business_capabilities IS
    '四层结构第 1 层：业务能力；一个能力对应多条 CQ，可被多个本体概念复用';
COMMENT ON TABLE ontology.cq_requirements IS
    '四层结构第 3 层：CQ 推导出的本体需求，带主键/外键/唯一约束与版本化引用';
COMMENT ON TABLE ontology.cq_query_plans IS
    '结构化查询计划 AST；LLM 只提计划，不生成任意 SQL/Cypher，计划须过 Schema/权限/复杂度校验';
COMMENT ON TABLE ontology.cq_coverage_v2 IS
    '五维覆盖 + 五态；加权覆盖率分母只取通过质量准入的 CQ';
COMMENT ON TABLE ontology.cq_gaps IS
    '六类缺口；GapPriority = BusinessImpact × CQWeight × GapSeverity';
-- =============================================================================
