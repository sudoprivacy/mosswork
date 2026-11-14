-- Public schema only; no data. Moss server-v0.1.103 d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d.
CREATE TABLE IF NOT EXISTS organizations (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        created_at INTEGER NOT NULL
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

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  transcript_session_id TEXT NOT NULL,
  org_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL,
  scopes_json TEXT NOT NULL,
  cwd TEXT NOT NULL,
  runtime_type TEXT NOT NULL,
  execution_engine TEXT NOT NULL DEFAULT 'scode',
  execution_profile_id TEXT,
  docker_image TEXT,
  docker_mode TEXT,
  config_dir TEXT,
  container_name TEXT,
  status TEXT NOT NULL,
  desired_state TEXT NOT NULL,
  current_attempt_id TEXT,
  transcript_path TEXT NOT NULL,
  title TEXT,
  summary TEXT,
  assistant_name TEXT,
  source TEXT,
  channel_chat_id TEXT,
  client_metadata TEXT,
  created_at BIGINT NOT NULL,
  last_active_at BIGINT NOT NULL,
  ended_at BIGINT,
  deleted_at BIGINT,
  -- P1a/H2：session 的 home zone 归属与 observed 对账持久化列。
  home_zone_id TEXT,
  home_zone_observed_at TEXT,
  home_zone_observed_revision TEXT,
  home_zone_sync_error TEXT
);

CREATE TABLE IF NOT EXISTS session_attempts (
  attempt_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  generation BIGINT NOT NULL,
  backend_type TEXT NOT NULL,
  runtime_state TEXT NOT NULL,
  server_instance_id TEXT,
  runner_pid BIGINT,
  container_name TEXT,
  attach_path TEXT,
  resume_transcript_session_id TEXT NOT NULL,
  -- P1a (§8.10 R5.2)：runner generation 的 execution zone。本表仍是
  -- runtime generation（R5.9）；真正 Attempt.execution_zone_id 属 P1b。
  execution_zone_id TEXT,
  started_at BIGINT NOT NULL,
  last_heartbeat_at BIGINT,
  stopped_at BIGINT,
  exit_code BIGINT,
  exit_signal TEXT,
  stop_reason TEXT,
  error_text TEXT,
  -- M-5：终态 attempt 的 Nexus run 收尾标记（endZoneRunsForSettledAttempts）。
  nexus_run_ended_at BIGINT,
  UNIQUE (session_id, generation)
);

CREATE TABLE IF NOT EXISTS server_instances (
  instance_id TEXT PRIMARY KEY,
  host TEXT NOT NULL,
  pid BIGINT,
  started_at BIGINT NOT NULL,
  heartbeat_at BIGINT NOT NULL,
  stopped_at BIGINT,
  status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_events (
  event_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  attempt_id TEXT,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at BIGINT NOT NULL
);



  CREATE TABLE IF NOT EXISTS cohost_sessions (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    owner_user_id TEXT NOT NULL,
    native_agent_id TEXT NOT NULL,
    durable_session_id TEXT NOT NULL,
    repository_path TEXT NOT NULL,
    UNIQUE (owner_user_id, durable_session_id)
  );
