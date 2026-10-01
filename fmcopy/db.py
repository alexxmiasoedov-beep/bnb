import os
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS anchors(block INTEGER PRIMARY KEY, ts INTEGER);
CREATE TABLE IF NOT EXISTS tokens(
  token TEXT PRIMARY KEY, creator TEXT, symbol TEXT, create_block INTEGER,
  quote TEXT, max_funds REAL, migrate_block INTEGER, pair TEXT);
CREATE TABLE IF NOT EXISTS trades(
  block INTEGER, idx INTEGER, token TEXT, account TEXT, side INTEGER,
  price REAL, amount REAL, cost REAL, offers REAL, funds REAL,
  PRIMARY KEY(block, idx));
CREATE INDEX IF NOT EXISTS trades_token ON trades(token, block, idx);
CREATE INDEX IF NOT EXISTS trades_acc ON trades(account);
-- после выхода на PancakeSwap: резервы пары и переводы держателей
CREATE TABLE IF NOT EXISTS syncs(block INTEGER, idx INTEGER, token TEXT,
  r_token REAL, r_bnb REAL, PRIMARY KEY(block, idx));
CREATE INDEX IF NOT EXISTS syncs_token ON syncs(token, block, idx);
CREATE TABLE IF NOT EXISTS transfers(block INTEGER, idx INTEGER, token TEXT,
  src TEXT, dst TEXT, amount REAL, PRIMARY KEY(block, idx));
CREATE INDEX IF NOT EXISTS transfers_token ON transfers(token, block, idx);
CREATE TABLE IF NOT EXISTS postmig_done(token TEXT PRIMARY KEY, to_block INTEGER);
"""


def connect(path=None):
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def meta_get(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
    con.commit()
