"""Bondok's own conversation store (bondok.sqlite): events, thread history, kv.
Operational state does not live here; it lives in the shared handler store."""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        with self.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,payload TEXT,state TEXT,reply TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,thread TEXT,role TEXT,text TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,thread TEXT,actor TEXT,body TEXT,before_hash TEXT,state TEXT,result TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,kind TEXT,detail TEXT,created REAL);
            ''')

    @contextlib.contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def kv(self, key, value=None):
        with self.db() as c:
            if value is not None:
                c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (key, json.dumps(value)))
                return value
            r = c.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
            return json.loads(r[0]) if r else None

    def history(self, thread, limit=12):
        with self.db() as c:
            rows = c.execute('SELECT role,text FROM messages WHERE thread=? ORDER BY id DESC LIMIT ?', (thread, limit)).fetchall()
        return [{'role': r['role'], 'content': r['text']} for r in reversed(rows)]

    def remember(self, thread, role, text):
        with self.db() as c:
            c.execute('INSERT INTO messages(thread,role,text,created) VALUES(?,?,?,?)', (thread, role, text[:8000], time.time()))

    def enqueue_event(self, key, event) -> bool:
        """Slack retries and duplicate deliveries collapse here, before any model call."""
        with self.db() as c:
            cur = c.execute('INSERT OR IGNORE INTO events(id,payload,state,reply,created) VALUES(?,?,?,?,?)',
                            (key, json.dumps(event), 'queued', None, time.time()))
            return cur.rowcount == 1

    def recover_working(self):
        with self.db() as c:
            c.execute("UPDATE events SET state='reply_ready', reply=? WHERE state='working'",
                      ('The service restarted during this request. Check the result before repeating any approval; '
                       'I will not repeat an unconfirmed change.',))

    def next_event(self):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            r = c.execute("SELECT * FROM events WHERE state IN ('queued','reply_ready') ORDER BY created LIMIT 1").fetchone()
            if r and r['state'] == 'queued':
                c.execute("UPDATE events SET state='working' WHERE id=?", (r['id'],))
            return dict(r) if r else None

    def set_event(self, key, state, reply=None):
        with self.db() as c:
            if reply is None:
                c.execute('UPDATE events SET state=? WHERE id=?', (state, key))
            else:
                c.execute('UPDATE events SET state=?, reply=? WHERE id=?', (state, reply, key))
