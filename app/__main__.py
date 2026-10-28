import asyncio
import contextlib
import hmac
import logging
import random
import signal
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramAPIError,
    TelegramConflictError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent, TelegramObject
from aiogram.utils.backoff import BackoffConfig

from app import texts
from app.config import Config, load_config
from app.db import Database
from app.handlers import complaint, secret_admin, start, submit
from app.logging_conf import setup_logging
from app.services.admin_notifications import send_due_admin_notification_digests
from app.services.cleanup import expire_secret_sessions
from app.services.moderation_ai import ModerationEngine
from app.services.secret_panel import SecretPanel

logger = logging.getLogger(__name__)

CLEANUP_INTERVAL_SECONDS = 20
NOTIFICATION_DIGEST_INTERVAL_SECONDS = 5
CONNECT_MIN_DELAY_SECONDS = 5.0
CONNECT_MAX_DELAY_SECONDS = 60.0
POLLING_TIMEOUT_SECONDS = 30
POLLING_CONCURRENCY_LIMIT = 20
POLLING_BACKOFF = BackoffConfig(
    min_delay=6.0,
    max_delay=60.0,
    factor=1.7,
    jitter=0.3,
)
SHUTDOWN_TASK_WAIT_SECONDS = 5.0


async def _sleep_or_stop(stop_event: asyncio.Event, delay_seconds: float) -> bool:
    if stop_event.is_set():
        return True
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_seconds)
    except TimeoutError:
        return False
    return True


def _retry_after_delay(exc: TelegramRetryAfter) -> float:
    return max(1.0, float(exc.retry_after)) + random.uniform(0.5, 1.5)


async def _connect_telegram(bot: Bot, stop_event: asyncio.Event) -> bool:
    delay = CONNECT_MIN_DELAY_SECONDS
    attempt = 0
    while not stop_event.is_set():
        attempt += 1
        try:
            me = await bot.me()
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("Telegram API доступна: @%s (id=%s)", me.username, me.id)
            return True
        except TelegramUnauthorizedError:
            logger.error(
                "Telegram отклонил токен бота. Проверьте BOT_TOKEN "
                "в переменных окружения Bothost."
            )
            raise
        except TelegramRetryAfter as exc:
            wait = _retry_after_delay(exc)
            logger.warning(
                "Telegram flood control при запуске (попытка %s). "
                "Повтор через %.1f с: %s",
                attempt,
                wait,
                exc,
            )
        except (ClientDecodeError, TelegramNetworkError, TelegramServerError) as exc:
            wait = delay + random.uniform(0.0, 1.0)
            delay = min(delay * 2, CONNECT_MAX_DELAY_SECONDS)
            logger.warning(
                "Telegram API временно недоступна (попытка %s). "
                "Повтор через %.1f с: %s",
                attempt,
                wait,
                exc,
            )
        except TelegramAPIError as exc:
            wait = delay + random.uniform(0.0, 1.0)
            delay = min(delay * 2, CONNECT_MAX_DELAY_SECONDS)
            logger.warning(
                "Ошибка Telegram API при запуске (попытка %s). "
                "Повтор через %.1f с: %s",
                attempt,
                wait,
                exc,
            )
        if await _sleep_or_stop(stop_event, wait):
            return False
    return False


async def _cleanup_loop(bot: Bot, db: Database, config: Config) -> None:
    while True:
        try:
            await expire_secret_sessions(bot, db, config)
        except Exception:
            logger.exception("ошибка в фоновой очистке секретных сессий")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


async def _admin_notification_loop(
    bot: Bot, db: Database, config: Config
) -> None:
    while True:
        try:
            await send_due_admin_notification_digests(bot, db, config)
        except Exception:
            logger.exception("ошибка в фоновой рассылке сводок администраторам")
        await asyncio.sleep(NOTIFICATION_DIGEST_INTERVAL_SECONDS)


