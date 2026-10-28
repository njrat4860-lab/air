import hashlib
import logging
import math
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app import texts
from app.config import Config
from app.db import Database, now_iso
from app.ids import is_valid_code
from app.keyboards.user import (
    complaint_button,
    strike_appeal_keyboard,
    strike_appeal_pending_keyboard,
    strike_appeal_prompt_keyboard,
)
from app.services.admin_notifications import queue_admin_notification
from app.services.profanity_words import (
    contains_explicit_sexual,
    contains_profanity,
    is_spam_text,
)
from app.states import ComplaintStates

logger = logging.getLogger(__name__)
router = Router(name="complaint")

COMPLAINT_INTERVAL_SECONDS = 30.0
COMPLAINT_HOURLY_LIMIT = 5
COMPLAINT_DAILY_LIMIT = 20
NEW_USER_INTERVAL_SECONDS = 120.0
NEW_USER_HOURLY_LIMIT = 2
NEW_USER_DAILY_LIMIT = 5
GLOBAL_SUBMISSION_WINDOW_SECONDS = 5 * 60
GLOBAL_SUBMISSION_THRESHOLD = 30
GLOBAL_SUBMISSION_PAUSE_SECONDS = 15 * 60
GLOBAL_QUEUE_THRESHOLD = 50
_RATE_WINDOW_SECONDS = 24 * 60 * 60


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


def _is_private_user_message(message: Message) -> bool:
    return (
        message.chat.type == ChatType.PRIVATE
        and message.from_user is not None
        and message.chat.id == message.from_user.id
    )


def _is_private_callback(callback: CallbackQuery) -> bool:
    return (
        isinstance(callback.message, Message)
        and callback.message.chat.type == ChatType.PRIVATE
        and callback.message.chat.id == callback.from_user.id
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
    normalized_hash: str,
    normalized_length: int,
    record_event: bool = True,
) -> tuple[str, int]:
    async with db.submission_lock:
        return await _submission_guard_locked(
            db, user_id, normalized_hash, normalized_length, record_event
        )


async def _submission_guard_locked(
    db: Database,
    user_id: int,
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
    complaint_times = [
        parsed
        for event in events
        if event.get("event_type") == "complaint"
        and (parsed := _parse_time(event.get("created_at"))) is not None
    ]
    first_seen = _parse_time(access.get("first_seen_at"))
    is_new = first_seen is None or now - first_seen < timedelta(hours=24)
    interval = NEW_USER_INTERVAL_SECONDS if is_new else COMPLAINT_INTERVAL_SECONDS
    hourly_limit = NEW_USER_HOURLY_LIMIT if is_new else COMPLAINT_HOURLY_LIMIT
    daily_limit = NEW_USER_DAILY_LIMIT if is_new else COMPLAINT_DAILY_LIMIT
    if times and (now - times[0]).total_seconds() < interval:
        return "rate", 0
    if complaint_times and (
        now - complaint_times[0]
    ).total_seconds() < interval:
        return "rate", 0
    hourly_count = sum(
        1 for value in times if value > now - timedelta(hours=1)
    )
    complaint_hourly_count = sum(
        1 for value in complaint_times if value > now - timedelta(hours=1)
    )
    if (
        hourly_count >= hourly_limit
        or len(times) >= daily_limit
        or complaint_hourly_count >= hourly_limit
        or len(complaint_times) >= daily_limit
    ):
        return "rate", 0
    duplicate = await db.submission_fingerprint_exists(
        normalized_hash, now - timedelta(seconds=_RATE_WINDOW_SECONDS)
    )
    if record_event:
        await db.record_submission_event(
            user_id, "complaint", normalized_hash, normalized_length
        )
        if (
            await db.count_recent_submission_events(global_since)
            >= GLOBAL_SUBMISSION_THRESHOLD
        ):
            await _set_global_pause(db)
    if duplicate:
        return "duplicate", 0
    return "granted", 0


def _normalize_complaint(text: str) -> str:
    normalized = text.casefold().replace("ё", "е")
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


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


async def _is_duplicate_complaint(db: Database, text: str) -> bool:
    normalized = _normalize_complaint(text)
    if not normalized:
        return False
    cutoff = (
        datetime.now(UTC) - timedelta(seconds=_RATE_WINDOW_SECONDS)
    ).isoformat()
    async with await db.conn.execute(
        "SELECT description FROM complaints WHERE created_at >= ? "
        "ORDER BY id DESC LIMIT 100",
        (cutoff,),
    ) as cur:
        rows = await cur.fetchall()
    return any(
        _texts_are_similar(
            normalized, _normalize_complaint(row["description"] or "")
        )
        for row in rows
    )


def _private_state(bot_id: int, fsm_storage: BaseStorage, user_id: int) -> FSMContext:
    key = StorageKey(bot_id=bot_id, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=fsm_storage, key=key)


async def _published_post(db: Database, code: str):
    async with await db.conn.execute(
        "SELECT public_code, published_message_id, complaints_count FROM posts "
        "WHERE public_code = ? AND status = 'published'",
        (code,),
    ) as cur:
        return await cur.fetchone()


async def _prepare_strike_appeal(
    user_id: int, state: FSMContext, db: Database, config: Config
) -> tuple[str, InlineKeyboardMarkup | None]:
    if _is_admin(config, user_id):
        await db.end_strike_appeal_draft(user_id)
        return texts.STRIKE_APPEAL_UNAVAILABLE, None
    access = await db.get_user_access(user_id)
    if access is None or int(access.get("is_restricted") or 0) != 1:
        await db.end_strike_appeal_draft(user_id)
        return texts.STRIKE_APPEAL_UNAVAILABLE, None
    pending = await db.get_pending_strike_appeal_for_user(user_id)
    if pending is not None:
        await db.end_strike_appeal_draft(user_id)
        return (
            texts.STRIKE_APPEAL_ALREADY_PENDING,
            strike_appeal_pending_keyboard(),
        )
    strike_event = await db.get_latest_active_strike_event(user_id)
    if strike_event is None:
        await db.end_strike_appeal_draft(user_id)
        return texts.STRIKE_APPEAL_UNAVAILABLE, None
    if await db.has_decided_strike_appeal(int(strike_event["id"])):
        await db.end_strike_appeal_draft(user_id)
        return texts.STRIKE_APPEAL_UNAVAILABLE, None
    await state.clear()
    await db.begin_strike_appeal_draft(
        user_id, int(strike_event["id"])
    )
    return texts.STRIKE_APPEAL_PROMPT, strike_appeal_prompt_keyboard()


@router.callback_query(F.data == "strike_appeal:start")
async def strike_appeal_start(
    callback: CallbackQuery, state: FSMContext, db: Database, config: Config
) -> None:
    if not _is_private_callback(callback):
        await callback.answer()
        return
    assert isinstance(callback.message, Message)
    text, reply_markup = await _prepare_strike_appeal(
        callback.from_user.id, state, db, config
    )
    await callback.message.answer(text, reply_markup=reply_markup)
    await callback.answer()


@router.message(Command("appeal"))
async def strike_appeal_command(
    message: Message, state: FSMContext, db: Database, config: Config
) -> None:
    if not _is_private_user_message(message):
        return
    assert message.from_user is not None
    text, reply_markup = await _prepare_strike_appeal(
        message.from_user.id, state, db, config
    )
    await message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data == "strike_appeal:cancel")
