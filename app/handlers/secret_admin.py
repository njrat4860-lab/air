import asyncio
import hmac
import logging
import sqlite3
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import ClientDecodeError, TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.config import Config
from app.db import Database, now_iso
from app.ids import generate_unique_code, is_valid_code
from app.keyboards.admin import (
    COMPLAINTS_PAGE_SIZE,
    complaint_detail_keyboard,
    complaints_list_keyboard,
    empty_queue_keyboard,
    notifications_keyboard,
    panel_keyboard,
    restrictions_keyboard,
    review_keyboard,
    strike_appeal_detail_keyboard,
)
from app.keyboards.user import complaint_button, strike_appeal_keyboard
from app.services.secret_panel import SecretPanel

logger = logging.getLogger(__name__)
router = Router(name="secret_admin")

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
_MAX_COMPLAINTS_OFFSET = 100_000
_review_lock = asyncio.Lock()


def _matches_secret(message: Message, config: Config) -> bool:
    if message.chat.type != ChatType.PRIVATE:
        return False
    if message.from_user is None or message.chat.id != message.from_user.id:
        return False
    if config.admin_ids and message.from_user.id not in config.admin_ids:
        return False
    if not message.text:
        return False
    candidate = message.text.strip().casefold().encode("utf-8")
    expected = config.secret_word.strip().casefold().encode("utf-8")
    return hmac.compare_digest(candidate, expected)


async def _moderation_mode(db: Database) -> str:
    return await db.get_setting("moderation_mode") or "manual"


async def _queue_count(db: Database) -> int:
    async with await db.conn.execute(
        "SELECT COUNT(*) AS total FROM posts "
        "WHERE route IN ('manual_queue', 'ai_manual_review') "
        "AND status IN ('pending', 'publishing')"
    ) as cur:
        row = await cur.fetchone()
    return int(row["total"]) if row is not None else 0


async def _restrictions_count(db: Database) -> int:
    async with await db.conn.execute(
        "SELECT COUNT(*) AS total FROM user_access "
        "WHERE is_restricted = 1 AND (restriction_permanent = 1 "
        "OR restricted_until > ?)",
        (now_iso(),),
    ) as cur:
        row = await cur.fetchone()
    return int(row["total"]) if row is not None else 0


async def _show_panel(chat_id: int, db: Database, secret_panel: SecretPanel) -> None:
    mode = await _moderation_mode(db)
    count = await _queue_count(db)
    restrictions_count = await _restrictions_count(db)
    appeals_count = await db.count_pending_strike_appeals()
    settings = await db.get_admin_notification_settings(chat_id)
    await secret_panel.show_text(
        chat_id,
        "Секретная панель.",
        reply_markup=panel_keyboard(
            mode,
            count,
            restrictions_count,
            appeals_count,
            any(settings.values()),
        ),
    )


def _restriction_deadline(row: dict[str, object]) -> str:
    if row.get("restriction_permanent") == 1:
        return "бессрочно"
    value = str(row.get("restricted_until") or "")
    return value[:16].replace("T", " ") + " UTC" if value else "не задан"


async def _show_restrictions(
    chat_id: int, db: Database, secret_panel: SecretPanel
) -> None:
    rows = await db.list_restricted_users(limit=20)
    if not rows:
        await secret_panel.show_text(
            chat_id,
            "Действующих ограничений нет.",
            reply_markup=empty_queue_keyboard(),
        )
        return
    lines = ["Действующие ограничения:"]
    user_ids: list[int] = []
    for row in rows:
        user_id = int(row["user_id"])
        user_ids.append(user_id)
        lines.append(
            f"<code>{user_id}</code> — страйков: {int(row.get('strikes') or 0)}, "
            f"срок: {escape(_restriction_deadline(row))}"
        )
    await secret_panel.show_text(
        chat_id,
        "\n".join(lines),
        reply_markup=restrictions_keyboard(user_ids),
    )


