"""The single authoritative operational store (existing state.sqlite).

Migration rules:
* Additive only. Legacy tables keep their exact column lists because the
  deployed helper inserts positionally (e.g. ``INSERT INTO publications
  VALUES(?,?,?,?,?)``); rollback to the old helper must keep working.
* New state lives in ``ops_*`` tables.
* Short ``BEGIN IMMEDIATE`` transactions only; no network or media work while a
  transaction is open.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 1

LEGACY = '''
CREATE TABLE IF NOT EXISTS locks (name TEXT PRIMARY KEY, owner TEXT, until REAL);
CREATE TABLE IF NOT EXISTS reservations (item TEXT PRIMARY KEY, format TEXT,
  style TEXT, at TEXT, committed INTEGER DEFAULT 0, UNIQUE(format,at));
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, result TEXT);
CREATE TABLE IF NOT EXISTS media (id TEXT PRIMARY KEY, item TEXT, path TEXT,
  source TEXT, metadata TEXT);
CREATE TABLE IF NOT EXISTS publications (item TEXT PRIMARY KEY, owner TEXT,
  stage TEXT, data TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS assets (identity TEXT PRIMARY KEY, item TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS health (name TEXT PRIMARY KEY, updated REAL, data TEXT);
CREATE TABLE IF NOT EXISTS item_state (item TEXT PRIMARY KEY, format TEXT, asset TEXT);
CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY, item TEXT, kind TEXT, detail TEXT, created REAL);
'''

OPS_V1 = '''
CREATE TABLE IF NOT EXISTS ops_meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS ops_items (
  item_id TEXT PRIMARY KEY,
  name TEXT, code TEXT, source_item_id TEXT,
  format TEXT,                       -- authorized format (owner-controlled after import)
  version INTEGER NOT NULL DEFAULT 1,     -- bumps on every committed change (approval binding)
  content_rev INTEGER NOT NULL DEFAULT 1, -- bumps when published payload inputs change
  caption TEXT, caption_state TEXT,  -- approved | pending_approval | missing | legacy_unapproved
  caption_origin TEXT,               -- human | approved_draft | legacy
  collab TEXT, variety TEXT, notes TEXT,
  folder_url TEXT, source_override_url TEXT,
  asset_key TEXT, file_id TEXT, file_rev TEXT, content_hash TEXT, file_name TEXT, file_url TEXT,
  topaz_asset TEXT,                  -- asset key the human Topaz confirmation is bound to
  readiness TEXT NOT NULL DEFAULT 'unchecked',  -- unchecked|checking|blocked|ready
  block_kind TEXT,                   -- content|editor|config|review|infra
  block_reason TEXT, block_key TEXT,
  verification_id TEXT,              -- media.id of the verified prepared file
  owner_state TEXT NOT NULL DEFAULT 'active',   -- active|paused|skipped
  owner_state_reason TEXT,
  hold TEXT,                         -- protective hold awaiting owner (json)
  requested_at TEXT,                 -- requested slot (UTC iso), distinct from reservation
  requested_by TEXT,
  publication TEXT NOT NULL DEFAULT 'not_started', -- not_started|in_progress|published|outcome_unknown|failed
  legacy_posted INTEGER NOT NULL DEFAULT 0,
  infra_issue TEXT,
  observed TEXT,                     -- last observed human-editable Monday values (json)
  projected TEXT,                    -- last display projection confirmed written (json)
  pending_projection TEXT,           -- queued projection not yet confirmed (json)
  waiting_since REAL,
  created REAL, updated REAL
);

CREATE TABLE IF NOT EXISTS ops_reservations (
  item_id TEXT PRIMARY KEY REFERENCES ops_items(item_id),
  account TEXT NOT NULL, format TEXT NOT NULL, slot TEXT NOT NULL,
  content_rev INTEGER NOT NULL, payload_fp TEXT NOT NULL,
  origin TEXT NOT NULL,              -- auto|owner|repair|legacy
  owner_pinned INTEGER NOT NULL DEFAULT 0,
  created REAL, updated REAL,
  UNIQUE(account, format, slot)
);

CREATE TABLE IF NOT EXISTS ops_commands (
  id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, op TEXT NOT NULL, item_id TEXT,
  expected_version INTEGER, actor TEXT NOT NULL, actor_kind TEXT NOT NULL, auth_ref TEXT,
  state TEXT NOT NULL, result TEXT, created REAL, updated REAL
);

CREATE TABLE IF NOT EXISTS ops_proposals (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, bindings TEXT NOT NULL, payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL, summary TEXT, created_by TEXT, thread TEXT,
  state TEXT NOT NULL, expires REAL NOT NULL, decided_by TEXT, result TEXT,
  created REAL, updated REAL
);

CREATE TABLE IF NOT EXISTS ops_attempts (
  id TEXT PRIMARY KEY, item_id TEXT NOT NULL, content_rev INTEGER NOT NULL,
  payload_fp TEXT NOT NULL, payload TEXT NOT NULL, slot TEXT NOT NULL,
  worker TEXT, fence INTEGER NOT NULL, lease_until REAL,
  stage TEXT NOT NULL,   -- claimed|container_created|committed|published|outcome_unknown|failed|abandoned
  container_id TEXT, media_id TEXT, permalink TEXT, evidence TEXT,
  next_check REAL, checks INTEGER NOT NULL DEFAULT 0,
  created REAL, updated REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ops_attempts_one_active ON ops_attempts(item_id)
  WHERE stage IN ('claimed','container_created','committed','outcome_unknown');

CREATE TABLE IF NOT EXISTS ops_outbox (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE,
  item_id TEXT, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0, last_error TEXT,
  lease_owner TEXT, lease_until REAL, created REAL, updated REAL
);

CREATE TABLE IF NOT EXISTS ops_findings (
  fingerprint TEXT PRIMARY KEY, item_id TEXT, kind TEXT, detail TEXT,
  first_seen REAL, last_seen REAL, notified INTEGER NOT NULL DEFAULT 0, resolved REAL
);

CREATE TABLE IF NOT EXISTS ops_runs (
  kind TEXT PRIMARY KEY, run_id TEXT, fence INTEGER NOT NULL DEFAULT 0,
  lease_until REAL, started REAL, heartbeat REAL, last_result TEXT
);

CREATE TABLE IF NOT EXISTS ops_heartbeat (name TEXT PRIMARY KEY, at REAL, detail TEXT);

CREATE TABLE IF NOT EXISTS ops_editor_tasks (
  item_id TEXT NOT NULL, issue_key TEXT NOT NULL, task_id TEXT, body_hash TEXT,
  state TEXT NOT NULL DEFAULT 'pending', next_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
  updated REAL, PRIMARY KEY(item_id, issue_key)
);

CREATE TABLE IF NOT EXISTS ops_caption_drafts (
  input_hash TEXT PRIMARY KEY, item_id TEXT NOT NULL, text TEXT, model TEXT,
  state TEXT NOT NULL,  -- pending_approval|approved|rejected|invalid|superseded
  reason TEXT, proposal_id TEXT, created REAL, updated REAL
);

CREATE TABLE IF NOT EXISTS ops_source_map (
  source_item_id TEXT PRIMARY KEY, social_item_id TEXT UNIQUE, state TEXT NOT NULL, created REAL
);

CREATE TABLE IF NOT EXISTS ops_checks (
  item_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, requested REAL,
  requested_by TEXT, state TEXT NOT NULL, result TEXT, updated REAL,
  PRIMARY KEY(item_id, kind)
);

CREATE TABLE IF NOT EXISTS ops_audit (
  id INTEGER PRIMARY KEY, at REAL, item_id TEXT, kind TEXT, actor TEXT, detail TEXT
);

CREATE TABLE IF NOT EXISTS ops_counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
'''


def dumps(x) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def loads(s, default=None):
    if s is None or s == '':
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


class Store:
    """Connection factory. Each call site opens a short-lived connection."""

    def __init__(self, path: str | os.PathLike, *, create: bool = True):
        self.path = Path(path)
        self.create = create

    def connect(self) -> sqlite3.Connection:
        if not self.create and not self.path.is_file():
            raise FileNotFoundError(f'Operational database not found: {self.path}')
        if self.create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA busy_timeout=30000')
        c.execute('PRAGMA foreign_keys=ON')
        return c

    @contextlib.contextmanager
    def tx(self):
        """BEGIN IMMEDIATE ... COMMIT. Serialises writers; keep it short."""
        c = self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.execute('COMMIT')
        except BaseException:
            try:
                c.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()

    @contextlib.contextmanager
    def read(self):
        c = self.connect()
        try:
            yield c
        finally:
            c.close()

    def migrate(self) -> int:
        c = self.connect()
        try:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript(LEGACY)
            c.executescript(OPS_V1)
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT value FROM ops_meta WHERE key='schema_version'").fetchone()
            current = int(row[0]) if row else 0
            if current > SCHEMA_VERSION:
                c.execute('ROLLBACK')
                raise RuntimeError(f'Database schema {current} is newer than this code ({SCHEMA_VERSION}); refusing to run')
            if current < SCHEMA_VERSION:
                c.execute("INSERT OR REPLACE INTO ops_meta VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
                c.execute("INSERT OR REPLACE INTO ops_meta VALUES('migrated_at',?)", (str(time.time()),))
            c.execute('COMMIT')
            return SCHEMA_VERSION
        finally:
            c.close()

    def schema_version(self) -> int:
        with self.read() as c:
            try:
                row = c.execute("SELECT value FROM ops_meta WHERE key='schema_version'").fetchone()
            except sqlite3.OperationalError:
                return 0
            return int(row[0]) if row else 0


def next_counter(c: sqlite3.Connection, name: str) -> int:
    c.execute('INSERT INTO ops_counters(name,value) VALUES(?,1) '
              'ON CONFLICT(name) DO UPDATE SET value=value+1', (name,))
    return c.execute('SELECT value FROM ops_counters WHERE name=?', (name,)).fetchone()[0]


def audit(c: sqlite3.Connection, item_id, kind, actor, detail):
    c.execute('INSERT INTO ops_audit(at,item_id,kind,actor,detail) VALUES(?,?,?,?,?)',
              (time.time(), None if item_id is None else str(item_id), kind, actor, dumps(detail)))
