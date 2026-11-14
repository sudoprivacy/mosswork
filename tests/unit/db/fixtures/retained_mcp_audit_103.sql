-- Public schema only. Moss server-v0.1.103 d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d, mcp/db.ts.
CREATE TABLE IF NOT EXISTS mcp_audit_log (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL,
  mcp_server_id TEXT REFERENCES mcp_servers(id) ON DELETE SET NULL,
  mcp_server_name TEXT,
  session_id TEXT,
  user_id TEXT NOT NULL,
  user_name TEXT,
  action TEXT NOT NULL,
  tool_name TEXT,
  request_params_json TEXT,
  response_summary TEXT,
  status TEXT,
  error_message TEXT,
  ip_address TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_org_time ON mcp_audit_log(org_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_server ON mcp_audit_log(mcp_server_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_user ON mcp_audit_log(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_action ON mcp_audit_log(action, created_at DESC);
