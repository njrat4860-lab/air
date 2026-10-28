import hashlib
import logging
import math
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher

from aiogram import F, Router
from aiogram.exceptions import ClientDecodeError, TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import texts
from app.config import Config
from app.db import Database, now_iso
from app.ids import generate_unique_code
from app.keyboards.user import complaint_button, strike_appeal_keyboard
from app.services.admin_notifications import queue_admin_notification
from app.services.moderation_ai import ModerationEngine, Verdict
from app.services.profanity_words import is_spam_text
from app.states import SubmitStates

logger = logging.getLogger(__name__)
router = Router(name="submit")

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
SUBMISSION_INTERVAL_SECONDS = 30.0
SUBMISSION_HOURLY_LIMIT = 5
SUBMISSION_DAILY_LIMIT = 20
NEW_USER_INTERVAL_SECONDS = 120.0
NEW_USER_HOURLY_LIMIT = 2
NEW_USER_DAILY_LIMIT = 5
GLOBAL_SUBMISSION_WINDOW_SECONDS = 5 * 60
GLOBAL_SUBMISSION_THRESHOLD = 30
GLOBAL_SUBMISSION_PAUSE_SECONDS = 15 * 60
GLOBAL_QUEUE_THRESHOLD = 50
_RATE_WINDOW_SECONDS = 24 * 60 * 60
_RATE_LIMIT_CACHE_LIMIT = 1000
_last_unsupported_at: dict[int, float] = {}


def _is_admin(config: Config, user_id: int) -> bool:
    return user_id in config.admin_ids


def _parse_time(value: object) -> datetime | None:
    return Database._parse_datetime(value)


def _restriction_notice(access: dict[str, object] | None) -> str:
    if access is not None and access.get("restriction_permanent") == 1:
        return texts.ACCESS_RESTRICTED_UNTIL_ADMIN
    return texts.ACCESS_RESTRICTED_TEMPORARY


async def _strike_and_answer(
    message: Message,
    db: Database,
    config: Config,
    reason: str,
    base_text: str,
) -> None:
    if message.from_user is None or _is_admin(config, message.from_user.id):
        await message.answer(base_text)
        return
    access = await db.add_strike(message.from_user.id, reason)
    await message.answer(
        f"{base_text}\n{_restriction_notice(access)}",
        reply_markup=strike_appeal_keyboard(),
    )


async def _global_pause_until(db: Database) -> datetime | None:
    raw = await db.get_setting("global_submission_pause_until")
    until = _parse_time(raw)
    if until is not None and until > datetime.now(UTC):
        return until
    if raw:
        await db.set_setting("global_submission_pause_until", "")
    return None


async def _set_global_pause(db: Database) -> datetime:
    until = datetime.now(UTC) + timedelta(
        seconds=GLOBAL_SUBMISSION_PAUSE_SECONDS
    )
    await db.set_setting("global_submission_pause_until", until.isoformat())
    return until


async def _queue_size(db: Database) -> int:
    async with await db.conn.execute(
        "SELECT COUNT(*) AS total FROM posts "
        "WHERE route IN ('manual_queue', 'ai_manual_review') "
        "AND status IN ('pending', 'publishing')"
    ) as cur:
        row = await cur.fetchone()
    return int(row["total"] if row else 0)


async def _submission_guard(
    db: Database,
    user_id: int,
    event_type: str,
    normalized_hash: str,
    normalized_length: int,
    record_event: bool = True,
) -> tuple[str, int]:
    async with db.submission_lock:
        return await _submission_guard_locked(
            db,
            user_id,
            event_type,
            normalized_hash,
            normalized_length,
            record_event,
        )


