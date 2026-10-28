import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import ClientDecodeError, TelegramAPIError
from aiogram.types import InlineKeyboardMarkup

from app.db import Database, now_iso

logger = logging.getLogger(__name__)

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024


def _limit_text(text: str, limit: int) -> str:
    if len(text) <= limit and all(ord(ch) <= 0xFFFF for ch in text):
        return text
    units = 0
    end = 0
    for index, ch in enumerate(text[: limit - 1]):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit - 1:
            break
        end = index + 1
    cut = text[:end]
    last_amp = cut.rfind("&")
    last_semicolon = cut.rfind(";")
    if last_amp > last_semicolon:
        cut = cut[:last_amp]
    last_open = cut.rfind("<")
    last_close = cut.rfind(">")
    if last_open > last_close:
        cut = cut[:last_open]
    return f"{cut.rstrip()}…"


class SecretPanel:
    def __init__(self, bot: Bot, db: Database, session_minutes: int = 10):
        self._bot = bot
        self._db = db
        self._session_minutes = session_minutes
        self._lock = asyncio.Lock()

    async def _previous_message_id(self, chat_id: int) -> int | None:
        async with await self._db.conn.execute(
            "SELECT message_id FROM secret_sessions WHERE chat_id = ?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
        return row["message_id"] if row else None

    async def _delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            await self._bot.delete_message(chat_id, message_id)
        except (ClientDecodeError, TelegramAPIError):
            logger.debug(
                "не удалось удалить предыдущее сообщение админки", exc_info=True
            )

    async def _delete_previous(self, chat_id: int) -> None:
        message_id = await self._previous_message_id(chat_id)
        if message_id is not None:
            await self._delete_message(chat_id, message_id)

    async def _track(
        self, chat_id: int, message_id: int, context: dict[str, Any] | None
    ) -> None:
        async with await self._db.conn.execute(
            "INSERT INTO secret_sessions (chat_id, message_id, context_data, last_activity) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET message_id = excluded.message_id, "
            "context_data = excluded.context_data, last_activity = excluded.last_activity",
            (chat_id, message_id, json.dumps(context or {}), now_iso()),
        ):
            pass
        await self._db.conn.commit()

    async def show_text(
        self,
        chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        safe_text = _limit_text(text, TEXT_LIMIT)
        async with self._lock:
            previous_message_id = await self._previous_message_id(chat_id)
            message = await self._bot.send_message(
                chat_id, safe_text, reply_markup=reply_markup
            )
            await self._track(chat_id, message.message_id, context)
            if previous_message_id is not None and previous_message_id != message.message_id:
                await self._delete_message(chat_id, previous_message_id)

    async def show_media(
        self,
        chat_id: int,
        kind: str,
        file_id: str,
        caption: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        if kind not in {"photo", "video"}:
            raise ValueError(f"Неподдерживаемый тип медиа: {kind}")
        if not file_id:
            raise ValueError("Не задан file_id медиа")
        safe_caption = _limit_text(caption, CAPTION_LIMIT) if caption else None
        async with self._lock:
            previous_message_id = await self._previous_message_id(chat_id)
            if kind == "photo":
                message = await self._bot.send_photo(
                    chat_id, file_id, caption=safe_caption, reply_markup=reply_markup
                )
            else:
                message = await self._bot.send_video(
                    chat_id, file_id, caption=safe_caption, reply_markup=reply_markup
                )
            await self._track(chat_id, message.message_id, context)
            if previous_message_id is not None and previous_message_id != message.message_id:
                await self._delete_message(chat_id, previous_message_id)

    async def touch(self, chat_id: int) -> None:
        async with await self._db.conn.execute(
            "UPDATE secret_sessions SET last_activity = ? WHERE chat_id = ?",
            (now_iso(), chat_id),
        ):
            pass
        await self._db.conn.commit()

    async def close(self, chat_id: int) -> None:
        async with self._lock:
            await self._delete_previous(chat_id)
            async with await self._db.conn.execute(
                "DELETE FROM secret_sessions WHERE chat_id = ?", (chat_id,)
            ):
                pass
            await self._db.conn.commit()

    async def is_current_message(self, chat_id: int, message_id: int) -> bool:
        async with await self._db.conn.execute(
            "SELECT message_id, last_activity FROM secret_sessions WHERE chat_id = ?",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row or row["message_id"] != message_id:
            return False
        try:
            last_activity = datetime.fromisoformat(row["last_activity"])
        except (TypeError, ValueError):
            logger.warning("Повреждён last_activity секретной сессии chat_id=%s", chat_id)
            return False
        if last_activity.tzinfo is None:
            last_activity = last_activity.replace(tzinfo=UTC)
        cutoff = datetime.now(UTC) - timedelta(minutes=self._session_minutes)
        return last_activity >= cutoff

    async def get_context(self, chat_id: int) -> dict[str, Any]:
        async with await self._db.conn.execute(
            "SELECT context_data FROM secret_sessions WHERE chat_id = ?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return {}
        try:
            parsed = json.loads(row["context_data"])
        except (TypeError, ValueError):
            logger.warning("Повреждён context_data секретной сессии chat_id=%s", chat_id)
            return {}
        return parsed if isinstance(parsed, dict) else {}
