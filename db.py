"""
Database layer — supports both PostgreSQL (Render) and SQLite (local dev).
Set DATABASE_URL env var for PostgreSQL, otherwise falls back to SQLite.
"""
import os, json
from flask import g

DATABASE_URL = os.getenv('DATABASE_URL', '')

# Render gives postgres:// but psycopg2 needs postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3

SQLITE_PATH = os.path.join(os.path.dirname(__file__), 'betpawa.db')

# ── Connection ─────────────────────────────────────────────────────────────
def get_db():
    if 'db' not in g:
        if USE_PG:
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = False
            g.db = conn
        else:
            conn = sqlite3.connect(SQLITE_PATH)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            g.db = conn
    return g.db

def close_db(e=None):
    db = g.pop('db', None)
    if db:
        try: db.close()
        except: pass

# ── PG row wrapper to mimic sqlite3.Row dict-style access ─────────────────
class PGRow(dict):
    """Wraps a psycopg2 dict-cursor row so templates can use row['col']."""
    def __getitem__(self, key):
        return super().__getitem__(key)

def _wrap(rows):
    if rows is None:
        return None
    if isinstance(rows, list):
        return [PGRow(r) for r in rows]
    return PGRow(rows)

# ── Query helpers ──────────────────────────────────────────────────────────
def _adapt_sql(sql):
    """Convert SQLite ? placeholders to PostgreSQL %s."""
    if USE_PG:
        return sql.replace('?', '%s')
    return sql

def query(sql, params=(), one=False):
    sql = _adapt_sql(sql)
    db  = get_db()
    if USE_PG:
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rv = cur.fetchall()
        cur.close()
        result = [PGRow(r) for r in rv]
        return (result[0] if result else None) if one else result
    else:
        cur = db.execute(sql, params)
        rv  = cur.fetchall()
        return (rv[0] if rv else None) if one else rv

def execute(sql, params=()):
    """Execute a write statement and return the last inserted id."""
    sql = _adapt_sql(sql)
    db  = get_db()
    if USE_PG:
        # Add RETURNING id for INSERT statements
        if sql.strip().upper().startswith('INSERT'):
            if 'RETURNING' not in sql.upper():
                sql = sql.rstrip('; ') + ' RETURNING id'
        cur = db.cursor()
        cur.execute(sql, params)
        last_id = None
        if sql.strip().upper().startswith('INSERT') and 'RETURNING' in sql.upper():
            row = cur.fetchone()
            last_id = row[0] if row else None
        db.commit()
        cur.close()
        return last_id
    else:
        cur = db.execute(sql, params)
        db.commit()
        return cur.lastrowid

# ── Schema ─────────────────────────────────────────────────────────────────
SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    phone TEXT UNIQUE,
    password_hash TEXT NOT NULL,
    balance REAL DEFAULT 0.0,
    role TEXT DEFAULT 'user',
    created_at TIMESTAMP DEFAULT NOW(),
    is_active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS transactions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    type TEXT NOT NULL,
    amount REAL NOT NULL,
    status TEXT DEFAULT 'pending',
    reference TEXT,
    note TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS matchdays (
    id SERIAL PRIMARY KEY,
    matchday_number INTEGER NOT NULL,
    league TEXT NOT NULL,
    starts_at TIMESTAMP NOT NULL,
    status TEXT DEFAULT 'upcoming',
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS matches (
    id SERIAL PRIMARY KEY,
    matchday_id INTEGER NOT NULL REFERENCES matchdays(id),
    home_code TEXT NOT NULL,
    away_code TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    league TEXT NOT NULL,
    home_score INTEGER DEFAULT 0,
    away_score INTEGER DEFAULT 0,
    ht_home INTEGER DEFAULT 0,
    ht_away INTEGER DEFAULT 0,
    status TEXT DEFAULT 'upcoming',
    current_minute INTEGER DEFAULT 0,
    kickoff_time TIMESTAMP,
    preset_home INTEGER DEFAULT NULL,
    preset_away INTEGER DEFAULT NULL,
    odds_json TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS match_events (
    id SERIAL PRIMARY KEY,
    match_id INTEGER NOT NULL REFERENCES matches(id),
    minute INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    description TEXT,
    team TEXT,
    is_home INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS bets (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    total_stake REAL NOT NULL,
    potential_win REAL NOT NULL,
    status TEXT DEFAULT 'pending',
    placed_at TIMESTAMP DEFAULT NOW(),
    settled_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bet_selections (
    id SERIAL PRIMARY KEY,
    bet_id INTEGER NOT NULL REFERENCES bets(id),
    match_id INTEGER NOT NULL REFERENCES matches(id),
    market TEXT NOT NULL,
    selection TEXT NOT NULL,
    odds REAL NOT NULL,
    result TEXT DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS admin_logs (
    id SERIAL PRIMARY KEY,
    admin_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    details TEXT,
    timestamp TIMESTAMP DEFAULT NOW()
);
"""

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    phone TEXT UNIQUE,
    password_hash TEXT NOT NULL,
    balance REAL DEFAULT 0.0,
    role TEXT DEFAULT 'user',
    created_at TEXT DEFAULT (datetime('now')),
    is_active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    amount REAL NOT NULL,
    status TEXT DEFAULT 'pending',
    reference TEXT,
    note TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS matchdays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    matchday_number INTEGER NOT NULL,
    league TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    status TEXT DEFAULT 'upcoming',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    matchday_id INTEGER NOT NULL,
    home_code TEXT NOT NULL,
    away_code TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    league TEXT NOT NULL,
    home_score INTEGER DEFAULT 0,
    away_score INTEGER DEFAULT 0,
    ht_home INTEGER DEFAULT 0,
    ht_away INTEGER DEFAULT 0,
    status TEXT DEFAULT 'upcoming',
    current_minute INTEGER DEFAULT 0,
    kickoff_time TEXT,
    preset_home INTEGER DEFAULT NULL,
    preset_away INTEGER DEFAULT NULL,
    odds_json TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(matchday_id) REFERENCES matchdays(id)
);
CREATE TABLE IF NOT EXISTS match_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL,
    minute INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    description TEXT,
    team TEXT,
    is_home INTEGER DEFAULT 1,
    FOREIGN KEY(match_id) REFERENCES matches(id)
);
CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    total_stake REAL NOT NULL,
    potential_win REAL NOT NULL,
    status TEXT DEFAULT 'pending',
    placed_at TEXT DEFAULT (datetime('now')),
    settled_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS bet_selections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_id INTEGER NOT NULL,
    match_id INTEGER NOT NULL,
    market TEXT NOT NULL,
    selection TEXT NOT NULL,
    odds REAL NOT NULL,
    result TEXT DEFAULT 'pending',
    FOREIGN KEY(bet_id) REFERENCES bets(id),
    FOREIGN KEY(match_id) REFERENCES matches(id)
);
CREATE TABLE IF NOT EXISTS admin_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER,
    action TEXT NOT NULL,
    details TEXT,
    timestamp TEXT DEFAULT (datetime('now'))
);
"""

def init_db(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        db = get_db()
        if USE_PG:
            cur = db.cursor()
            cur.execute(SCHEMA_PG)
            db.commit()
            cur.close()
        else:
            db.executescript(SCHEMA_SQLITE)
            db.commit()
