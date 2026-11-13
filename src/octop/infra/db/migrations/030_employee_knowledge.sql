-- Employee read grants for centrally managed knowledge bases.
CREATE TABLE IF NOT EXISTS employee_knowledge_bases (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kb_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, kb_id)
);
CREATE INDEX IF NOT EXISTS idx_employee_knowledge_kb ON employee_knowledge_bases(kb_id);

-- Canonical product identity projection:
-- Compatibility IDs only; canonical identities and grants stay in their own store.
CREATE TABLE IF NOT EXISTS canonical_user_projections (
    authority_id TEXT NOT NULL CHECK (length(authority_id) > 0),
    account_id TEXT NOT NULL CHECK (length(account_id) > 0),
    organization_id TEXT NOT NULL CHECK (length(organization_id) > 0),
    member_id TEXT NOT NULL CHECK (length(member_id) > 0),
    local_user_id BIGINT NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
    created_at BIGINT NOT NULL,
    PRIMARY KEY (authority_id, organization_id, member_id)
);
-- End canonical product identity projection.

-- Retained cohost product projection:
CREATE TABLE IF NOT EXISTS retained_session_projections (
    thread_id TEXT PRIMARY KEY REFERENCES threads(thread_id) ON DELETE RESTRICT,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE RESTRICT,
    authority_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    application_session_id TEXT NOT NULL,
    native_agent_id TEXT NOT NULL,
    durable_session_id TEXT NOT NULL,
    repository_path TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    UNIQUE(authority_id, organization_id, application_session_id)
);
-- End retained cohost product projection.

UPDATE _schema_version SET version = 30;