async def _submission_guard_locked(
    db: Database,
    user_id: int,
    event_type: str,
    normalized_hash: str,
    normalized_length: int,
    record_event: bool = True,
) -> tuple[str, int]:
    access = await db.touch_user(user_id)
    if int(access.get("is_restricted") or 0):
        return "restricted", 0
    now = datetime.now(UTC)
    pause_until = await _global_pause_until(db)
    if pause_until is not None:
        minutes = max(1, math.ceil((pause_until - now).total_seconds() / 60))
        return "global", minutes
    if await _queue_size(db) >= GLOBAL_QUEUE_THRESHOLD:
        await _set_global_pause(db)
        return "global", GLOBAL_SUBMISSION_PAUSE_SECONDS // 60
    global_since = now - timedelta(
        seconds=GLOBAL_SUBMISSION_WINDOW_SECONDS
    )
    if (
        await db.count_recent_submission_events(global_since)
        >= GLOBAL_SUBMISSION_THRESHOLD
    ):
        await _set_global_pause(db)
        return "global", GLOBAL_SUBMISSION_PAUSE_SECONDS // 60
    events = await db.recent_submission_events(
        user_id, now - timedelta(seconds=_RATE_WINDOW_SECONDS)
    )
    times = [
        parsed
        for event in events
        if (parsed := _parse_time(event.get("created_at"))) is not None
    ]
    first_seen = _parse_time(access.get("first_seen_at"))
    is_new = first_seen is None or now - first_seen < timedelta(hours=24)
    interval = NEW_USER_INTERVAL_SECONDS if is_new else SUBMISSION_INTERVAL_SECONDS
    hourly_limit = NEW_USER_HOURLY_LIMIT if is_new else SUBMISSION_HOURLY_LIMIT
    daily_limit = NEW_USER_DAILY_LIMIT if is_new else SUBMISSION_DAILY_LIMIT
    if times and (now - times[0]).total_seconds() < interval:
        return "rate", 0
    hourly_count = sum(
        1 for value in times if value > now - timedelta(hours=1)
    )
    if hourly_count >= hourly_limit or len(times) >= daily_limit:
        return "rate", 0
    duplicate = await db.submission_fingerprint_exists(
        normalized_hash, now - timedelta(seconds=_RATE_WINDOW_SECONDS)
    )
    if record_event:
        await db.record_submission_event(
            user_id, event_type, normalized_hash, normalized_length
        )
        if (
            await db.count_recent_submission_events(global_since)
            >= GLOBAL_SUBMISSION_THRESHOLD
        ):
            await _set_global_pause(db)
    if duplicate:
        return "duplicate", 0
    return "granted", 0


def _is_unsupported_rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    last = _last_unsupported_at.get(user_id)
    if last is not None and now - last < SUBMISSION_INTERVAL_SECONDS:
        return True
    _last_unsupported_at[user_id] = now
    if len(_last_unsupported_at) > _RATE_LIMIT_CACHE_LIMIT:
        cutoff = now - _RATE_WINDOW_SECONDS
        for key in [
            key for key, value in _last_unsupported_at.items() if value <= cutoff
        ]:
            del _last_unsupported_at[key]
    return False


def _normalize_content(text: str) -> str:
    normalized = text.casefold().replace("ё", "е")
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def _duplicate_cutoff() -> str:
    return (datetime.now(UTC) - timedelta(seconds=_RATE_WINDOW_SECONDS)).isoformat()


def _normalized_hash(scope: str, normalized: str) -> str:
    if not normalized:
        return ""
    return hashlib.sha256(f"{scope}:{normalized}".encode()).hexdigest()


def _texts_are_similar(first: str, second: str) -> bool:
    if first == second:
        return True
    shorter = min(len(first), len(second))
    longer = max(len(first), len(second))
    if shorter < 40 or longer == 0 or shorter / longer < 0.8:
        return False
    matcher = SequenceMatcher(None, first, second, autojunk=False)
    return matcher.quick_ratio() >= 0.9 and matcher.ratio() >= 0.9


async def _is_duplicate_text(db: Database, content: str) -> bool:
    normalized = _normalize_content(content)
    if not normalized:
        return False
    async with await db.conn.execute(
        "SELECT text_content FROM posts WHERE created_at >= ? "
        "ORDER BY queue_id DESC LIMIT 100",
        (_duplicate_cutoff(),),
    ) as cur:
        rows = await cur.fetchall()
    return any(
        _texts_are_similar(
            normalized, _normalize_content(row["text_content"] or "")
        )
        for row in rows
    )