async def _run_polling(
    dp: Dispatcher,
    bot: Bot,
    stop_event: asyncio.Event,
    workflow_data: dict[str, Any],
) -> None:
    delay = CONNECT_MIN_DELAY_SECONDS
    while not stop_event.is_set():
        try:
            await dp.start_polling(
                bot,
                polling_timeout=POLLING_TIMEOUT_SECONDS,
                backoff_config=POLLING_BACKOFF,
                handle_signals=False,
                close_bot_session=False,
                tasks_concurrency_limit=POLLING_CONCURRENCY_LIMIT,
                **workflow_data,
            )
            return
        except TelegramUnauthorizedError:
            logger.error(
                "Telegram отклонил токен бота. Проверьте BOT_TOKEN "
                "в переменных окружения Bothost."
            )
            raise
        except TelegramRetryAfter as exc:
            wait = _retry_after_delay(exc)
            logger.warning(
                "Polling остановлен из-за Telegram flood control. "
                "Перезапуск через %.1f с: %s",
                wait,
                exc,
            )
        except TelegramConflictError as exc:
            wait = delay + random.uniform(0.0, 1.0)
            delay = min(delay * 2, CONNECT_MAX_DELAY_SECONDS)
            logger.error(
                "Polling остановлен из-за конфликта getUpdates. Проверьте, "
                "что с этим токеном не запущен второй экземпляр бота. "
                "Перезапуск через %.1f с: %s",
                wait,
                exc,
            )
        except (ClientDecodeError, TelegramNetworkError, TelegramServerError) as exc:
            wait = delay + random.uniform(0.0, 1.0)
            delay = min(delay * 2, CONNECT_MAX_DELAY_SECONDS)
            logger.warning(
                "Polling остановлен из-за временной ошибки Telegram. "
                "Перезапуск через %.1f с: %s",
                wait,
                exc,
            )
        if await _sleep_or_stop(stop_event, wait):
            return


async def _stop_polling_quietly(dp: Dispatcher) -> None:
    with contextlib.suppress(RuntimeError):
        await dp.stop_polling()


async def _wait_for_handler_tasks(
    ignored: set[asyncio.Task[Any]],
) -> None:
    current = asyncio.current_task()
    pending = {
        task
        for task in asyncio.all_tasks()
        if task is not current and task not in ignored and not task.done()
    }
    if not pending:
        return
    _done, still_pending = await asyncio.wait(
        pending, timeout=SHUTDOWN_TASK_WAIT_SECONDS
    )
    if still_pending:
        logger.warning(
            "При остановке не завершились %s фоновых задач из %s",
            len(still_pending),
            len(pending),
        )


def _install_signal_handlers(
    dp: Dispatcher,
    stop_event: asyncio.Event,
    stop_tasks: set[asyncio.Task[None]],
) -> bool:
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        if stop_event.is_set():
            return
        stop_event.set()
        logger.warning("Получен сигнал остановки")
        task = asyncio.create_task(_stop_polling_quietly(dp))
        stop_tasks.add(task)

        def finish_stop(finished: asyncio.Task[None]) -> None:
            stop_tasks.discard(finished)
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                logger.error(
                    "Задача остановки polling завершилась с ошибкой",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(finish_stop)

    installed = False
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, request_stop)
            installed = True
    return installed


def _remove_signal_handlers(installed: bool) -> None:
    if not installed:
        return
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.remove_signal_handler(sig)


class _RestrictedUserMiddleware(BaseMiddleware):
    def __init__(self, db: Database, config: Config) -> None:
        super().__init__()
        self._db = db
        self._config = config

    def _is_secret_panel_entry(
        self, event: TelegramObject, user_id: int
    ) -> bool:
        if self._config.admin_ids and user_id not in self._config.admin_ids:
            return False
        callback_data = getattr(event, "data", None)
        if isinstance(callback_data, str) and callback_data.startswith(
            "sadmin:"
        ):
            return True
        text = getattr(event, "text", None)
        chat = getattr(event, "chat", None)
        chat_type_value = getattr(chat, "type", None)
        chat_type = getattr(chat_type_value, "value", chat_type_value)
        if not isinstance(text, str) or chat_type != "private":
            return False
        if getattr(chat, "id", None) != user_id:
            return False
        candidate = text.strip().casefold().encode("utf-8")
        expected = self._config.secret_word.strip().casefold().encode("utf-8")
        return hmac.compare_digest(candidate, expected)

    async def _is_strike_appeal_entry(
        self, event: TelegramObject, user_id: int
    ) -> bool:
        callback_data = getattr(event, "data", None)
        if isinstance(callback_data, str) and callback_data.startswith(
            "strike_appeal:"
        ):
            return True
        chat = getattr(event, "chat", None)
        chat_type_value = getattr(chat, "type", None)
        chat_type = getattr(chat_type_value, "value", chat_type_value)
        if chat_type != "private" or getattr(chat, "id", None) != user_id:
            return False
        text = getattr(event, "text", None)
        if not isinstance(text, str):
            return False
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return False
        command = parts[0].casefold()
        is_appeal_command = command == "/appeal" or command.startswith(
            "/appeal@"
        )
        if self._db.is_strike_appeal_draft_user_sync(user_id):
            if command.startswith("/") and not is_appeal_command:
                await self._db.end_strike_appeal_draft(user_id)
                return False
            return True
        return is_appeal_command

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user") or getattr(event, "from_user", None)
        user_id = getattr(user, "id", None)
        if (
            isinstance(user_id, int)
            and user_id not in self._config.admin_ids
            and self._db.is_user_restricted_sync(user_id)
            and not self._is_secret_panel_entry(event, user_id)
            and not await self._is_strike_appeal_entry(event, user_id)
        ):
            return None
        return await handler(event, data)


