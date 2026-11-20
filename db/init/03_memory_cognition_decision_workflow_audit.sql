-- 03: 记忆、认知、决策、工作流与审计

-- ---------- D. 个人记忆 ----------
CREATE TABLE IF NOT EXISTS memory.memory_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_type VARCHAR(50) NOT NULL,           -- episodic/semantic/preference/decision_pattern
    content JSONB NOT NULL,
    confidence NUMERIC(5,4),
    source_claim_ids UUID[] DEFAULT '{}',       -- MVP 用数组；生产版建议关联表
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    status VARCHAR(30) DEFAULT 'candidate',     -- candidate/confirmed/deprecated
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------- D. 认知模型（版本快照，不覆盖）----------
CREATE TABLE IF NOT EXISTS cognition.profile_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version INTEGER NOT NULL,
    profile JSONB NOT NULL,                     -- knowledge/preference/belief/decision/communication 五类 Profile
    evidence_refs JSONB DEFAULT '[]',
    review_status VARCHAR(30) NOT NULL DEFAULT 'pending',  -- pending/confirmed/rejected
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(version)
);

CREATE TABLE IF NOT EXISTS cognition.profile_proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_type VARCHAR(50) NOT NULL,          -- knowledge/preference/belief/decision/communication
    proposal JSONB NOT NULL,                    -- 含 preference/scope/status/effective_from
    evidence_count INTEGER DEFAULT 0,
    evidence_claim_ids UUID[] DEFAULT '{}',
    status VARCHAR(30) DEFAULT 'pending',       -- pending/confirmed/rejected
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------- E. 决策与人工审批（四级权限 D0~D3）----------
CREATE TABLE IF NOT EXISTS decision.decision_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_type VARCHAR(100) NOT NULL,            -- project_approval/tech_choice/email_reply/...
    input_context JSONB NOT NULL,
    recommendation VARCHAR(100),
    reasoning JSONB,
    evidence_refs JSONB,
    risk_level VARCHAR(30),
    confidence NUMERIC(5,4),
    policy_result VARCHAR(30),                  -- allowed/approval_required/denied
    status VARCHAR(30) NOT NULL DEFAULT 'analyzing',
    -- analyzing/waiting_approval/approved/rejected/executed/completed
    profile_version INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS decision.decision_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id UUID NOT NULL REFERENCES decision.decision_records(id),
    evidence_type VARCHAR(50),                  -- fact/memory/document/similar_decision
    reference_id UUID,
    quote TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS workflow.approval_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id UUID REFERENCES decision.decision_records(id),
    approver_id VARCHAR(200),
    approval_status VARCHAR(30) NOT NULL DEFAULT 'pending',  -- pending/approved/rejected/cancelled
    approval_comment TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    approved_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS workflow.execution_audits (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id UUID REFERENCES decision.decision_records(id),
    action VARCHAR(100) NOT NULL,
    executed_by VARCHAR(200),
    execution_result JSONB,
    executed_at TIMESTAMPTZ DEFAULT NOW()
);

-- 动作授权表：D3 自动执行按具体动作逐项授权（MVP 全部禁止）
CREATE TABLE IF NOT EXISTS workflow.action_authorizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action VARCHAR(100) NOT NULL UNIQUE,
    enabled BOOLEAN DEFAULT FALSE,              -- MVP: false
    scope VARCHAR(200),
    max_amount NUMERIC(14,2),
    requires_confirmation BOOLEAN DEFAULT TRUE,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------- 审计（不可变决策快照）----------
CREATE TABLE IF NOT EXISTS audit.decision_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id UUID NOT NULL REFERENCES decision.decision_records(id),
    snapshot JSONB NOT NULL,                    -- 输入/证据/Profile版本/模型与Prompt版本/建议
    snapshot_hash VARCHAR(128) NOT NULL,        -- 防篡改校验
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit.access_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor VARCHAR(200),
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(100),
    resource_id UUID,
    detail JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 用户反馈（事实错误/认知不符/判断不符，进入不同处理队列）
CREATE TABLE IF NOT EXISTS audit.user_feedbacks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    feedback_type VARCHAR(50) NOT NULL,         -- fact_error/cognition_mismatch/judgment_mismatch
    target_id UUID,                             -- decision_id / reply_id / chat_message_id
    content TEXT,
    handled BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------- 企业 IM消息接管：回复草稿与人工确认闸门 ----------
-- 铁律：草稿生成只读；任何外发必须先 reviewed(approved)，且逐条由本人确认。
CREATE TABLE IF NOT EXISTS workflow.message_replies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id VARCHAR(400) NOT NULL UNIQUE,    -- DWS openMessageId，幂等去重
    conversation_id VARCHAR(400),
    conversation_name VARCHAR(500),
    conversation_type VARCHAR(20) DEFAULT 'group',   -- group / direct（单聊外发参数不同）
    sender VARCHAR(300),
    sender_id VARCHAR(300),
    incoming_text TEXT,
    incoming_time VARCHAR(60),
    trigger_reason VARCHAR(100),                -- at_me / unread_question
    draft_text TEXT,                            -- 分身生成的草稿
    final_text TEXT,                            -- 本人编辑/确认后的定稿
    unconfirmed_points JSONB DEFAULT '[]',
    evidence JSONB DEFAULT '[]',                -- 检索到的事实/文档引用
    status VARCHAR(30) NOT NULL DEFAULT 'pending',  -- pending/approved/rejected/sent/failed
    reviewed_by VARCHAR(200),
    reviewed_at TIMESTAMPTZ,
    send_mode VARCHAR(20),                      -- dry_run / live
    send_result JSONB,
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_message_replies_status
    ON workflow.message_replies (status, created_at DESC);

-- 会话与扫描状态持久化（此前 _SESSIONS / _scan 都在内存里，重启即丢）
CREATE TABLE IF NOT EXISTS workflow.twin_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id VARCHAR(200) NOT NULL,
    request_id VARCHAR(100) NOT NULL UNIQUE,
    message TEXT,
    answer JSONB,
    task_type VARCHAR(50),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_twin_sessions_session
    ON workflow.twin_sessions (session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS workflow.scan_state (
    id SMALLINT PRIMARY KEY DEFAULT 1,
    state JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 本地老库升级：补单聊类型列（幂等）
ALTER TABLE workflow.message_replies
    ADD COLUMN IF NOT EXISTS conversation_type VARCHAR(20) DEFAULT 'group';
