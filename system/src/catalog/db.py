"""SQLite store for the work index.

Rows are never deleted: a file that disappears becomes `missing`, and a changed
output keeps its previous version as `superseded`. Every change is also written
to `events`, so "what did we do with this video" stays answerable after the
source or an output is gone. The database can be rebuilt from the work folders
for everything that still exists; only the history of removed items is unique
to it, so it lives with the private work data, not in Git.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from workspace_paths import ROOT

INDEX_DIR = Path("工作資料/索引")
DB_NAME = "catalog.sqlite3"
MARKDOWN_NAME = "工作索引.md"

SCHEMA = """
create table if not exists meta(key text primary key, value text);

create table if not exists sources(
  path text primary key, name text not null, bytes integer, mtime text,
  status text not null, first_seen text not null, last_seen text not null);

create table if not exists runs(
  run_id text primary key, workflow text, run_dir text not null, source_path text,
  status text, primary_path text, primary_sha256 text, updated_at text,
  state text not null, first_seen text not null, last_seen text not null);

create table if not exists tasks(
  kind text not null, run_id text not null, folder text not null, status text,
  output text, primary_sha256 text, updated_at text, parts text,
  state text not null, first_seen text not null, last_seen text not null,
  primary key(kind, run_id));

create table if not exists artifacts(
  id integer primary key, kind text not null, path text not null, run_id text,
  sha256 text not null, bytes integer, status text not null,
  first_seen text not null, last_seen text not null,
  unique(path, sha256));

create table if not exists events(
  id integer primary key, at text not null, kind text not null, key text not null,
  event text not null, detail text);

create virtual table if not exists search using fts5(path, kind, run_id, body, tokenize='trigram');

create table if not exists documents(
  path text primary key, kind text not null, run_id text, sha256 text not null);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path(root=ROOT):
    return Path(root) / INDEX_DIR / DB_NAME


def connect(root=ROOT):
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    _migrate(connection)
    return connection


def _migrate(connection):
    """Columns added after a database was first created."""
    columns = {row["name"] for row in connection.execute("pragma table_info(tasks)")}
    if "parts" not in columns:
        connection.execute("alter table tasks add column parts text")


def log(connection, kind, key, event, **detail):
    connection.execute("insert into events(at, kind, key, event, detail) values(?,?,?,?,?)",
                       (now(), kind, key, event, json.dumps(detail, ensure_ascii=False) if detail else None))


def rows(connection, query, *params):
    return [dict(r) for r in connection.execute(query, params).fetchall()]


def one(connection, query, *params):
    row = connection.execute(query, params).fetchone()
    return dict(row) if row else None


def upsert(connection, table, key_fields, values, kind, event_key, watch=()):
    """Insert or update one row, recording an event when a watched field changes."""
    existing = one(connection, f"select * from {table} where "
                   + " and ".join(f"{f}=?" for f in key_fields), *[values[f] for f in key_fields])
    stamp = now()
    if existing is None:
        values = {**values, "first_seen": stamp, "last_seen": stamp}
        columns = ", ".join(values)
        connection.execute(f"insert into {table}({columns}) values({', '.join('?' * len(values))})",
                           tuple(values.values()))
        log(connection, kind, event_key, "added", **{f: values.get(f) for f in watch})
        return "added"
    changes = {f: values[f] for f in watch if f in values and values[f] != existing.get(f)}
    assignments = ", ".join(f"{f}=?" for f in values if f not in key_fields)
    connection.execute(f"update {table} set {assignments}, last_seen=? where "
                       + " and ".join(f"{f}=?" for f in key_fields),
                       tuple(values[f] for f in values if f not in key_fields) + (stamp,)
                       + tuple(values[f] for f in key_fields))
    if changes:
        log(connection, kind, event_key, "changed", **{f: [existing.get(f), v] for f, v in changes.items()})
        return "changed"
    return "seen"


def mark_missing(connection, table, kind, key_field, seen, state_field="status", missing="missing"):
    """Anything not seen in this scan keeps its row and becomes missing."""
    gone = []
    for row in rows(connection, f"select * from {table} where {state_field} != ?", missing):
        key = row[key_field]
        if key in seen:
            continue
        connection.execute(f"update {table} set {state_field}=? where {key_field}=?", (missing, key))
        log(connection, kind, str(key), "missing", previous=row[state_field])
        gone.append(key)
    return gone