async def _is_duplicate_media(db: Database, kind: str, file_id: str) -> bool:
    async with await db.conn.execute(
        "SELECT 1 FROM posts WHERE kind = ? AND file_id = ? "
        "AND created_at >= ? LIMIT 1",
        (kind, file_id, _duplicate_cutoff()),
    ) as cur:
        return await cur.fetchone() is not None


async def _save_manual_post(
    db: Database,
    kind: str,
    content: str,
    file_id: str | None,
    submitter_id: int,
    route: str = "manual_queue",
    config: Config | None = None,
) -> None:
    async with await db.conn.execute(
        "INSERT INTO posts (kind, text_content, file_id, submitter_id, route, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (kind, content, file_id, submitter_id, route, now_iso()),
    ):
        pass
    await db.conn.commit()
    if config is not None:
        await queue_admin_notification(db, config, "posts")


async def _reserve_ai_post(
    db: Database, content: str, submitter_id: int
) -> str:
    for _ in range(3):
        code = await generate_unique_code(db)
        try:
            async with await db.conn.execute(
                "INSERT INTO posts (public_code, kind, text_content, submitter_id, route, "
                "status, created_at) VALUES (?, 'text', ?, ?, 'ai_processed', "
                "'publishing', ?)",
                (code, content, submitter_id, now_iso()),
            ):
                pass
            await db.conn.commit()
            return code
        except sqlite3.IntegrityError:
            await db.conn.rollback()
    raise RuntimeError("не удалось зарезервировать свободный код поста")


async def _delete_reserved_post(db: Database, code: str) -> None:
    async with await db.conn.execute(
        "DELETE FROM posts WHERE public_code = ? AND status = 'publishing'", (code,)
    ):
        pass
    await db.conn.commit()


@router.message(SubmitStates.waiting_content, F.text)
async def on_text_post(
    message: Message,
    state: FSMContext,
    db: Database,
    config: Config,
    moderation: ModerationEngine,
) -> None:
    if message.from_user is None:
        await state.clear()
        return
    user_id = message.from_user.id
    is_admin = _is_admin(config, user_id)
    content = (message.text or "").strip()
    if not content:
        await message.answer(texts.POST_EMPTY)
        return
    rendered = texts.bold_user_text(content)
    if texts.telegram_text_len(content) > TEXT_LIMIT:
        await message.answer(texts.POST_TOO_LONG)
        return
    normalized = _normalize_content(content)
    if not is_admin:
        guard_status, pause_minutes = await _submission_guard(
            db,
            user_id,
            "post",
            _normalized_hash("post", normalized),
            len(normalized),
        )
        if guard_status == "restricted":
            await state.clear()
            return
        if guard_status == "global":
            await message.answer(
                texts.GLOBAL_SUBMISSION_LIMIT.format(minutes=pause_minutes)
            )
            return
        if guard_status == "rate":
            await message.answer(texts.TOO_FREQUENT)
            return
        if guard_status == "duplicate" or await _is_duplicate_text(db, content):
            await message.answer(texts.POST_DUPLICATE)
            return
    mode = await db.get_setting("moderation_mode")
    if is_spam_text(content):
        if mode == "ai":
            await message.answer(texts.POST_REJECTED_SPAM)
        else:
            await _strike_and_answer(
                message, db, config, "spam", texts.POST_REJECTED_SPAM
            )
        await state.clear()
        return
    if mode != "ai":
        await _save_manual_post(
            db, "text", content, None, user_id, config=config
        )
        await message.answer(texts.POST_ACCEPTED_QUEUE)
        await state.clear()
        return

    if not is_admin:
        result = await moderation.check(content)
        if result.manual_review:
            await _save_manual_post(
                db,
                "text",
                content,
                None,
                user_id,
                route="ai_manual_review",
                config=config,
            )
            await message.answer(texts.POST_ACCEPTED_QUEUE)
            await state.clear()
            return
        if not result.approved:
            if result.verdict is Verdict.REJECT_SPAM:
                await message.answer(texts.POST_REJECTED_SPAM)
            elif result.verdict is Verdict.REJECT_TARGETED_INSULT:
                await message.answer(texts.POST_REJECTED_TARGETED_INSULT)
            else:
                await message.answer(texts.POST_REJECTED)
            await state.clear()
            return

    code = await _reserve_ai_post(db, content, user_id)
    bot = message.bot
    assert bot is not None
    try:
        sent = await bot.send_message(
            config.channel_id,
            rendered,
            reply_markup=complaint_button(code, 0),
        )
    except (ClientDecodeError, TelegramAPIError):
        logger.warning("Не удалось опубликовать AI-пост code=%s", code, exc_info=True)
        await _delete_reserved_post(db, code)
        await message.answer(texts.POST_SEND_FAILED)
        return

    async with await db.conn.execute(
        "UPDATE posts SET status = 'published', published_message_id = ? "
        "WHERE public_code = ? AND status = 'publishing'",
        (sent.message_id, code),
    ):
        pass
    await db.conn.commit()
    await message.answer(texts.POST_PUBLISHED)
    await state.clear()


