import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from app import texts
from app.config import Config
from app.db import Database

logger = logging.getLogger(__name__)

DIGEST_QUIET_SECONDS = 60
DIGEST_MAX_WAIT_SECONDS = 295


async def queue_admin_notification(
    db: Database, config: Config, kind: str
) -> None:
    recipients = await db.list_notification_recipients(kind)
    if config.admin_ids:
        recipients = [
            recipient for recipient in recipients if recipient in config.admin_ids
        ]
    await db.queue_admin_notification_events(kind, recipients)


def _digest_text(counts: Counter[str]) -> str:
    lines = [texts.ADMIN_NOTIFICATION_DIGEST_TITLE]
    if counts["posts"]:
        lines.append(
            texts.ADMIN_NOTIFICATION_DIGEST_POSTS.format(count=counts["posts"])
        )
    if counts["complaints"]:
        lines.append(
            texts.ADMIN_NOTIFICATION_DIGEST_COMPLAINTS.format(
                count=counts["complaints"]
            )
        )
    if counts["strike_appeals"]:
        lines.append(
            texts.ADMIN_NOTIFICATION_DIGEST_STRIKE_APPEALS.format(
                count=counts["strike_appeals"]
            )
        )
    return "\n".join(lines)


async def send_due_admin_notification_digests(
    bot: Bot, db: Database, config: Config
) -> None:
    now = datetime.now(UTC)
    admin_ids = await db.list_due_admin_notification_admin_ids(
        now,
        now - timedelta(seconds=DIGEST_QUIET_SECONDS),
        now - timedelta(seconds=DIGEST_MAX_WAIT_SECONDS),
    )
    for admin_id in admin_ids:
        events = await db.list_pending_admin_notification_events(
            admin_id, due_before=now
        )
        if not events:
            continue
        event_ids = [int(event["id"]) for event in events]
        if config.admin_ids and admin_id not in config.admin_ids:
            await db.delete_admin_notification_events(event_ids)
            continue
        settings = await db.get_admin_notification_settings(admin_id)
        enabled_ids: list[int] = []
        disabled_ids: list[int] = []
        counts: Counter[str] = Counter()
        for event in events:
            kind = str(event["kind"])
            if settings.get(kind, False):
                enabled_ids.append(int(event["id"]))
                counts[kind] += 1
            else:
                disabled_ids.append(int(event["id"]))
        if disabled_ids:
            await db.delete_admin_notification_events(disabled_ids)
        if not enabled_ids:
            continue
        try:
            await bot.send_message(admin_id, _digest_text(counts))
        except (TelegramForbiddenError, TelegramBadRequest):
            await db.delete_admin_notification_events(enabled_ids)
            logger.debug(
                "сводка уведомлений администратору недоставляема",
                exc_info=True,
            )
            continue
        except TelegramRetryAfter as exc:
            retry_at = datetime.now(UTC) + timedelta(
                seconds=max(60, int(exc.retry_after))
            )
            await db.schedule_admin_notification_retry(enabled_ids, retry_at)
            continue
        except (ClientDecodeError, TelegramAPIError):
            attempts = max(
                int(event.get("attempts") or 0)
                for event in events
                if int(event["id"]) in enabled_ids
            )
            delay_seconds = 60 if attempts == 0 else 300 if attempts == 1 else 900
            retry_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
            await db.schedule_admin_notification_retry(enabled_ids, retry_at)
            logger.debug(
                "не удалось отправить сводку уведомлений администратору",
                exc_info=True,
            )
            continue
        await db.mark_admin_notification_events_sent(enabled_ids)
