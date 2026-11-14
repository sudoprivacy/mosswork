-- Public schema only. Moss103 d0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d reviewStoreSchema.ts.
CREATE TABLE IF NOT EXISTS ontology_review_values (
  org_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value_json TEXT NOT NULL,
  PRIMARY KEY (org_id, key)
);
