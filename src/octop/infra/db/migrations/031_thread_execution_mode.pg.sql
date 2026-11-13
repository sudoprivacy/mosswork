ALTER TABLE threads ADD COLUMN IF NOT EXISTS execution_mode TEXT NOT NULL DEFAULT 'server';
ALTER TABLE threads ADD COLUMN IF NOT EXISTS continuation_of_thread_id TEXT;
ALTER TABLE threads DROP CONSTRAINT IF EXISTS threads_execution_mode_check;
ALTER TABLE threads ADD CONSTRAINT threads_execution_mode_check
    CHECK (execution_mode IN ('server', 'local'));

CREATE INDEX IF NOT EXISTS idx_threads_execution_mode ON threads(execution_mode);

CREATE TABLE IF NOT EXISTS thread_project_bindings (
    thread_id TEXT PRIMARY KEY REFERENCES threads(thread_id) ON DELETE CASCADE,
    owner_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    binding_revision INTEGER NOT NULL DEFAULT 1 CHECK (binding_revision > 0),
    state TEXT NOT NULL DEFAULT 'bound'
        CHECK (state IN ('unbound', 'bound', 'legacy', 'stale', 'unavailable', 'needs_rebind')),
    device_id TEXT,
    grant_id TEXT,
    grant_version INTEGER,
    installation_hash TEXT,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    UNIQUE(thread_id, project_id)
);
CREATE INDEX IF NOT EXISTS idx_thread_project_bindings_owner
    ON thread_project_bindings(owner_user_id, agent_id);

UPDATE threads
SET execution_mode = 'local'
WHERE thread_id IN (SELECT thread_id FROM company_runtime_threads);

INSERT INTO thread_project_bindings(
    thread_id, owner_user_id, agent_id, project_id, display_name,
    binding_revision, state, device_id, installation_hash, created_at, updated_at
)
SELECT c.thread_id, c.owner_user_id, c.agent_id, c.project_name, c.project_name,
       1, 'legacy', c.device_id, c.installation_hash, c.created_at, c.created_at
FROM company_runtime_threads c
WHERE NOT EXISTS (
    SELECT 1 FROM thread_project_bindings p WHERE p.thread_id = c.thread_id
);

UPDATE _schema_version SET version = 31;
