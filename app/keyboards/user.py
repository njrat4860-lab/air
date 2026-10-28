from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сделать пост", callback_data="menu:gossip")],
            [InlineKeyboardButton(text="Жалоба на пост", callback_data="menu:complaint")],
        ]
    )


def complaint_button(code: str, count: int) -> InlineKeyboardMarkup:
    label = f"🚩 Жалоба · {code}" if count <= 0 else f"🚩 Жалоба · {code} · {count}"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=f"complain:{code}")]]
    )


def strike_appeal_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Обжаловать ограничение",
                    callback_data="strike_appeal:start",
                )
            ]
        ]
    )


def strike_appeal_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отмена", callback_data="strike_appeal:cancel"
                )
            ]
        ]
    )


def strike_appeal_pending_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отозвать обжалование",
                    callback_data="strike_appeal:withdraw",
                )
            ]
        ]
    )
