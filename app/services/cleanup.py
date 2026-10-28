import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import ClientDecodeError, TelegramAPIError

from app.config import Config
from app.db import Database

logger = logging.getLogger(__name__)

_pending_deletions: set[asyncio.Task[None]] = set()


def _finish_delete(task: asyncio.Task[None]) -> None:
    _pending_deletions.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "Фоновая задача удаления сообщения админки завершилась с ошибкой",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def _schedule_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    task = asyncio.create_task(_delete_after_delay(bot, chat_id, message_id))
    _pending_deletions.add(task)
    task.add_done_callback(_finish_delete)


async def expire_secret_sessions(bot: Bot, db: Database, config: Config) -> None:
    cutoff = datetime.now(UTC) - timedelta(minutes=config.secret_session_minutes)
    cutoff_iso = cutoff.isoformat()
    async with await db.conn.execute(
        "SELECT chat_id, message_id, last_activity FROM secret_sessions "
        "WHERE last_activity < ?",
        (cutoff_iso,),
    ) as cur:
        expired = await cur.fetchall()
    for row in expired:
        chat_id = row["chat_id"]
        message_id = row["message_id"]
        last_activity = row["last_activity"]
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text="Время вышло."
            )
        except (ClientDecodeError, TelegramAPIError):
            logger.debug(
                "не удалось отредактировать сообщение админки при истечении",
                exc_info=True,
            )
        async with await db.conn.execute(
            "DELETE FROM secret_sessions "
            "WHERE chat_id = ? AND message_id = ? AND last_activity = ? "
            "AND last_activity < ?",
            (chat_id, message_id, last_activity, cutoff_iso),
        ):
            pass
        await db.conn.commit()
        _schedule_delete(bot, chat_id, message_id)


async def _delete_after_delay(bot: Bot, chat_id: int, message_id: int, delay: float = 2.0) -> None:
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except (ClientDecodeError, TelegramAPIError):
        logger.debug("не удалось удалить истёкшее сообщение админки", exc_info=True)
