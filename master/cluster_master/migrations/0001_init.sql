-- Cluster Web master schema, Phase 2 (docs/PLAN.md 13, security.md 13.1).
-- Times are integer epoch milliseconds (canonical JSON refuses floats).

CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER PRIMARY KEY,
  applied_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
  id           TEXT PRIMARY KEY,                -- node name, e.g. rpi3-01
  board        TEXT NOT NULL,
  token_hash   TEXT,                            -- NULL once revoked
  labels       TEXT NOT NULL DEFAULT '{}',      -- JSON, admin-set, authoritative (security.md 8.3)
  capacity     TEXT NOT NULL DEFAULT '{}',      -- JSON {slots, bpu_slots, job_mem_mb}
  static_info  TEXT,                            -- JSON, last reported by the agent (not trusted)
  sched_state  TEXT NOT NULL DEFAULT 'active',  -- active | cordoned | draining | drained
  sched_reason TEXT,
  registered_ip TEXT,
  last_seen_ms INTEGER,
  last_ip      TEXT,
  created_ms   INTEGER NOT NULL,
  created_by   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_tokens (
  id          INTEGER PRIMARY KEY,
  principal   TEXT NOT NULL,                    -- telegram-bot | ai-operator
  token_hash  TEXT NOT NULL UNIQUE,
  scopes      TEXT NOT NULL DEFAULT '[]',
  created_ms  INTEGER NOT NULL,
  revoked_ms  INTEGER
);

CREATE TABLE IF NOT EXISTS alerts (
  id          INTEGER PRIMARY KEY,
  node_id     TEXT,
  kind        TEXT NOT NULL,
  level       TEXT NOT NULL,                    -- warning | critical
  message     TEXT NOT NULL,
  started_ms  INTEGER NOT NULL,
  resolved_ms INTEGER
);
CREATE INDEX IF NOT EXISTS alerts_open ON alerts (kind, node_id) WHERE resolved_ms IS NULL;

CREATE TABLE IF NOT EXISTS system_state (
  key       TEXT PRIMARY KEY,
  value     TEXT NOT NULL,
  changed_ms INTEGER NOT NULL,
  changed_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,                     -- JSON
  changed_ms INTEGER NOT NULL,
  changed_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics_1m (
  node_id   TEXT NOT NULL,
  ts_ms     INTEGER NOT NULL,
  cpu_avg   REAL, cpu_max REAL, mem_pct REAL, temp_avg REAL, temp_max REAL,
  disk_pct  REAL, net_rx INTEGER, net_tx INTEGER, bpu_avg REAL,
  extra     TEXT,
  PRIMARY KEY (node_id, ts_ms)
);

-- Append-only, hash-chained (security.md 13.1). Triggers stop application bugs; the chain
-- and the external copies detect an attacker with direct file access.
CREATE TABLE IF NOT EXISTS audit_log (
  id           INTEGER PRIMARY KEY,
  ts_ms        INTEGER NOT NULL,
  actor_type   TEXT NOT NULL,                   -- user | service | ai | node | system | cli
  actor_id     TEXT NOT NULL,
  on_behalf_of TEXT,
  channel      TEXT NOT NULL,                   -- web | telegram | ai | cli | agent | system
  action       TEXT NOT NULL,
  target       TEXT,
  detail       TEXT NOT NULL,                   -- canonical JSON, redacted
  ip           TEXT,
  approval_id  INTEGER,
  prev_hash    TEXT NOT NULL,
  hash         TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
