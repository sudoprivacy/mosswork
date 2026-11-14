-- Exact public DDL only; Moss d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d.
CREATE TABLE IF NOT EXISTS organizations (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        created_at INTEGER NOT NULL
      );
CREATE TABLE IF NOT EXISTS departments (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        parent_id TEXT REFERENCES departments(id),
        name TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
      );
CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        email TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        department_id TEXT REFERENCES departments(id),
        role TEXT NOT NULL DEFAULT 'user',
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('pending', 'active', 'locked', 'disabled')),
        membership_revision INTEGER NOT NULL DEFAULT 0,
        password_hash TEXT,
        password_updated_at INTEGER,
        last_login_at INTEGER,
        created_at INTEGER NOT NULL
      );
CREATE TABLE IF NOT EXISTS human_accounts (
    id TEXT PRIMARY KEY,
    phone TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'locked', 'disabled')),
    created_at BIGINT NOT NULL
  );
CREATE TABLE IF NOT EXISTS human_account_memberships (
    account_id TEXT NOT NULL REFERENCES human_accounts(id),
    org_id TEXT NOT NULL REFERENCES organizations(id),
    user_id TEXT NOT NULL UNIQUE,
    created_at BIGINT NOT NULL,
    PRIMARY KEY (account_id, org_id),
    FOREIGN KEY (user_id, org_id) REFERENCES users(id, org_id) ON DELETE CASCADE ON UPDATE CASCADE
  );
CREATE TABLE IF NOT EXISTS mcp_servers (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL,
        name TEXT NOT NULL,
        display_name TEXT,
        description TEXT,
        icon TEXT,
        category TEXT,
        risk_level TEXT NOT NULL DEFAULT 'low',
        responsible_person TEXT,
        scope TEXT NOT NULL,
        owner_type TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        mcp_type TEXT NOT NULL,
        url TEXT,
        command TEXT,
        args_json TEXT,
        env_json TEXT,
        timeout_ms INTEGER DEFAULT 30000,
        health_check_url TEXT,
        use_proxy INTEGER DEFAULT 0,
        auth_type TEXT DEFAULT 'none',
        secret_ref TEXT,
        auth_config_json TEXT,
        visible_to TEXT,
        bound_assistants TEXT,
        bound_skills TEXT,
        allow_read INTEGER DEFAULT 1,
        allow_write INTEGER DEFAULT 1,
        require_confirmation_for_write INTEGER DEFAULT 0,
        allow_read_sensitive_fields INTEGER DEFAULT 0,
        allow_outbound_network INTEGER DEFAULT 1,
        allow_scheduled_task INTEGER DEFAULT 0,
        audit_request INTEGER DEFAULT 0,
        audit_response_summary INTEGER DEFAULT 0,
        redact_sensitive_fields INTEGER DEFAULT 0,
        allow_user_disable INTEGER DEFAULT 1,
        status TEXT DEFAULT 'pending',
        enabled INTEGER DEFAULT 1,
        last_invocation_at INTEGER,
        created_by TEXT NOT NULL,
        updated_by TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
      );
CREATE TABLE IF NOT EXISTS mcp_policies (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL UNIQUE,
        allow_personal_mcp INTEGER DEFAULT 0,
        allow_stdio_mcp INTEGER DEFAULT 0,
        allow_http_sse_mcp INTEGER DEFAULT 0,
        allow_local_file_access INTEGER DEFAULT 0,
        allow_external_network INTEGER DEFAULT 0,
        domain_whitelist_json TEXT,
        require_approval INTEGER DEFAULT 0,
        allow_auto_task_call_personal_mcp INTEGER DEFAULT 0,
        allow_enterprise_assistant_call_personal_mcp INTEGER DEFAULT 0,
        allow_enterprise_context_in_personal_mcp INTEGER DEFAULT 0,
        require_confirmation_for_high_risk INTEGER DEFAULT 1,
        require_confirmation_for_write INTEGER DEFAULT 1,
        audit_request_params INTEGER DEFAULT 0,
        audit_response_summary INTEGER DEFAULT 0,
        redact_audit_logs INTEGER DEFAULT 0,
        limit_concurrency_and_rate INTEGER DEFAULT 0,
        restrict_callable_models INTEGER DEFAULT 0,
        updated_by TEXT,
        created_by TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
      );
CREATE TABLE IF NOT EXISTS agent_tool_reviews (
          org_id TEXT NOT NULL, resource TEXT NOT NULL, server_id TEXT NOT NULL, revision TEXT NOT NULL, document TEXT NOT NULL,
          PRIMARY KEY(org_id, resource)
        );
CREATE TABLE IF NOT EXISTS config_items (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        description   TEXT,
        icon          TEXT,
        pinyin        TEXT,
        scope         TEXT NOT NULL DEFAULT 'system',
        url_pattern   TEXT,
        scheme        TEXT,
        bearer_prefix TEXT,
        status        INTEGER DEFAULT 1,
        org_id        TEXT,
        created_at    INTEGER NOT NULL,
        updated_at    INTEGER NOT NULL,
        auth_type          TEXT,
        auth_url           TEXT,
        token_url          TEXT,
        client_id          TEXT,
        client_secret_key  TEXT,
        refresh_token_key  TEXT,
        default_scopes     TEXT,
        token_request_json TEXT,
        mint_script        TEXT,
        body_auth_check    TEXT
      );
CREATE TABLE IF NOT EXISTS config_entries (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        config_item_id INTEGER NOT NULL,
        config_key     TEXT NOT NULL,
        name           TEXT NOT NULL,
        config_desc    TEXT,
        required       INTEGER DEFAULT 0,
        created_at     INTEGER NOT NULL,
        updated_at     INTEGER NOT NULL,
        FOREIGN KEY (config_item_id) REFERENCES config_items(id) ON DELETE CASCADE,
        UNIQUE(config_item_id, config_key)
      );