async def main() -> None:
    config = load_config()
    setup_logging(config.log_path)

    db = Database(config.db_path)
    bot: Bot | None = None
    cleanup_task: asyncio.Task[None] | None = None
    notification_task: asyncio.Task[None] | None = None
    stop_event = asyncio.Event()
    stop_tasks: set[asyncio.Task[None]] = set()
    signals_installed = False

    try:
        await db.connect()
        bot = Bot(
            token=config.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        storage = MemoryStorage()
        dp = Dispatcher(storage=storage)
        restricted_middleware = _RestrictedUserMiddleware(db, config)
        dp.message.outer_middleware(restricted_middleware)
        dp.callback_query.outer_middleware(restricted_middleware)

        @dp.errors()
        async def on_error(event: ErrorEvent) -> bool:
            incident_code = uuid.uuid4().hex[:6]
            logger.exception(
                "Ошибка обработки update_id=%s code=%s",
                event.update.update_id,
                incident_code,
                exc_info=(
                    type(event.exception),
                    event.exception,
                    event.exception.__traceback__,
                ),
            )
            try:
                if event.update.callback_query is not None:
                    await event.update.callback_query.answer(
                        texts.ERROR_GENERIC_PLAIN.format(code=incident_code),
                        show_alert=True,
                    )
                elif event.update.message is not None:
                    await event.update.message.answer(
                        texts.ERROR_GENERIC.format(code=incident_code)
                    )
            except Exception:
                logger.debug(
                    "не удалось отправить сообщение об ошибке code=%s",
                    incident_code,
                    exc_info=True,
                )
            return True

        dp.include_router(secret_admin.router)
        dp.include_router(start.router)
        dp.include_router(submit.router)
        dp.include_router(complaint.router)

        secret_panel = SecretPanel(bot, db, config.secret_session_minutes)
        if not config.admin_ids:
            logger.warning(
                "ADMIN_IDS не задан; секретная панель защищена только SECRET_WORD"
            )
        moderation = ModerationEngine()
        signals_installed = _install_signal_handlers(dp, stop_event, stop_tasks)

        if not await _connect_telegram(bot, stop_event):
            return
        cleanup_task = asyncio.create_task(_cleanup_loop(bot, db, config))
        notification_task = asyncio.create_task(
            _admin_notification_loop(bot, db, config)
        )
        logger.info("Бот запущен, long polling")
        await _run_polling(
            dp,
            bot,
            stop_event,
            {
                "db": db,
                "config": config,
                "moderation": moderation,
                "secret_panel": secret_panel,
                "fsm_storage": storage,
            },
        )
    finally:
        _remove_signal_handlers(signals_installed)
        ignored_tasks: set[asyncio.Task[Any]] = set(stop_tasks)
        if cleanup_task is not None:
            ignored_tasks.add(cleanup_task)
        if notification_task is not None:
            ignored_tasks.add(notification_task)
        await _wait_for_handler_tasks(ignored_tasks)
        for task in list(stop_tasks):
            task.cancel()
        if stop_tasks:
            await asyncio.gather(*list(stop_tasks), return_exceptions=True)
        if notification_task is not None:
            notification_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await notification_task
        if cleanup_task is not None:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task
        await db.close()
        if bot is not None:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