@router.message(_matches_secret)
async def on_secret_word(
    message: Message, db: Database, secret_panel: SecretPanel, state: FSMContext
) -> None:
    chat_id = message.chat.id
    try:
        await message.delete()
    except (ClientDecodeError, TelegramAPIError):
        logger.debug("не удалось удалить сообщение с секретным словом", exc_info=True)
    await state.clear()
    await _show_panel(chat_id, db, secret_panel)


async def _guard(
    callback: CallbackQuery, secret_panel: SecretPanel
) -> int | None:
    if callback.message is None:
        await callback.answer()
        return None
    chat = callback.message.chat
    if chat.type != ChatType.PRIVATE or chat.id != callback.from_user.id:
        await callback.answer()
        return None
    if not await secret_panel.is_current_message(chat.id, callback.message.message_id):
        await callback.answer()
        return None
    await secret_panel.touch(chat.id)
    return chat.id


@router.callback_query(F.data == "sadmin:mode:cycle")
async def on_cycle_mode(
    callback: CallbackQuery, db: Database, secret_panel: SecretPanel
) -> None:
    chat_id = await _guard(callback, secret_panel)
    if chat_id is None:
        return
    current = await _moderation_mode(db)
    next_mode_by_current = {"manual": "ai", "ai": "off", "off": "manual"}
    next_mode = next_mode_by_current.get(current, "manual")
    await db.set_setting("moderation_mode", next_mode)
    await _show_panel(chat_id, db, secret_panel)
    await callback.answer()


@router.callback_query(F.data.in_({"sadmin:mode:manual", "sadmin:mode:ai"}))
async def on_toggle_mode(
    callback: CallbackQuery, db: Database, secret_panel: SecretPanel
) -> None:
    chat_id = await _guard(callback, secret_panel)
    if chat_id is None:
        return
    requested = (callback.data or "").split(":")[-1]
    current = await _moderation_mode(db)
    await db.set_setting(
        "moderation_mode", "off" if current == requested else requested
    )
    await _show_panel(chat_id, db, secret_panel)
    await callback.answer()


@router.callback_query(F.data == "sadmin:back")
async def on_back(
    callback: CallbackQuery, db: Database, secret_panel: SecretPanel
) -> None:
    chat_id = await _guard(callback, secret_panel)
    if chat_id is None:
        return
    await _show_panel(chat_id, db, secret_panel)
    await callback.answer()