async def strike_appeal_cancel(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if not _is_private_callback(callback):
        await callback.answer()
        return
    await db.end_strike_appeal_draft(callback.from_user.id)
    await state.clear()
    assert isinstance(callback.message, Message)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except (ClientDecodeError, TelegramAPIError):
        logger.debug("не удалось убрать клавиатуру апелляции", exc_info=True)
    await callback.answer("Отменено")


@router.callback_query(F.data == "strike_appeal:withdraw")
async def strike_appeal_withdraw(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if not _is_private_callback(callback):
        await callback.answer()
        return
    await db.end_strike_appeal_draft(callback.from_user.id)
    await state.clear()
    changed = await db.withdraw_strike_appeal(callback.from_user.id)
    assert isinstance(callback.message, Message)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except (ClientDecodeError, TelegramAPIError):
        logger.debug("не удалось убрать клавиатуру апелляции", exc_info=True)
    if changed:
        await callback.message.answer(texts.STRIKE_APPEAL_WITHDRAWN)
    await callback.answer()


async def _is_strike_appeal_draft_message(
    message: Message, db: Database
) -> bool:
    return (
        _is_private_user_message(message)
        and message.from_user is not None
        and db.is_user_restricted_sync(message.from_user.id)
        and db.is_strike_appeal_draft_user_sync(message.from_user.id)
    )


@router.message(_is_strike_appeal_draft_message, F.text)
async def strike_appeal_text(
    message: Message, state: FSMContext, db: Database, config: Config
) -> None:
    if not _is_private_user_message(message):
        return
    assert message.from_user is not None
    user_id = message.from_user.id
    description = (message.text or "").strip()
    if len(description) < config.complaint_min_len:
        await message.answer(texts.STRIKE_APPEAL_TOO_SHORT)
        return
    if len(description) > config.complaint_max_len:
        await message.answer(texts.STRIKE_APPEAL_TOO_LONG)
        return
    await db.end_strike_appeal_draft(user_id)
    await state.clear()
    pending = await db.get_pending_strike_appeal_for_user(user_id)
    if pending is not None:
        await message.answer(
            texts.STRIKE_APPEAL_ALREADY_PENDING,
            reply_markup=strike_appeal_pending_keyboard(),
        )
        return
    appeal = await db.create_strike_appeal(user_id, description)
    if appeal is None:
        await message.answer(texts.STRIKE_APPEAL_UNAVAILABLE)
        return
    await queue_admin_notification(db, config, "strike_appeals")
    await message.answer(
        texts.STRIKE_APPEAL_ACCEPTED,
        reply_markup=strike_appeal_pending_keyboard(),
    )


@router.callback_query(F.data == "menu:complaint")
async def on_menu_complaint(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ComplaintStates.waiting_code)
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.ASK_COMPLAINT_CODE)
    else:
        bot = callback.bot
        assert bot is not None
        await bot.send_message(callback.from_user.id, texts.ASK_COMPLAINT_CODE)
    await callback.answer()


@router.message(ComplaintStates.waiting_code, F.text)
async def on_complaint_code(message: Message, state: FSMContext, db: Database) -> None:
    code = (message.text or "").strip().upper()
    if not is_valid_code(code) or not await _published_post(db, code):
        await message.answer(texts.COMPLAINT_CODE_NOT_FOUND)
        return
    await state.update_data(code=code)
    await state.set_state(ComplaintStates.waiting_description)
    await message.answer(texts.ASK_COMPLAINT_TEXT)


@router.callback_query(F.data.startswith("complain:"))
async def on_complain_button(
    callback: CallbackQuery, fsm_storage: BaseStorage, db: Database
) -> None:
    code = (callback.data or "").split(":", 1)[1]
    if not is_valid_code(code) or not await _published_post(db, code):
        await callback.answer(texts.COMPLAINT_CODE_NOT_FOUND_PLAIN, show_alert=True)
        return
    bot = callback.bot
    assert bot is not None
    state = _private_state(bot.id, fsm_storage, callback.from_user.id)
    await state.update_data(code=code)
    await state.set_state(ComplaintStates.waiting_description)
    try:
        await bot.send_message(callback.from_user.id, texts.ASK_COMPLAINT_TEXT)
    except (TelegramForbiddenError, TelegramBadRequest):
        await state.clear()
        await callback.answer(texts.COMPLAINT_START_BOT_FIRST_PLAIN, show_alert=True)
        return
    await callback.answer()


@router.message(ComplaintStates.waiting_description, F.text)
async def on_complaint_description(
    message: Message, state: FSMContext, db: Database, config: Config
) -> None:
    if message.from_user is None:
        await state.clear()
        return
    data = await state.get_data()
    code_value = data.get("code")
    if not isinstance(code_value, str) or not is_valid_code(code_value):
        await message.answer(texts.COMPLAINT_CODE_NOT_FOUND)
        await state.clear()
        return
    code = code_value

    text = (message.text or "").strip()
    if len(text) < config.complaint_min_len:
        await message.answer(texts.COMPLAINT_TOO_SHORT)
        return
    if len(text) > config.complaint_max_len:
        await message.answer(texts.COMPLAINT_TOO_LONG)
        return
    user_id = message.from_user.id
    is_admin = _is_admin(config, user_id)
    normalized = _normalize_complaint(text)
    if not is_admin:
        guard_status, pause_minutes = await _submission_guard(
            db,
            user_id,
            _normalized_hash("complaint", normalized),
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
        if guard_status == "duplicate" or await _is_duplicate_complaint(db, text):
            await message.answer(texts.COMPLAINT_DUPLICATE)
            await state.clear()
            return
    if contains_profanity(text) or contains_explicit_sexual(text):
        await _strike_and_answer(
            message,
            db,
            config,
            "profanity",
            texts.COMPLAINT_REJECTED_PROFANITY,
        )
        await state.clear()
        return
    if is_spam_text(text):
        await _strike_and_answer(
            message, db, config, "spam", texts.COMPLAINT_REJECTED_SPAM
        )
        await state.clear()
        return
    if not await _published_post(db, code):
        await message.answer(texts.COMPLAINT_CODE_NOT_FOUND)
        await state.clear()
        return

    try:
        async with await db.conn.execute(
            "INSERT INTO complaints (public_code, user_id, description, created_at) "
            "VALUES (?, ?, ?, ?)",
            (code, message.from_user.id, text, now_iso()),
        ):
            pass
        async with await db.conn.execute(
            "UPDATE posts SET complaints_count = ("
            "SELECT COUNT(*) FROM complaints WHERE complaints.public_code = posts.public_code"
            ") WHERE public_code = ?",
            (code,),
        ):
            pass
        await db.conn.commit()
    except sqlite3.IntegrityError:
        await db.conn.rollback()
        await message.answer(texts.COMPLAINT_DUPLICATE)
        await state.clear()
        return

    await queue_admin_notification(db, config, "complaints")
    row = await _published_post(db, code)
    bot = message.bot
    if bot is not None and row and row["published_message_id"] is not None:
        try:
            await bot.edit_message_reply_markup(
                chat_id=config.channel_id,
                message_id=row["published_message_id"],
                reply_markup=complaint_button(code, row["complaints_count"]),
            )
        except (ClientDecodeError, TelegramAPIError):
            logger.debug(
                "не удалось обновить кнопку жалобы code=%s", code, exc_info=True
            )

    await message.answer(texts.COMPLAINT_ACCEPTED)
    await state.clear()
