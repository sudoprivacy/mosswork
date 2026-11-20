ALTER TABLE desktop_devices ADD COLUMN renewal_ticket TEXT;
ALTER TABLE desktop_devices ADD COLUMN ticket_deadline BIGINT NOT NULL DEFAULT 0;
ALTER TABLE desktop_devices ADD COLUMN renewal_until BIGINT NOT NULL DEFAULT 0;
ALTER TABLE desktop_devices ADD COLUMN prior_hash TEXT;
ALTER TABLE desktop_devices ADD COLUMN prior_until BIGINT NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS desktop_device_ticket ON desktop_devices(renewal_ticket);
CREATE UNIQUE INDEX IF NOT EXISTS desktop_device_prior ON desktop_devices(prior_hash);
UPDATE _schema_version SET version = 32;
