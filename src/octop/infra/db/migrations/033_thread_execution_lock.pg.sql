ALTER TABLE threads ADD COLUMN IF NOT EXISTS execution_mode_locked INTEGER NOT NULL DEFAULT 0;
UPDATE _schema_version SET version = 33;
