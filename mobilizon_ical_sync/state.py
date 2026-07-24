"""SQLite-backed state: app credentials, OAuth tokens, event mappings."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS app (
    instance_url    TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL,
    client_secret   TEXT NOT NULL,
    scope           TEXT NOT NULL,
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS token (
    instance_url        TEXT PRIMARY KEY,
    access_token        TEXT NOT NULL,
    refresh_token       TEXT NOT NULL,
    access_expires_at   INTEGER NOT NULL,
    refresh_expires_at  INTEGER NOT NULL,
    scope               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_map (
    org_id        TEXT NOT NULL,
    ical_uid      TEXT NOT NULL,
    event_uuid    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    updated_at    INTEGER NOT NULL,
    PRIMARY KEY (org_id, ical_uid)
);

CREATE TABLE IF NOT EXISTS media_cache (
    org_id      TEXT NOT NULL,
    filename    TEXT NOT NULL,
    media_uuid  TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (org_id, filename)
);
"""


@dataclass(frozen=True)
class AppCredentials:
    client_id: str
    client_secret: str
    scope: str


@dataclass(frozen=True)
class TokenBundle:
    access_token: str
    refresh_token: str
    access_expires_at: int
    refresh_expires_at: int
    scope: str

    def access_expired(self, skew_seconds: int = 60) -> bool:
        return time.time() + skew_seconds >= self.access_expires_at

    def refresh_expired(self, skew_seconds: int = 60) -> bool:
        return time.time() + skew_seconds >= self.refresh_expires_at


@dataclass(frozen=True)
class EventMapping:
    ical_uid: str
    event_uuid: str
    content_hash: str


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # --- app credentials -------------------------------------------------
    def get_app(self, instance_url: str) -> AppCredentials | None:
        row = self._conn.execute(
            "SELECT client_id, client_secret, scope FROM app WHERE instance_url = ?",
            (instance_url,),
        ).fetchone()
        if row is None:
            return None
        return AppCredentials(client_id=row[0], client_secret=row[1], scope=row[2])

    def save_app(self, instance_url: str, creds: AppCredentials) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO app (instance_url, client_id, client_secret, scope, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (instance_url, creds.client_id, creds.client_secret, creds.scope, int(time.time())),
            )

    # --- tokens ----------------------------------------------------------
    def get_token(self, instance_url: str) -> TokenBundle | None:
        row = self._conn.execute(
            "SELECT access_token, refresh_token, access_expires_at, refresh_expires_at, scope "
            "FROM token WHERE instance_url = ?",
            (instance_url,),
        ).fetchone()
        if row is None:
            return None
        return TokenBundle(*row)

    def save_token(self, instance_url: str, t: TokenBundle) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO token "
                "(instance_url, access_token, refresh_token, access_expires_at, refresh_expires_at, scope) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    instance_url,
                    t.access_token,
                    t.refresh_token,
                    t.access_expires_at,
                    t.refresh_expires_at,
                    t.scope,
                ),
            )

    def delete_token(self, instance_url: str) -> None:
        with self._tx() as c:
            c.execute(
                "DELETE FROM token WHERE instance_url = ?",
                (instance_url,),
            )

    # --- event mappings --------------------------------------------------
    def get_mappings(self, org_id: str) -> dict[str, EventMapping]:
        rows = self._conn.execute(
            "SELECT ical_uid, event_uuid, content_hash FROM event_map WHERE org_id = ?",
            (org_id,),
        ).fetchall()
        return {r[0]: EventMapping(ical_uid=r[0], event_uuid=r[1], content_hash=r[2]) for r in rows}

    def upsert_mapping(self, org_id: str, mapping: EventMapping) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO event_map "
                "(org_id, ical_uid, event_uuid, content_hash, updated_at) VALUES (?, ?, ?, ?, ?)",
                (org_id, mapping.ical_uid, mapping.event_uuid, mapping.content_hash, int(time.time())),
            )

    def delete_mapping(self, org_id: str, ical_uid: str) -> None:
        with self._tx() as c:
            c.execute(
                "DELETE FROM event_map WHERE org_id = ? AND ical_uid = ?",
                (org_id, ical_uid),
            )

    # --- media cache -----------------------------------------------------
    def get_media_uuid(self, org_id: str, filename: str) -> str | None:
        row = self._conn.execute(
            "SELECT media_uuid FROM media_cache WHERE org_id = ? AND filename = ?",
            (org_id, filename),
        ).fetchone()
        return row[0] if row else None

    def save_media_uuid(self, org_id: str, filename: str, media_uuid: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO media_cache "
                "(org_id, filename, media_uuid, created_at) VALUES (?, ?, ?, ?)",
                (org_id, filename, media_uuid, int(time.time())),
            )

    def delete_media_uuid(self, org_id: str, filename: str) -> None:
        with self._tx() as c:
            c.execute(
                "DELETE FROM media_cache WHERE org_id = ? AND filename = ?",
                (org_id, filename),
            )
