-- Tabella config_snapshots
CREATE TABLE IF NOT EXISTS config_snapshots (
  id BIGSERIAL PRIMARY KEY,
  kind VARCHAR(10) NOT NULL CHECK (kind IN ('csv','yaml')),
  content TEXT NOT NULL,
  source_sha256 VARCHAR(64),
  created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  note VARCHAR(255),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_config_snapshots_kind_created_at ON config_snapshots(kind, created_at);

-- Tabella alert_rules
CREATE TABLE IF NOT EXISTS alert_rules (
  id BIGSERIAL PRIMARY KEY,
  name VARCHAR(120) NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  pool_filter VARCHAR(255),
  health_threshold_pct INTEGER NOT NULL DEFAULT 50,
  check_every_sec INTEGER NOT NULL DEFAULT 120,
  webhook_url VARCHAR(500),
  telegram_chat_id VARCHAR(64),
  notify_min_interval_sec INTEGER NOT NULL DEFAULT 900,
  last_notified_at TIMESTAMPTZ,
  created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);