async def _show_review_item(
    chat_id: int, db: Database, secret_panel: SecretPanel
) -> None:
    async with await db.conn.execute(
        "SELECT * FROM posts WHERE route IN ('manual_queue', 'ai_manual_review') "
        "AND status IN ('pending', 'publishing') ORDER BY queue_id ASC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
    if not row:
        await secret_panel.show_text(
            chat_id, "Очередь пуста.", reply_markup=empty_queue_keyboard()
        )
        return
    context = {"queue_id": row["queue_id"]}
    if row["kind"] == "text":
        review_text = escape(row["text_content"] or "(пусто)")
        if row["route"] == "ai_manual_review":
            review_text = (
                "AI отправил на ручную проверку: негатив о личности.\n\n"
                f"{review_text}"
            )
        await secret_panel.show_text(
            chat_id,
            review_text,
            reply_markup=review_keyboard(),
            context=context,
        )
    elif row["kind"] in {"photo", "video"} and row["file_id"]:
        caption = escape(row["text_content"]) if row["text_content"] else None
        await secret_panel.show_media(
            chat_id,
            row["kind"],
            row["file_id"],
            caption=caption,
            reply_markup=review_keyboard(),
            context=context,
        )
    else:
        await secret_panel.show_text(
            chat_id,
            "Повреждённая запись очереди.",
            reply_markup=review_keyboard(),
            context=context,
        )


@router.callback_query(F.data == "sadmin:review:start")
async def on_review_start(
    callback: CallbackQuery, db: Database, secret_panel: SecretPanel
) -> None:
    chat_id = await _guard(callback, secret_panel)
    if chat_id is None:
        return
    if await _moderation_mode(db) not in {"manual", "ai"}:
        await on_back(callback, db, secret_panel)
        return
    await _show_review_item(chat_id, db, secret_panel)
    await callback.answer()


async def _reserve_review_code(db: Database, row) -> str | None:
    existing_code = row["public_code"] if is_valid_code(row["public_code"]) else None
    attempts = 1 if existing_code else 3
    for _ in range(attempts):
        code = existing_code or await generate_unique_code(db)
        try:
            async with await db.conn.execute(
                "UPDATE posts SET public_code = ?, status = 'publishing' "
                "WHERE queue_id = ? AND status IN ('pending', 'publishing') "
                "AND (public_code IS NULL OR public_code = ?)",
                (code, row["queue_id"], code),
            ) as cur:
                changed = cur.rowcount
            await db.conn.commit()
        except sqlite3.IntegrityError:
            await db.conn.rollback()
            continue
        return code if changed == 1 else None
    return None


async def _unreserve_review_code(
    db: Database, queue_id: int, code: str, had_code: bool
) -> None:
    if had_code:
        async with await db.conn.execute(
            "UPDATE posts SET status = 'pending' WHERE queue_id = ? "
            "AND public_code = ? AND status = 'publishing'",
            (queue_id, code),
        ):
            pass
    else:
        async with await db.conn.execute(
            "UPDATE posts SET public_code = NULL, status = 'pending' "
            "WHERE queue_id = ? AND public_code = ? AND status = 'publishing'",
            (queue_id, code),
        ):
            pass
    await db.conn.commit()


@router.callback_query(
    F.data.in_(
        {
            "sadmin:review:approve",
            "sadmin:review:reject",
            "sadmin:review:reject_strike",
        }
    )
)
async def on_review_decision(
    callback: CallbackQuery, db: Database, config: Config, secret_panel: SecretPanel
) -> None:
    chat_id = await _guard(callback, secret_panel)
    if chat_id is None:
        return
    context = await secret_panel.get_context(chat_id)
    queue_id = context.get("queue_id")
    async with _review_lock:
        await _review_decision_locked(
            callback, db, config, secret_panel, chat_id, queue_id
        )


async def _review_decision_locked(
    callback: CallbackQuery,
    db: Database,
    config: Config,
    secret_panel: SecretPanel,
    chat_id: int,
    queue_id: object,
) -> None:
    if type(queue_id) is not int:
        await _show_review_item(chat_id, db, secret_panel)
        await callback.answer()
        return

    async with await db.conn.execute(
        "SELECT * FROM posts WHERE queue_id = ? "
        "AND route IN ('manual_queue', 'ai_manual_review') "
        "AND status IN ('pending', 'publishing')",
        (queue_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        await _show_review_item(chat_id, db, secret_panel)
        await callback.answer()
        return

    if not (callback.data or "").endswith("approve"):
        reject_with_strike = callback.data == "sadmin:review:reject_strike"
        submitter_id = int(row["submitter_id"])
        cursor = await db.conn.execute(
            "DELETE FROM posts WHERE queue_id = ? "
            "AND status IN ('pending', 'publishing')",
            (queue_id,),
        )
        try:
            deleted = cursor.rowcount
        finally:
            await cursor.close()
        await db.conn.commit()
        if reject_with_strike and deleted and submitter_id not in config.admin_ids:
            was_restricted = db.is_user_restricted_sync(submitter_id)
            access = await db.add_strike(
                submitter_id, "manual_reject", queue_id=queue_id
            )
            if access is not None and not was_restricted:
                notice = (
                    texts.ACCESS_RESTRICTED_UNTIL_ADMIN
                    if int(access.get("restriction_permanent") or 0)
                    else texts.ACCESS_RESTRICTED_TEMPORARY
                )
                bot = callback.bot
                assert bot is not None
                try:
                    await bot.send_message(
                        submitter_id,
                        notice,
                        reply_markup=strike_appeal_keyboard(),
                    )
                except (ClientDecodeError, TelegramAPIError):
                    logger.debug(
                        "не удалось уведомить пользователя об ограничении",
                        exc_info=True,
                    )
        await _show_review_item(chat_id, db, secret_panel)
        await callback.answer()
        return

    caption = row["text_content"] or ""
    rendered_caption = texts.bold_user_text(caption) if caption else None
    if row["kind"] == "text":
        if not caption.strip():
            await callback.answer("Пустой текст нельзя опубликовать.", show_alert=True)
            return
        if rendered_caption is None or texts.telegram_text_len(caption) > TEXT_LIMIT:
            await callback.answer("Слишком длинный текст поста.", show_alert=True)
            return
    elif row["kind"] in {"photo", "video"}:
        if not row["file_id"]:
            await callback.answer("У медиапоста нет file_id.", show_alert=True)
            return
        if texts.telegram_text_len(caption) > CAPTION_LIMIT:
            await callback.answer("Слишком длинная подпись.", show_alert=True)
            return
    else:
        await callback.answer("Неизвестный тип поста.", show_alert=True)
        return

    had_code = is_valid_code(row["public_code"])
    code = await _reserve_review_code(db, row)
    if code is None:
        await _show_review_item(chat_id, db, secret_panel)
        await callback.answer()
        return

    bot = callback.bot
    assert bot is not None
    try:
        if row["kind"] == "text":
            sent = await bot.send_message(
                config.channel_id,
                rendered_caption or "",
                reply_markup=complaint_button(code, 0),
            )
        elif row["kind"] == "photo":
            sent = await bot.send_photo(
                config.channel_id,
                row["file_id"],
                caption=rendered_caption,
                reply_markup=complaint_button(code, 0),
            )
        else:
            sent = await bot.send_video(
                config.channel_id,
                row["file_id"],
                caption=rendered_caption,
                reply_markup=complaint_button(code, 0),
            )
    except (ClientDecodeError, TelegramAPIError):
        logger.warning(
            "Не удалось опубликовать пост queue_id=%s code=%s",
            queue_id,
            code,
            exc_info=True,
        )
        await _unreserve_review_code(db, queue_id, code, had_code)
        await callback.answer("Не удалось опубликовать пост.", show_alert=True)
        return

    async with await db.conn.execute(
        "UPDATE posts SET status = 'published', published_message_id = ? "
        "WHERE queue_id = ? AND public_code = ? AND status = 'publishing'",
        (sent.message_id, queue_id, code),
    ):
        pass
    await db.conn.commit()
    await _show_review_item(chat_id, db, secret_panel)
    await callback.answer()


def _complaints_offset(callback_data: str | None) -> int:
    raw = (callback_data or "").rsplit(":", 1)[-1]
    try:
        return min(max(0, int(raw)), _MAX_COMPLAINTS_OFFSET)
    except ValueError:
        return 0


@router.callback_query(F.data == "sadmin:restrictions:list")
async def on_restrictions_list(
    callback: CallbackQuery, db: Database, secret_panel: SecretPanel
) -> None:
    chat_id = await _guard(callback, secret_panel)
    if chat_id is None:
        return
    await _show_restrictions(chat_id, db, secret_panel)
    await callback.answer()


@router.callback_query(F.data.startswith("sadmin:restrictions:unblock:"))
async def on_restrictions_unblock(
    callback: CallbackQuery, db: Database, secret_panel: SecretPanel
) -> None:
    chat_id = await _guard(callback, secret_panel)
    if chat_id is None:
        return
    raw_user_id = (callback.data or "").rsplit(":", 1)[-1]
    try:
        user_id = int(raw_user_id)
    except ValueError:
        await callback.answer("Некорректный user_id.", show_alert=True)
        return
    removed, appeal_closed = await db.remove_restriction_details(
        user_id, callback.from_user.id
    )
    if removed:
        bot = callback.bot
        assert bot is not None
        user_text = (
            texts.STRIKE_APPEAL_APPROVED + "\n" + texts.ACCESS_RESTORED
            if appeal_closed
            else texts.ACCESS_RESTORED
        )
        try:
            await bot.send_message(user_id, user_text)
        except (ClientDecodeError, TelegramAPIError):
            logger.debug(
                "не удалось уведомить пользователя о снятии ограничения",
                exc_info=True,
            )
    await _show_restrictions(chat_id, db, secret_panel)
    await callback.answer(
        "Ограничение снято." if removed else "Ограничение уже отсутствует.",
        show_alert=not removed,
    )


@router.callback_query(F.data.startswith("sadmin:complaints:list:"))
async def on_complaints_list(
    callback: CallbackQuery, db: Database, secret_panel: SecretPanel
) -> None:
    chat_id = await _guard(callback, secret_panel)
    if chat_id is None:
        return
    offset = _complaints_offset(callback.data)
    async with await db.conn.execute(
        "SELECT public_code, complaints_count FROM posts "
        "WHERE complaints_count > 0 AND public_code IS NOT NULL "
        "ORDER BY complaints_count DESC, queue_id ASC LIMIT ? OFFSET ?",
        (COMPLAINTS_PAGE_SIZE + 1, offset),
    ) as cur:
        rows = await cur.fetchall()
    has_next = len(rows) > COMPLAINTS_PAGE_SIZE
    page_rows = rows[:COMPLAINTS_PAGE_SIZE]
    if not page_rows:
        await secret_panel.show_text(
            chat_id, "Жалоб нет.", reply_markup=empty_queue_keyboard()
        )
    else:
        items = [
            (row["public_code"], row["complaints_count"])
            for row in page_rows
            if is_valid_code(row["public_code"])
        ]
        await secret_panel.show_text(
            chat_id,
            "Посты с жалобами:",
            reply_markup=complaints_list_keyboard(items, offset, has_next),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("sadmin:complaints:open:"))
async def on_complaint_open(
    callback: CallbackQuery, db: Database, secret_panel: SecretPanel
) -> None:
    chat_id = await _guard(callback, secret_panel)
    if chat_id is None:
        return
    code = (callback.data or "").split(":")[-1]
    if not is_valid_code(code):
        await callback.answer(texts.COMPLAINT_CODE_NOT_FOUND_PLAIN, show_alert=True)
        return
    async with await db.conn.execute(
        "SELECT text_content FROM posts WHERE public_code = ?", (code,)
    ) as cur:
        post = await cur.fetchone()
    async with await db.conn.execute(
        "SELECT description FROM complaints WHERE public_code = ? ORDER BY id", (code,)
    ) as cur:
        complaints = await cur.fetchall()
    lines = [
        f"Пост {escape(code)}:",
        escape(post["text_content"] or "") if post else "",
        "",
        "Жалобы:",
    ]
    lines += [f"- {escape(item['description'])}" for item in complaints]
    await secret_panel.show_text(
        chat_id,
        "\n".join(lines),
        reply_markup=complaint_detail_keyboard(code, len(complaints)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sadmin:complaints:delete:"))
async def on_complaint_delete(
    callback: CallbackQuery, db: Database, config: Config, secret_panel: SecretPanel
) -> None:
    chat_id = await _guard(callback, secret_panel)
    if chat_id is None:
        return
    code = (callback.data or "").split(":")[-1]
    if not is_valid_code(code):
        await callback.answer(texts.COMPLAINT_CODE_NOT_FOUND_PLAIN, show_alert=True)
        return
    async with await db.conn.execute(
        "SELECT published_message_id FROM posts WHERE public_code = ?", (code,)
    ) as cur:
        row = await cur.fetchone()
    if row and row["published_message_id"] is not None:
        bot = callback.bot
        assert bot is not None
        try:
            await bot.delete_message(
                config.channel_id, row["published_message_id"]
            )
        except (ClientDecodeError, TelegramAPIError):
            logger.debug(
                "не удалось удалить пост из канала code=%s", code, exc_info=True
            )
    async with await db.conn.execute(
        "DELETE FROM complaints WHERE public_code = ?", (code,)
    ):
        pass
    async with await db.conn.execute(
        "DELETE FROM posts WHERE public_code = ?", (code,)
    ):
        pass
    await db.conn.commit()
    await on_complaints_list(callback, db, secret_panel)


@router.callback_query(F.data.startswith("sadmin:complaints:dismiss:"))
async def on_complaint_dismiss(
    callback: CallbackQuery, db: Database, config: Config, secret_panel: SecretPanel
) -> None:
    chat_id = await _guard(callback, secret_panel)
    if chat_id is None:
        return
    code = (callback.data or "").split(":")[-1]
    if not is_valid_code(code):
        await callback.answer(texts.COMPLAINT_CODE_NOT_FOUND_PLAIN, show_alert=True)
        return
    async with await db.conn.execute(
        "DELETE FROM complaints WHERE public_code = ?", (code,)
    ):
        pass
    async with await db.conn.execute(
        "UPDATE posts SET complaints_count = 0 WHERE public_code = ?", (code,)
    ):
        pass
    await db.conn.commit()
    async with await db.conn.execute(
        "SELECT published_message_id FROM posts WHERE public_code = ?", (code,)
    ) as cur:
        row = await cur.fetchone()
    if row and row["published_message_id"] is not None:
        bot = callback.bot
        assert bot is not None
        try:
            await bot.edit_message_reply_markup(
                chat_id=config.channel_id,
                message_id=row["published_message_id"],
                reply_markup=complaint_button(code, 0),
            )
        except (ClientDecodeError, TelegramAPIError):
            logger.debug(
                "не удалось сбросить счётчик жалоб code=%s", code, exc_info=True
            )
    await on_complaints_list(callback, db, secret_panel)


async def _show_notifications(
    chat_id: int, db: Database, secret_panel: SecretPanel
) -> None:
    settings = await db.get_admin_notification_settings(chat_id)
    await secret_panel.show_text(
        chat_id,
        "Уведомления администратора:",
        reply_markup=notifications_keyboard(settings),
    )


@router.callback_query(F.data == "sadmin:notifications:show")
async def on_notifications_show(
    callback: CallbackQuery, db: Database, secret_panel: SecretPanel
) -> None:
    chat_id = await _guard(callback, secret_panel)
    if chat_id is None:
        return
    await _show_notifications(chat_id, db, secret_panel)
    await callback.answer()


@router.callback_query(F.data.startswith("sadmin:notifications:toggle:"))
async def on_notifications_toggle(
    callback: CallbackQuery, db: Database, secret_panel: SecretPanel
) -> None:
    chat_id = await _guard(callback, secret_panel)
    if chat_id is None:
        return
    kind = (callback.data or "").rsplit(":", 1)[-1]
    try:
        await db.toggle_admin_notification(callback.from_user.id, kind)
    except ValueError:
        await callback.answer("Некорректный тип уведомления.", show_alert=True)
        return
    await _show_notifications(chat_id, db, secret_panel)
    await callback.answer()


async def _show_next_strike_appeal(
    chat_id: int, db: Database, secret_panel: SecretPanel
) -> bool:
    total = await db.count_pending_strike_appeals()
    appeal = await db.get_next_pending_strike_appeal()
    if appeal is None:
        await secret_panel.show_text(
            chat_id,
            "Ожидающих апелляций на страйк нет.",
            reply_markup=empty_queue_keyboard(),
        )
        return False
    appeal_id = int(appeal["id"])
    lines = [
        f"Апелляция #{appeal_id} · ожидает: {total}",
        f"Пользователь: <code>{int(appeal['user_id'])}</code>",
        f"Причина страйка: {escape(str(appeal.get('strike_reason') or ''))}",
        f"Страйк: {escape(str(appeal.get('strike_created_at') or ''))}",
        f"Апелляция: {escape(str(appeal.get('created_at') or ''))}",
        "",
        escape(str(appeal.get("description") or "")),
    ]
    await secret_panel.show_text(
        chat_id,
        "\n".join(lines),
        reply_markup=strike_appeal_detail_keyboard(appeal_id),
    )
    return True


def _callback_appeal_id(callback_data: str | None) -> int | None:
    raw = (callback_data or "").rsplit(":", 1)[-1]
    try:
        appeal_id = int(raw)
    except ValueError:
        return None
    return appeal_id if appeal_id > 0 else None


@router.callback_query(F.data == "sadmin:appeals:next")
@router.callback_query(F.data == "sadmin:appeals:list")
@router.callback_query(F.data.startswith("sadmin:appeals:open:"))
async def on_strike_appeal_next(
    callback: CallbackQuery, db: Database, secret_panel: SecretPanel
) -> None:
    chat_id = await _guard(callback, secret_panel)
    if chat_id is None:
        return
    await _show_next_strike_appeal(
        chat_id, db, secret_panel
    )
    await callback.answer()

@router.callback_query(F.data.startswith("sadmin:appeals:decision:"))
async def on_strike_appeal_decision(
    callback: CallbackQuery, db: Database, secret_panel: SecretPanel
) -> None:
    chat_id = await _guard(callback, secret_panel)
    if chat_id is None:
        return
    parts = (callback.data or "").split(":")
    appeal_id = _callback_appeal_id(callback.data)
    decision = parts[3] if len(parts) == 5 else ""
    if appeal_id is None or decision not in {"reject", "unblock", "cancel"}:
        await callback.answer("Некорректное решение.", show_alert=True)
        return
    appeal = await db.decide_strike_appeal(
        appeal_id, callback.from_user.id, decision
    )
    if appeal is None:
        await _show_next_strike_appeal(chat_id, db, secret_panel)
        await callback.answer("Апелляция уже обработана.", show_alert=True)
        return
    user_id = int(appeal["user_id"])
    reply_markup = None
    if decision == "reject":
        user_text = texts.STRIKE_APPEAL_REJECTED
    elif decision == "unblock":
        user_text = (
            texts.STRIKE_APPEAL_APPROVED + "\n" + texts.ACCESS_RESTORED
        )
    else:
        access = await db.get_user_access(user_id)
        if access is not None and int(access.get("is_restricted") or 0) == 1:
            restriction_text = (
                texts.ACCESS_RESTRICTED_UNTIL_ADMIN
                if int(access.get("restriction_permanent") or 0) == 1
                else texts.ACCESS_RESTRICTED_TEMPORARY
            )
            user_text = (
                texts.STRIKE_APPEAL_APPROVED + "\n" + restriction_text
            )
            reply_markup = strike_appeal_keyboard()
        else:
            user_text = (
                texts.STRIKE_APPEAL_APPROVED + "\n" + texts.ACCESS_RESTORED
            )
    bot = callback.bot
    assert bot is not None
    try:
        await bot.send_message(
            user_id, user_text, reply_markup=reply_markup
        )
    except (ClientDecodeError, TelegramAPIError):
        logger.debug(
            "не удалось отправить пользователю результат апелляции",
            exc_info=True,
        )
    await _show_next_strike_appeal(chat_id, db, secret_panel)
    await callback.answer("Решение по апелляции сохранено.")