async def _save_media_post(
    message: Message,
    state: FSMContext,
    db: Database,
    config: Config,
    kind: str,
    file_id: str,
) -> None:
    if message.from_user is None:
        await state.clear()
        return
    user_id = message.from_user.id
    is_admin = _is_admin(config, user_id)
    caption = (message.caption or "").strip()
    if texts.telegram_text_len(caption) > CAPTION_LIMIT:
        await message.answer(texts.CAPTION_TOO_LONG)
        return
    normalized = _normalize_content(caption) or f"media:{kind}:{file_id}"
    if not is_admin:
        guard_status, pause_minutes = await _submission_guard(
            db,
            user_id,
            "post",
            _normalized_hash("post", normalized),
            len(normalized),
        )
        if guard_status == "restricted":
            await state.clear()
            return
        if guard_status == "global":
            await message.answer(
                texts.GLOBAL_SUBMISSION_LIMIT.format(minutes=pause_minutes)
            )
            return
        if guard_status == "rate":
            await message.answer(texts.TOO_FREQUENT)
            return
        if guard_status == "duplicate" or await _is_duplicate_media(db, kind, file_id) or (
            caption and await _is_duplicate_text(db, caption)
        ):
            await message.answer(texts.POST_DUPLICATE)
            return
    if caption and is_spam_text(caption):
        mode = await db.get_setting("moderation_mode")
        if mode == "ai":
            await message.answer(texts.POST_REJECTED_SPAM)
        else:
            await _strike_and_answer(
                message, db, config, "spam", texts.POST_REJECTED_SPAM
            )
        await state.clear()
        return
    await _save_manual_post(
        db, kind, caption, file_id, user_id, config=config
    )
    await message.answer(texts.POST_ACCEPTED_QUEUE)
    await state.clear()


@router.message(SubmitStates.waiting_content, F.photo)
async def on_photo_post(
    message: Message, state: FSMContext, db: Database, config: Config
) -> None:
    if not message.photo:
        await message.answer(texts.ASK_POST_CONTENT)
        return
    await _save_media_post(
        message, state, db, config, "photo", message.photo[-1].file_id
    )


@router.message(SubmitStates.waiting_content, F.video)
async def on_video_post(
    message: Message, state: FSMContext, db: Database, config: Config
) -> None:
    if message.video is None:
        await message.answer(texts.ASK_POST_CONTENT)
        return
    await _save_media_post(
        message, state, db, config, "video", message.video.file_id
    )


@router.message(SubmitStates.waiting_content)
async def on_unsupported_post(message: Message) -> None:
    if message.from_user is not None and _is_unsupported_rate_limited(message.from_user.id):
        return
    await message.answer(texts.ASK_POST_CONTENT)
