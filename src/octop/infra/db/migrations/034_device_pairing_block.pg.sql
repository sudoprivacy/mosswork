ALTER TABLE desktop_devices ADD COLUMN IF NOT EXISTS pairing_blocked INTEGER NOT NULL DEFAULT 0;
UPDATE _schema_version SET version = 34;
