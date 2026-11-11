-- D1 schema for the community price bot.
-- Reports are append-only: nothing is updated or deleted when a newer report
-- arrives. "Current" prices are derived at read time from the last 72 hours.

CREATE TABLE IF NOT EXISTS reporters (
  id          TEXT PRIMARY KEY,           -- HMAC of the Telegram user id; not reversible without the salt
  first_seen  TEXT NOT NULL,              -- ISO 8601 UTC
  last_seen   TEXT NOT NULL,
  accepted    INTEGER NOT NULL DEFAULT 0,
  rejected    INTEGER NOT NULL DEFAULT 0,
  blocked     INTEGER NOT NULL DEFAULT 0  -- set to 1 by hand to silence an abuser
);

CREATE TABLE IF NOT EXISTS reports (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  reporter_id      TEXT NOT NULL REFERENCES reporters(id),
  area             TEXT NOT NULL,         -- pilot area id, e.g. "port-harcourt"
  state            TEXT NOT NULL,         -- state slug used by the static API, e.g. "rivers"
  brand            TEXT NOT NULL,         -- brand id, e.g. "nnpc" or "other-xyz"
  brand_label      TEXT NOT NULL,
  brand_category   TEXT NOT NULL,         -- nnpc | major | independent
  fuel             TEXT NOT NULL,         -- pms | ago
  price            REAL NOT NULL,         -- NGN per litre
  status           TEXT NOT NULL,         -- accepted | rejected
  reject_reason    TEXT,
  checked_against  TEXT,                  -- recent_reports | official_average | bounds_only
  baseline_price   REAL,                  -- NBS state average used for the check, if any
  baseline_period  TEXT,                  -- "YYYY-MM"
  created_at       TEXT NOT NULL          -- ISO 8601 UTC
);

CREATE INDEX IF NOT EXISTS reports_area_time ON reports (area, fuel, status, created_at);
CREATE INDEX IF NOT EXISTS reports_status_time ON reports (status, created_at);
CREATE INDEX IF NOT EXISTS reports_reporter_time ON reports (reporter_id, created_at);
