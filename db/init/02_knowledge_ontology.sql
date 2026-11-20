-- 02: 知识（Claim/Evidence/Fact）、向量存储与本体
-- 关键设计：Claim 是模型提取的候选主张，Fact 是校验/人工确认后发布的知识。
-- 不要把所有 Claim 自动转为 Fact。

-- ---------- B. 证据和知识主张 ----------
CREATE TABLE IF NOT EXISTS knowledge.evidence_spans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES source.documents(id),
    content TEXT NOT NULL,
    locator JSONB DEFAULT '{}',                 -- 页码/段落/字符区间
    evidence_type VARCHAR(50),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS knowledge.claims (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_type VARCHAR(50) NOT NULL,            -- fact/preference/belief/event/relation/experience
    subject JSONB NOT NULL,
    predicate VARCHAR(200),
    object_value JSONB,
    confidence NUMERIC(5,4),
    status VARCHAR(30) NOT NULL DEFAULT 'candidate',  -- candidate/approved/rejected/merged
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    extraction_version VARCHAR(50),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS knowledge.claim_evidence (
    claim_id UUID NOT NULL REFERENCES knowledge.claims(id),
    evidence_id UUID NOT NULL REFERENCES knowledge.evidence_spans(id),
    support_type VARCHAR(30) NOT NULL,          -- direct/derived/context
    PRIMARY KEY(claim_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS knowledge.facts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id UUID REFERENCES knowledge.claims(id),
    subject_id UUID,
    predicate VARCHAR(200) NOT NULL,
    object_value JSONB NOT NULL,
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    recorded_at TIMESTAMPTZ DEFAULT NOW(),
    status VARCHAR(30) NOT NULL DEFAULT 'active',  -- active/contested/superseded/withdrawn
    version INTEGER DEFAULT 1,
    -- ===== SIL-DC-05 标准字段 + DC-08 公共字段（P0-3）=====
    -- 权威定义见 backend/app/modules/knowledge/contract.py；
    -- 存量库由 contract.ensure_schema() 幂等补列，两边必须保持一致。
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    datatype VARCHAR(20) NOT NULL DEFAULT 'text',        -- text/integer/number/boolean/date/datetime/duration/ref/json
    source_id UUID,                                      -- 来源文档（claim→evidence→document 解析）
    evidence_id UUID,                                    -- 首条证据（claim_evidence）
    verification_status VARCHAR(24) NOT NULL DEFAULT 'unverified',
                                                         -- unverified/machine_checked/human_confirmed/disputed
    ontology_version VARCHAR(50) NOT NULL DEFAULT 'v1.0.0',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by VARCHAR(64) NOT NULL DEFAULT 'unknown'    -- owner/agent/unknown
);
CREATE INDEX IF NOT EXISTS idx_facts_subject ON knowledge.facts(subject_id, predicate);
CREATE INDEX IF NOT EXISTS idx_facts_tenant_status ON knowledge.facts(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_facts_source ON knowledge.facts(source_id);
CREATE INDEX IF NOT EXISTS idx_facts_evidence ON knowledge.facts(evidence_id);

-- ---------- 4.3 向量存储（pgvector，维度按 Embedding 模型确定）----------
CREATE TABLE IF NOT EXISTS knowledge.semantic_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID REFERENCES source.documents(id),
    content TEXT NOT NULL,
    embedding VECTOR(1024),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
-- HNSW 索引（数据量大或召回要求高时启用）
-- CREATE INDEX IF NOT EXISTS idx_chunks_hnsw
--   ON knowledge.semantic_chunks USING hnsw (embedding vector_cosine_ops);

-- ---------- C. 本体和实体关系 ----------
CREATE TABLE IF NOT EXISTS ontology.ontology_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version VARCHAR(50) NOT NULL UNIQUE,
    schema_uri TEXT,
    schema_content JSONB NOT NULL,
    status VARCHAR(30) DEFAULT 'draft',         -- draft/published/deprecated
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ontology.entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type VARCHAR(100) NOT NULL,          -- 基础本体六域内的类型
    canonical_name TEXT NOT NULL,
    aliases JSONB DEFAULT '[]',
    properties JSONB DEFAULT '{}',
    ontology_version_id UUID REFERENCES ontology.ontology_versions(id),
    merged_into UUID,                           -- 实体合并走重定向，不删除
    status VARCHAR(30) DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_entities_type ON ontology.entities(entity_type);

CREATE TABLE IF NOT EXISTS ontology.relations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_entity_id UUID NOT NULL REFERENCES ontology.entities(id),
    relation_type VARCHAR(100) NOT NULL,
    target_entity_id UUID NOT NULL REFERENCES ontology.entities(id),
    properties JSONB DEFAULT '{}',
    evidence_claim_id UUID REFERENCES knowledge.claims(id),
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    status VARCHAR(30) DEFAULT 'candidate'      -- candidate/active/deprecated
);

-- 本体扩展建议（人工审核后发布新版本）
CREATE TABLE IF NOT EXISTS ontology.extension_proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal JSONB NOT NULL,                    -- {entity_type, definition, examples}
    rationale TEXT,
    status VARCHAR(30) DEFAULT 'pending',       -- pending/approved/rejected
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 首版基础本体种子数据（六域，约 15~25 个实体类型）
INSERT INTO ontology.ontology_versions (id, version, schema_content, status)
VALUES ('00000000-0000-0000-0000-000000000001', 'v1.0.0', '{
  "domains": {
    "Identity":     {"entities": ["Person", "Organization", "Role"]},
    "Knowledge":    {"entities": ["Knowledge", "Skill", "Domain", "Concept"]},
    "Experience":   {"entities": ["Experience", "Project", "Event", "Achievement"]},
    "Cognition":    {"entities": ["Belief", "Preference", "Value", "Principle"]},
    "Decision":     {"entities": ["Decision", "Option", "Criterion", "Rule"]},
    "Relationship": {"entities": ["Person", "Relationship", "Communication"]}
  }
}'::jsonb, 'published')
ON CONFLICT (version) DO NOTHING;
