from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

COMPLAINTS_PAGE_SIZE = 20


def panel_keyboard(
    mode: str | None,
    queue_count: int = 0,
    restrictions_count: int = 0,
    appeals_count: int = 0,
    notifications_enabled: bool = False,
) -> InlineKeyboardMarkup:
    mode_labels = {"manual": "Ручной", "ai": "AI", "off": "Off"}
    review_row: list[InlineKeyboardButton] = []
    if mode in {"manual", "ai"}:
        review_row.append(
            InlineKeyboardButton(
                text=f"Посты ({queue_count})",
                callback_data="sadmin:review:start",
            )
        )
    review_row.append(
        InlineKeyboardButton(
            text="Жалобы", callback_data="sadmin:complaints:list:0"
        )
    )
    rows = [
        review_row,
        [
            InlineKeyboardButton(
                text=f"Апелляции ({appeals_count})",
                callback_data="sadmin:appeals:next",
            ),
            InlineKeyboardButton(
                text=f"Ограничения ({restrictions_count})",
                callback_data="sadmin:restrictions:list",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"Режим: {mode_labels.get(mode or '', 'Ручной')}",
                callback_data="sadmin:mode:cycle",
            ),
            InlineKeyboardButton(
                text=(
                    "Уведомления: "
                    f"{'On' if notifications_enabled else 'Off'}"
                ),
                callback_data="sadmin:notifications:show",
            ),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Одобрить", callback_data="sadmin:review:approve"),
                InlineKeyboardButton(text="Отклонить", callback_data="sadmin:review:reject"),
            ],
            [
                InlineKeyboardButton(
                    text="Отклонить со страйком",
                    callback_data="sadmin:review:reject_strike",
                )
            ],
            [InlineKeyboardButton(text="Назад", callback_data="sadmin:back")],
        ]
    )


def empty_queue_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="sadmin:back")]]
    )


def complaints_list_keyboard(
    items: list[tuple[str, int]], offset: int = 0, has_next: bool = False
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{code} ({count})", callback_data=f"sadmin:complaints:open:{code}"
            )
        ]
        for code, count in items
    ]
    navigation = []
    if offset > 0:
        previous_offset = max(0, offset - COMPLAINTS_PAGE_SIZE)
        navigation.append(
            InlineKeyboardButton(
                text="Назад по списку",
                callback_data=f"sadmin:complaints:list:{previous_offset}",
            )
        )
    if has_next:
        next_offset = offset + COMPLAINTS_PAGE_SIZE
        navigation.append(
            InlineKeyboardButton(
                text="Вперёд по списку",
                callback_data=f"sadmin:complaints:list:{next_offset}",
            )
        )
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="Назад", callback_data="sadmin:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def complaint_detail_keyboard(code: str, count: int) -> InlineKeyboardMarkup:
    dismiss_label = "Отклонить жалобу" if count == 1 else "Отклонить жалобы"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Удалить из канала", callback_data=f"sadmin:complaints:delete:{code}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=dismiss_label, callback_data=f"sadmin:complaints:dismiss:{code}"
                )
            ],
            [InlineKeyboardButton(text="Назад", callback_data="sadmin:complaints:list:0")],
        ]
    )


def restrictions_keyboard(user_ids: list[int]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"Разблокировать {user_id}",
                callback_data=f"sadmin:restrictions:unblock:{user_id}",
            )
        ]
        for user_id in user_ids
    ]
    rows.append(
        [InlineKeyboardButton(text="Назад", callback_data="sadmin:back")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def notifications_keyboard(settings: dict[str, bool]) -> InlineKeyboardMarkup:
    def label(title: str, enabled: bool) -> str:
        return f"{title}: {'On' if enabled else 'Off'}"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label("Посты", settings.get("posts", False)),
                    callback_data="sadmin:notifications:toggle:posts",
                )
            ],
            [
                InlineKeyboardButton(
                    text=label(
                        "Жалобы", settings.get("complaints", False)
                    ),
                    callback_data="sadmin:notifications:toggle:complaints",
                )
            ],
            [
                InlineKeyboardButton(
                    text=label(
                        "Апелляции", settings.get("strike_appeals", False),
                    ),
                    callback_data="sadmin:notifications:toggle:strike_appeals",
                )
            ],
            [InlineKeyboardButton(text="Назад", callback_data="sadmin:back")],
        ]
    )


def strike_appeal_detail_keyboard(appeal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отменить страйк",
                    callback_data=(
                        f"sadmin:appeals:decision:cancel:{appeal_id}"
                    ),
                ),
                InlineKeyboardButton(
                    text="Разблокировать",
                    callback_data=(
                        f"sadmin:appeals:decision:unblock:{appeal_id}"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Отклонить",
                    callback_data=(
                        f"sadmin:appeals:decision:reject:{appeal_id}"
                    ),
                ),
                InlineKeyboardButton(
                    text="Назад", callback_data="sadmin:back"
                ),
            ],
        ]
    )
