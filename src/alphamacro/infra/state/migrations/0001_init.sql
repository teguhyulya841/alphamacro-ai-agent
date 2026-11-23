CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv_cache (
    key TEXT PRIMARY KEY,
    cache_class TEXT NOT NULL,
    value_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    source TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    expires_at_epoch REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kv_cache_expires ON kv_cache(expires_at_epoch);

CREATE TABLE IF NOT EXISTS rate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket_key TEXT NOT NULL,
    ts_epoch REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rate_events_bucket_ts ON rate_events(bucket_key, ts_epoch);

CREATE TABLE IF NOT EXISTS leases (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at_epoch REAL NOT NULL
);