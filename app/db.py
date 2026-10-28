import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_code TEXT UNIQUE,
    kind TEXT NOT NULL,
    text_content TEXT,
    file_id TEXT,
    submitter_id INTEGER NOT NULL,
    route TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    published_message_id INTEGER,
    complaints_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS complaints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_code TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(public_code, user_id)
);

CREATE TABLE IF NOT EXISTS user_access (
    user_id INTEGER PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    strikes INTEGER NOT NULL DEFAULT 0,
    restricted_until TEXT,
    restriction_permanent INTEGER NOT NULL DEFAULT 0,
    is_restricted INTEGER NOT NULL DEFAULT 0,
    last_reason TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submission_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    normalized_hash TEXT NOT NULL DEFAULT '',
    normalized_length INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS strike_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    queue_id INTEGER,
    created_at TEXT NOT NULL,
    cancelled_at TEXT
);

CREATE TABLE IF NOT EXISTS strike_appeals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    strike_event_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by INTEGER
);

CREATE TABLE IF NOT EXISTS strike_appeal_drafts (
    user_id INTEGER PRIMARY KEY,
    strike_event_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_notification_settings (
    admin_user_id INTEGER PRIMARY KEY,
    posts_enabled INTEGER NOT NULL DEFAULT 0,
    complaints_enabled INTEGER NOT NULL DEFAULT 0,
    strike_appeals_enabled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_notification_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT
);

CREATE TABLE IF NOT EXISTS secret_sessions (
    chat_id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    context_data TEXT NOT NULL DEFAULT '{}',
    last_activity TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_posts_review
    ON posts (route, status, queue_id);
CREATE INDEX IF NOT EXISTS idx_posts_complaints_count
    ON posts (complaints_count, queue_id);
CREATE INDEX IF NOT EXISTS idx_complaints_public_code
    ON complaints (public_code, id);
CREATE INDEX IF NOT EXISTS idx_secret_sessions_last_activity
    ON secret_sessions (last_activity);
CREATE INDEX IF NOT EXISTS idx_posts_submitter_created
    ON posts (submitter_id, created_at);
CREATE INDEX IF NOT EXISTS idx_complaints_user_created
    ON complaints (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_user_access_restricted
    ON user_access (is_restricted, restricted_until, user_id);
CREATE INDEX IF NOT EXISTS idx_submission_events_user_created
    ON submission_events (user_id, created_at, event_type);
CREATE INDEX IF NOT EXISTS idx_submission_events_created
    ON submission_events (created_at, user_id);
CREATE INDEX IF NOT EXISTS idx_submission_events_hash_created
    ON submission_events (normalized_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_strike_events_user_created
    ON strike_events (user_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_strike_events_queue_reason
    ON strike_events (queue_id, reason) WHERE queue_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_strike_appeals_pending_user
    ON strike_appeals (user_id) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS idx_strike_appeals_decided_strike
    ON strike_appeals (strike_event_id)
    WHERE status IN ('rejected', 'strike_cancelled', 'unblocked');
CREATE INDEX IF NOT EXISTS idx_strike_appeals_status_created
    ON strike_appeals (status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_strike_appeals_user_created
    ON strike_appeals (user_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_admin_notification_events_pending
    ON admin_notification_events (sent_at, admin_user_id, created_at, id);
"""

_DEFAULT_SETTINGS = {
    "moderation_mode": "manual",
}

_NOTIFICATION_COLUMNS = {
    "posts": "posts_enabled",
    "complaints": "complaints_enabled",
    "strike_appeals": "strike_appeals_enabled",
}

_APPEAL_DECIDED_STATUSES = ("rejected", "strike_cancelled", "unblocked")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path):
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._restricted_until: dict[int, datetime | None] = {}
        self._access_cache_loaded = False
        self._submission_lock = asyncio.Lock()
        self._strike_lock = asyncio.Lock()
        self._appeal_drafts: dict[int, int] = {}

    async def _execute_pragma(self, conn: aiosqlite.Connection, sql: str) -> None:
        async with await conn.execute(sql) as cursor:
            await cursor.fetchall()

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path, timeout=10.0)
        try:
            conn.row_factory = aiosqlite.Row
            await self._execute_pragma(conn, "PRAGMA journal_mode = WAL")
            await self._execute_pragma(conn, "PRAGMA busy_timeout = 5000")
            await self._execute_pragma(conn, "PRAGMA foreign_keys = ON")
            await self._execute_pragma(conn, "PRAGMA synchronous = NORMAL")
            cursor = await conn.executescript(SCHEMA)
            await cursor.close()
            await self._migrate_schema(conn)
            for key, value in _DEFAULT_SETTINGS.items():
                async with await conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                ):
                    pass
            await self._backfill_access_data(conn)
            await conn.commit()
        except Exception:
            await conn.close()
            raise
        self._conn = conn
        try:
            await self._refresh_restricted_cache()
            await self._close_pending_appeals_for_unrestricted_users()
            await self._refresh_appeal_draft_cache()
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        conn = self._conn
        self._conn = None
        self._restricted_until.clear()
        self._access_cache_loaded = False
        self._appeal_drafts.clear()
        if conn is not None:
            await conn.close()

    @property
    def submission_lock(self) -> asyncio.Lock:
        return self._submission_lock

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База данных не подключена")
        return self._conn

    @staticmethod
    async def _migrate_schema(conn: aiosqlite.Connection) -> None:
        async with await conn.execute("PRAGMA table_info(strike_events)") as cur:
            rows = await cur.fetchall()
        columns = {str(row["name"]) for row in rows}
        if "cancelled_at" not in columns:
            async with await conn.execute(
                "ALTER TABLE strike_events ADD COLUMN cancelled_at TEXT"
            ):
                pass
        async with await conn.execute(
            "PRAGMA table_info(admin_notification_events)"
        ) as cur:
            event_rows = await cur.fetchall()
        event_columns = {str(row["name"]) for row in event_rows}
        if event_columns and "attempts" not in event_columns:
            async with await conn.execute(
                "ALTER TABLE admin_notification_events "
                "ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
            ):
                pass
        if event_columns and "next_attempt_at" not in event_columns:
            async with await conn.execute(
                "ALTER TABLE admin_notification_events "
                "ADD COLUMN next_attempt_at TEXT"
            ):
                pass

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    async def _backfill_access_data(conn: aiosqlite.Connection) -> None:
        async with await conn.execute(
            "SELECT value FROM settings WHERE key = 'access_events_backfilled_v1'"
        ) as cur:
            if await cur.fetchone() is not None:
                return
        for sql in (
            """
            INSERT INTO user_access (
                user_id, first_seen_at, updated_at
            )
            SELECT submitter_id, MIN(created_at), MIN(created_at)
            FROM posts GROUP BY submitter_id
            ON CONFLICT(user_id) DO UPDATE SET
                first_seen_at = MIN(user_access.first_seen_at, excluded.first_seen_at)
            """,
            """
            INSERT INTO user_access (
                user_id, first_seen_at, updated_at
            )
            SELECT user_id, MIN(created_at), MIN(created_at)
            FROM complaints GROUP BY user_id
            ON CONFLICT(user_id) DO UPDATE SET
                first_seen_at = MIN(user_access.first_seen_at, excluded.first_seen_at)
            """,
            """
            INSERT INTO submission_events (user_id, event_type, created_at)
            SELECT submitter_id, 'post', created_at FROM posts
            """,
            """
            INSERT INTO submission_events (user_id, event_type, created_at)
            SELECT user_id, 'complaint', created_at FROM complaints
            """,
        ):
            async with await conn.execute(sql):
                pass
        async with await conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("access_events_backfilled_v1", "1"),
        ):
            pass

    async def _refresh_restricted_cache(self) -> None:
        now = datetime.now(UTC)
        active: dict[int, datetime | None] = {}
        expired: list[int] = []
        async with await self.conn.execute(
            "SELECT user_id, restricted_until, restriction_permanent "
            "FROM user_access WHERE is_restricted = 1"
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            user_id = int(row["user_id"])
            if int(row["restriction_permanent"] or 0):
                active[user_id] = None
                continue
            until = self._parse_datetime(row["restricted_until"])
            if until is not None and until > now:
                active[user_id] = until
            else:
                expired.append(user_id)
        if expired:
            placeholders = ", ".join("?" for _ in expired)
            async with await self.conn.execute(
                "UPDATE user_access SET is_restricted = 0, "
                "restricted_until = NULL, updated_at = ? "
                f"WHERE user_id IN ({placeholders})",
                (now.isoformat(), *expired),
            ):
                pass
            async with await self.conn.execute(
                "DELETE FROM strike_appeal_drafts "
                f"WHERE user_id IN ({placeholders})",
                expired,
            ):
                pass
            async with await self.conn.execute(
                "UPDATE strike_appeals SET status = 'unblocked', "
                "decided_at = ?, decided_by = NULL "
                f"WHERE status = 'pending' AND user_id IN ({placeholders})",
                (now.isoformat(), *expired),
            ):
                pass
            await self.conn.commit()
            for user_id in expired:
                self._appeal_drafts.pop(user_id, None)
        self._restricted_until = active
        self._access_cache_loaded = True

    def is_user_restricted_sync(self, user_id: int) -> bool:
        if not self._access_cache_loaded or user_id not in self._restricted_until:
            return False
        until = self._restricted_until[user_id]
        if until is not None and until <= datetime.now(UTC):
            del self._restricted_until[user_id]
            return False
        return True

    async def _refresh_appeal_draft_cache(self) -> None:
        async with await self.conn.execute(
            "SELECT user_id, strike_event_id FROM strike_appeal_drafts"
        ) as cur:
            rows = await cur.fetchall()
        active: dict[int, int] = {}
        stale: list[int] = []
        for row in rows:
            user_id = int(row["user_id"])
            if user_id in self._restricted_until:
                active[user_id] = int(row["strike_event_id"])
            else:
                stale.append(user_id)
        if stale:
            placeholders = ", ".join("?" for _ in stale)
            async with await self.conn.execute(
                "DELETE FROM strike_appeal_drafts "
                f"WHERE user_id IN ({placeholders})",
                stale,
            ):
                pass
            await self.conn.commit()
        self._appeal_drafts = active

    async def _delete_strike_appeal_draft_row(self, user_id: int) -> None:
        async with await self.conn.execute(
            "DELETE FROM strike_appeal_drafts WHERE user_id = ?", (user_id,)
        ):
            pass

    async def _close_pending_strike_appeal_row(
        self, user_id: int, admin_id: int | None, now: str
    ) -> bool:
        cursor = await self.conn.execute(
            "UPDATE strike_appeals SET status = 'unblocked', "
            "decided_at = ?, decided_by = ? "
            "WHERE user_id = ? AND status = 'pending'",
            (now, admin_id, user_id),
        )
        try:
            return bool(cursor.rowcount)
        finally:
            await cursor.close()

    async def _close_pending_appeals_for_unrestricted_users(self) -> None:
        async with await self.conn.execute(
            "UPDATE strike_appeals SET status = 'unblocked', "
            "decided_at = ?, decided_by = NULL "
            "WHERE status = 'pending' AND NOT EXISTS ("
            "SELECT 1 FROM user_access "
            "WHERE user_access.user_id = strike_appeals.user_id "
            "AND user_access.is_restricted = 1)",
            (now_iso(),),
        ):
            pass
        await self.conn.commit()

    async def begin_strike_appeal_draft(
        self, user_id: int, strike_event_id: int
    ) -> None:
        now = now_iso()
        async with await self.conn.execute(
            "INSERT INTO strike_appeal_drafts "
            "(user_id, strike_event_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "strike_event_id = excluded.strike_event_id, "
            "updated_at = excluded.updated_at",
            (user_id, strike_event_id, now, now),
        ):
            pass
        await self.conn.commit()
        self._appeal_drafts[user_id] = strike_event_id

    async def end_strike_appeal_draft(self, user_id: int) -> None:
        await self._delete_strike_appeal_draft_row(user_id)
        await self.conn.commit()
        self._appeal_drafts.pop(user_id, None)

    def get_strike_appeal_draft_sync(self, user_id: int) -> int | None:
        return self._appeal_drafts.get(user_id)

    def is_strike_appeal_draft_user_sync(self, user_id: int) -> bool:
        return user_id in self._appeal_drafts

    async def touch_user(self, user_id: int) -> dict[str, Any]:
        now = now_iso()
        async with await self.conn.execute(
            "INSERT OR IGNORE INTO user_access "
            "(user_id, first_seen_at, updated_at) VALUES (?, ?, ?)",
            (user_id, now, now),
        ):
            pass
        await self.conn.commit()
        access = await self.get_user_access(user_id)
        if access is None:
            raise RuntimeError("Не удалось создать запись доступа пользователя")
        return access

    async def get_user_access(self, user_id: int) -> dict[str, Any] | None:
        async with await self.conn.execute(
            "SELECT * FROM user_access WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        data = dict(row)
        if int(data.get("is_restricted") or 0) and not int(
            data.get("restriction_permanent") or 0
        ):
            until = self._parse_datetime(data.get("restricted_until"))
            if until is None or until <= datetime.now(UTC):
                now = now_iso()
                async with await self.conn.execute(
                    "UPDATE user_access SET is_restricted = 0, "
                    "restricted_until = NULL, updated_at = ? WHERE user_id = ?",
                    (now, user_id),
                ):
                    pass
                await self._delete_strike_appeal_draft_row(user_id)
                await self._close_pending_strike_appeal_row(
                    user_id, None, now
                )
                await self.conn.commit()
                data["is_restricted"] = 0
                data["restricted_until"] = None
                data["updated_at"] = now
                self._restricted_until.pop(user_id, None)
                self._appeal_drafts.pop(user_id, None)
        return data

    async def is_new_user(self, user_id: int) -> bool:
        access = await self.touch_user(user_id)
        first_seen = self._parse_datetime(access.get("first_seen_at"))
        if first_seen is None:
            return True
        return datetime.now(UTC) - first_seen < timedelta(hours=24)

    async def add_strike(
        self, user_id: int, reason: str, queue_id: int | None = None
    ) -> dict[str, Any] | None:
        async with self._strike_lock:
            return await self._add_strike_locked(user_id, reason, queue_id)

    async def _add_strike_locked(
        self, user_id: int, reason: str, queue_id: int | None = None
    ) -> dict[str, Any] | None:
        await self.touch_user(user_id)
        now = datetime.now(UTC)
        now_text = now.isoformat()
        try:
            async with await self.conn.execute(
                "INSERT INTO strike_events (user_id, reason, queue_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, reason, queue_id, now_text),
            ):
                pass
        except aiosqlite.IntegrityError:
            await self.conn.rollback()
            return None
        async with await self.conn.execute(
            "UPDATE user_access SET strikes = strikes + 1, last_reason = ?, "
            "updated_at = ? WHERE user_id = ?",
            (reason, now_text, user_id),
        ):
            pass
        async with await self.conn.execute(
            "SELECT strikes, restricted_until, restriction_permanent "
            "FROM user_access WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            await self.conn.rollback()
            raise RuntimeError("Запись доступа пользователя не найдена")
        strikes = int(row["strikes"])
        permanent = bool(int(row["restriction_permanent"] or 0)) or strikes >= 3
        if permanent:
            restricted_until: datetime | None = None
        else:
            duration = timedelta(hours=24) if strikes <= 1 else timedelta(days=7)
            candidate = now + duration
            existing_until = self._parse_datetime(row["restricted_until"])
            restricted_until = (
                existing_until
                if existing_until is not None and existing_until > candidate
                else candidate
            )
        async with await self.conn.execute(
            "UPDATE user_access SET is_restricted = 1, restricted_until = ?, "
            "restriction_permanent = ?, updated_at = ? WHERE user_id = ?",
            (
                restricted_until.isoformat() if restricted_until else None,
                1 if permanent else 0,
                now_text,
                user_id,
            ),
        ):
            pass
        await self._delete_strike_appeal_draft_row(user_id)
        await self.conn.commit()
        self._restricted_until[user_id] = restricted_until
        self._access_cache_loaded = True
        self._appeal_drafts.pop(user_id, None)
        return await self.get_user_access(user_id)

    async def remove_restriction(self, user_id: int) -> bool:
        removed, _appeal_closed = await self.remove_restriction_details(
            user_id
        )
        return removed

    async def remove_restriction_details(
        self, user_id: int, admin_id: int | None = None
    ) -> tuple[bool, bool]:
        async with self._strike_lock:
            return await self._remove_restriction_locked(user_id, admin_id)

    async def _remove_restriction_locked(
        self, user_id: int, admin_id: int | None = None
    ) -> tuple[bool, bool]:
        now = now_iso()
        cursor = await self.conn.execute(
            "UPDATE user_access SET is_restricted = 0, restricted_until = NULL, "
            "restriction_permanent = 0, updated_at = ? "
            "WHERE user_id = ? AND is_restricted = 1",
            (now, user_id),
        )
        try:
            changed = cursor.rowcount
        finally:
            await cursor.close()
        appeal_closed = False
        if changed:
            appeal_closed = await self._close_pending_strike_appeal_row(
                user_id, admin_id, now
            )
            await self._delete_strike_appeal_draft_row(user_id)
        await self.conn.commit()
        self._restricted_until.pop(user_id, None)
        if changed:
            self._appeal_drafts.pop(user_id, None)
        return bool(changed), appeal_closed

    async def list_restricted_users(
        self, limit: int = 20
    ) -> list[dict[str, Any]]:
        await self._refresh_restricted_cache()
        async with await self.conn.execute(
            "SELECT * FROM user_access WHERE is_restricted = 1 "
            "ORDER BY restriction_permanent DESC, restricted_until ASC, user_id ASC "
            "LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def get_latest_active_strike_event(
        self, user_id: int
    ) -> dict[str, Any] | None:
        async with await self.conn.execute(
            "SELECT * FROM strike_events WHERE user_id = ? "
            "AND cancelled_at IS NULL ORDER BY id DESC LIMIT 1",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_pending_strike_appeal_for_user(
        self, user_id: int
    ) -> dict[str, Any] | None:
        async with await self.conn.execute(
            "SELECT * FROM strike_appeals WHERE user_id = ? "
            "AND status = 'pending' ORDER BY id DESC LIMIT 1",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_strike_appeal(
        self, appeal_id: int
    ) -> dict[str, Any] | None:
        async with await self.conn.execute(
            "SELECT strike_appeals.*, strike_events.reason AS strike_reason, "
            "strike_events.created_at AS strike_created_at, "
            "strike_events.cancelled_at AS strike_cancelled_at "
            "FROM strike_appeals "
            "LEFT JOIN strike_events "
            "ON strike_events.id = strike_appeals.strike_event_id "
            "WHERE strike_appeals.id = ?",
            (appeal_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def has_decided_strike_appeal(self, strike_event_id: int) -> bool:
        placeholders = ", ".join("?" for _ in _APPEAL_DECIDED_STATUSES)
        async with await self.conn.execute(
            "SELECT 1 FROM strike_appeals WHERE strike_event_id = ? "
            f"AND status IN ({placeholders}) LIMIT 1",
            (strike_event_id, *_APPEAL_DECIDED_STATUSES),
        ) as cur:
            return await cur.fetchone() is not None

    async def create_strike_appeal(
        self, user_id: int, description: str
    ) -> dict[str, Any] | None:
        async with self._strike_lock:
            return await self._create_strike_appeal_locked(
                user_id, description
            )

    async def _create_strike_appeal_locked(
        self, user_id: int, description: str
    ) -> dict[str, Any] | None:
        access = await self.touch_user(user_id)
        if int(access.get("is_restricted") or 0) != 1:
            return None
        pending = await self.get_pending_strike_appeal_for_user(user_id)
        if pending is not None:
            return pending
        strike_event = await self.get_latest_active_strike_event(user_id)
        if strike_event is None:
            return None
        strike_event_id = int(strike_event["id"])
        if await self.has_decided_strike_appeal(strike_event_id):
            return None
        cursor = await self.conn.execute(
            "INSERT INTO strike_appeals "
            "(user_id, strike_event_id, description, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (user_id, strike_event_id, description, now_iso()),
        )
        try:
            appeal_id = int(cursor.lastrowid or 0)
        finally:
            await cursor.close()
        await self._delete_strike_appeal_draft_row(user_id)
        await self.conn.commit()
        self._appeal_drafts.pop(user_id, None)
        if appeal_id <= 0:
            return None
        return await self.get_strike_appeal(appeal_id)

    async def withdraw_strike_appeal(self, user_id: int) -> bool:
        async with self._strike_lock:
            cursor = await self.conn.execute(
                "UPDATE strike_appeals SET status = 'withdrawn', "
                "decided_at = ?, decided_by = ? "
                "WHERE user_id = ? AND status = 'pending'",
                (now_iso(), user_id, user_id),
            )
            try:
                changed = cursor.rowcount
            finally:
                await cursor.close()
            await self.conn.commit()
            return bool(changed)

    async def list_pending_strike_appeals(
        self, limit: int = 20
    ) -> list[dict[str, Any]]:
        async with await self.conn.execute(
            "SELECT * FROM strike_appeals WHERE status = 'pending' "
            "ORDER BY created_at ASC, id ASC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def count_pending_strike_appeals(self) -> int:
        async with await self.conn.execute(
            "SELECT COUNT(*) AS total FROM strike_appeals "
            "WHERE status = 'pending'"
        ) as cur:
            row = await cur.fetchone()
        return int(row["total"] if row else 0)

    async def get_next_pending_strike_appeal(
        self,
    ) -> dict[str, Any] | None:
        async with await self.conn.execute(
            "SELECT id FROM strike_appeals WHERE status = 'pending' "
            "ORDER BY created_at ASC, id ASC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return await self.get_strike_appeal(int(row["id"]))

    async def _recalculate_access_after_strike_cancellation(
        self, user_id: int, now: datetime
    ) -> None:
        async with await self.conn.execute(
            "SELECT reason, created_at FROM strike_events "
            "WHERE user_id = ? AND cancelled_at IS NULL ORDER BY id ASC",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
        strikes = len(rows)
        permanent = strikes >= 3
        restricted_until: datetime | None = None
        last_reason = None
        if rows:
            last_reason = rows[-1]["reason"]
            latest_at = self._parse_datetime(rows[-1]["created_at"]) or now
            if strikes == 1:
                restricted_until = latest_at + timedelta(hours=24)
            elif strikes == 2:
                restricted_until = latest_at + timedelta(days=7)
        is_restricted = bool(
            permanent
            or (restricted_until is not None and restricted_until > now)
        )
        if not is_restricted:
            permanent = False
            restricted_until = None
        async with await self.conn.execute(
            "UPDATE user_access SET strikes = ?, last_reason = ?, "
            "is_restricted = ?, restricted_until = ?, "
            "restriction_permanent = ?, updated_at = ? WHERE user_id = ?",
            (
                strikes,
                last_reason,
                1 if is_restricted else 0,
                restricted_until.isoformat() if restricted_until else None,
                1 if permanent else 0,
                now.isoformat(),
                user_id,
            ),
        ):
            pass
        if is_restricted:
            self._restricted_until[user_id] = restricted_until
        else:
            self._restricted_until.pop(user_id, None)
        self._access_cache_loaded = True

    async def decide_strike_appeal(
        self, appeal_id: int, admin_id: int, decision: str
    ) -> dict[str, Any] | None:
        async with self._strike_lock:
            return await self._decide_strike_appeal_locked(
                appeal_id, admin_id, decision
            )

    async def _decide_strike_appeal_locked(
        self, appeal_id: int, admin_id: int, decision: str
    ) -> dict[str, Any] | None:
        status_by_decision = {
            "reject": "rejected",
            "unblock": "unblocked",
            "cancel": "strike_cancelled",
        }
        status = status_by_decision.get(decision)
        if status is None:
            raise ValueError("Некорректное решение по апелляции")
        appeal = await self.get_strike_appeal(appeal_id)
        if appeal is None or appeal.get("status") != "pending":
            return None
        user_id = int(appeal["user_id"])
        strike_event_id = int(appeal["strike_event_id"])
        now = datetime.now(UTC)
        now_text = now.isoformat()
        async with await self.conn.execute(
            "UPDATE strike_appeals SET status = ?, decided_at = ?, "
            "decided_by = ? WHERE id = ? AND status = 'pending'",
            (status, now_text, admin_id, appeal_id),
        ):
            pass
        if decision == "unblock":
            async with await self.conn.execute(
                "UPDATE user_access SET is_restricted = 0, "
                "restricted_until = NULL, restriction_permanent = 0, "
                "updated_at = ? WHERE user_id = ?",
                (now_text, user_id),
            ):
                pass
            self._restricted_until.pop(user_id, None)
            await self._delete_strike_appeal_draft_row(user_id)
            self._appeal_drafts.pop(user_id, None)
        elif decision == "cancel":
            async with await self.conn.execute(
                "UPDATE strike_events SET cancelled_at = ? "
                "WHERE id = ? AND user_id = ? AND cancelled_at IS NULL",
                (now_text, strike_event_id, user_id),
            ):
                pass
            await self._recalculate_access_after_strike_cancellation(
                user_id, now
            )
            await self._delete_strike_appeal_draft_row(user_id)
            self._appeal_drafts.pop(user_id, None)
        await self.conn.commit()
        return await self.get_strike_appeal(appeal_id)

    @staticmethod
    def _notification_column(kind: str) -> str:
        column = _NOTIFICATION_COLUMNS.get(kind)
        if column is None:
            raise ValueError("Некорректный тип уведомления")
        return column

    async def get_admin_notification_settings(
        self, admin_user_id: int
    ) -> dict[str, bool]:
        async with await self.conn.execute(
            "SELECT posts_enabled, complaints_enabled, "
            "strike_appeals_enabled FROM admin_notification_settings "
            "WHERE admin_user_id = ?",
            (admin_user_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return {
                "posts": False,
                "complaints": False,
                "strike_appeals": False,
            }
        return {
            "posts": bool(int(row["posts_enabled"] or 0)),
            "complaints": bool(int(row["complaints_enabled"] or 0)),
            "strike_appeals": bool(
                int(row["strike_appeals_enabled"] or 0)
            ),
        }

    async def toggle_admin_notification(
        self, admin_user_id: int, kind: str
    ) -> dict[str, bool]:
        column = self._notification_column(kind)
        now = now_iso()
        async with await self.conn.execute(
            "INSERT OR IGNORE INTO admin_notification_settings "
            "(admin_user_id, updated_at) VALUES (?, ?)",
            (admin_user_id, now),
        ):
            pass
        async with await self.conn.execute(
            f"UPDATE admin_notification_settings SET {column} = 1 - {column}, "
            "updated_at = ? WHERE admin_user_id = ?",
            (now, admin_user_id),
        ):
            pass
        async with await self.conn.execute(
            "DELETE FROM admin_notification_events "
            "WHERE admin_user_id = ? AND kind = ? AND sent_at IS NULL "
            "AND NOT EXISTS ("
            "SELECT 1 FROM admin_notification_settings "
            f"WHERE admin_user_id = ? AND {column} = 1)",
            (admin_user_id, kind, admin_user_id),
        ):
            pass
        await self.conn.commit()
        return await self.get_admin_notification_settings(admin_user_id)

    async def list_notification_recipients(self, kind: str) -> list[int]:
        column = self._notification_column(kind)
        async with await self.conn.execute(
            "SELECT admin_user_id FROM admin_notification_settings "
            f"WHERE {column} = 1 ORDER BY admin_user_id ASC"
        ) as cur:
            rows = await cur.fetchall()
        return [int(row["admin_user_id"]) for row in rows]

    async def queue_admin_notification_events(
        self, kind: str, admin_user_ids: list[int]
    ) -> None:
        self._notification_column(kind)
        if not admin_user_ids:
            return
        now = now_iso()
        await self.conn.executemany(
            "INSERT INTO admin_notification_events "
            "(admin_user_id, kind, created_at) VALUES (?, ?, ?)",
            [(admin_id, kind, now) for admin_id in admin_user_ids],
        )
        await self.conn.commit()

    async def list_due_admin_notification_admin_ids(
        self, now: datetime, quiet_before: datetime, first_before: datetime
    ) -> list[int]:
        async with await self.conn.execute(
            "SELECT admin_user_id FROM admin_notification_events "
            "WHERE sent_at IS NULL "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "GROUP BY admin_user_id "
            "HAVING MAX(created_at) <= ? OR MIN(created_at) <= ? "
            "ORDER BY admin_user_id ASC",
            (
                now.astimezone(UTC).isoformat(),
                quiet_before.astimezone(UTC).isoformat(),
                first_before.astimezone(UTC).isoformat(),
            ),
        ) as cur:
            rows = await cur.fetchall()
        return [int(row["admin_user_id"]) for row in rows]

    async def list_pending_admin_notification_events(
        self, admin_user_id: int, due_before: datetime | None = None
    ) -> list[dict[str, Any]]:
        if due_before is None:
            async with await self.conn.execute(
                "SELECT id, kind, created_at, attempts "
                "FROM admin_notification_events "
                "WHERE admin_user_id = ? AND sent_at IS NULL ORDER BY id ASC",
                (admin_user_id,),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with await self.conn.execute(
                "SELECT id, kind, created_at, attempts "
                "FROM admin_notification_events "
                "WHERE admin_user_id = ? AND sent_at IS NULL "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                "ORDER BY id ASC",
                (admin_user_id, due_before.astimezone(UTC).isoformat()),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def mark_admin_notification_events_sent(
        self, event_ids: list[int]
    ) -> None:
        if not event_ids:
            return
        placeholders = ", ".join("?" for _ in event_ids)
        async with await self.conn.execute(
            "UPDATE admin_notification_events SET sent_at = ? "
            f"WHERE id IN ({placeholders}) AND sent_at IS NULL",
            (now_iso(), *event_ids),
        ):
            pass
        await self.conn.commit()

    async def schedule_admin_notification_retry(
        self, event_ids: list[int], next_attempt_at: datetime
    ) -> None:
        if not event_ids:
            return
        placeholders = ", ".join("?" for _ in event_ids)
        async with await self.conn.execute(
            "UPDATE admin_notification_events "
            "SET attempts = attempts + 1, next_attempt_at = ? "
            f"WHERE id IN ({placeholders}) AND sent_at IS NULL",
            (next_attempt_at.astimezone(UTC).isoformat(), *event_ids),
        ):
            pass
        await self.conn.commit()

    async def delete_admin_notification_events(
        self, event_ids: list[int]
    ) -> None:
        if not event_ids:
            return
        placeholders = ", ".join("?" for _ in event_ids)
        async with await self.conn.execute(
            "DELETE FROM admin_notification_events "
            f"WHERE id IN ({placeholders}) AND sent_at IS NULL",
            event_ids,
        ):
            pass
        await self.conn.commit()

    async def record_submission_event(
        self,
        user_id: int,
        event_type: str,
        normalized_hash: str = "",
        normalized_length: int = 0,
    ) -> None:
        async with await self.conn.execute(
            "INSERT INTO submission_events "
            "(user_id, event_type, created_at, normalized_hash, normalized_length) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                user_id,
                event_type,
                now_iso(),
                normalized_hash,
                normalized_length,
            ),
        ):
            pass
        await self.conn.commit()

    async def recent_submission_events(
        self, user_id: int, since: datetime
    ) -> list[dict[str, Any]]:
        async with await self.conn.execute(
            "SELECT event_type, created_at FROM submission_events "
            "WHERE user_id = ? AND created_at >= ? ORDER BY created_at DESC",
            (user_id, since.astimezone(UTC).isoformat()),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def count_recent_submission_events(self, since: datetime) -> int:
        async with await self.conn.execute(
            "SELECT COUNT(*) AS total FROM submission_events WHERE created_at >= ?",
            (since.astimezone(UTC).isoformat(),),
        ) as cur:
            row = await cur.fetchone()
        return int(row["total"] if row else 0)

    async def submission_fingerprint_exists(
        self, normalized_hash: str, since: datetime
    ) -> bool:
        if not normalized_hash:
            return False
        async with await self.conn.execute(
            "SELECT 1 FROM submission_events WHERE normalized_hash = ? "
            "AND created_at >= ? LIMIT 1",
            (normalized_hash, since.astimezone(UTC).isoformat()),
        ) as cur:
            return await cur.fetchone() is not None

    async def get_setting(self, key: str) -> str | None:
        async with await self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        async with await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        ):
            pass
        await self.conn.commit()
