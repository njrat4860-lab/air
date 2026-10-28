from html import escape


def b(s: str) -> str:
    return f"<b>{s}</b>"


def bold_user_text(s: str) -> str:
    return f"<b>{escape(s)}</b>"


def telegram_text_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


WELCOME = b(
    "Привет! 👋\n\n"
    "Выбери действие👇"
)

MENU_PROMPT = b("Выбери действие👇")

ASK_POST_CONTENT = b("Скидывай текст, фото или видео одним сообщением 👇")

POST_ACCEPTED_QUEUE = b("На проверке.")
POST_PUBLISHED = b("Пост опубликован 🚀")
POST_REJECTED = b("Пост не опубликован 🙅")
POST_EMPTY = b("Пустой пост не принимается.")
POST_TOO_LONG = b("Слишком длинный текст. Сократи пост.")
CAPTION_TOO_LONG = b("Слишком длинная подпись. Сократи подпись.")
POST_SEND_FAILED = b("Не удалось отправить пост. Попробуй ещё раз.")
POST_DUPLICATE = b("Такой пост уже отправлен.")
POST_REJECTED_SPAM = b("Пост отклонён как спам.")
TOO_FREQUENT = b("Слишком часто. Подожди немного.")

ASK_COMPLAINT_CODE = b(
    "Введи ID поста, "
    "Пример: RKF671:"
)
COMPLAINT_CODE_NOT_FOUND = b("Некорректный ID")
COMPLAINT_CODE_NOT_FOUND_PLAIN = "Некорректный ID"
ASK_COMPLAINT_TEXT = b("Причина?")
COMPLAINT_TOO_SHORT = b("Мало текста.")
COMPLAINT_TOO_LONG = b("Слишком длинный текст жалобы.")
COMPLAINT_REJECTED_PROFANITY = b("В жалобе нельзя материться.")
COMPLAINT_REJECTED_SPAM = b("Жалоба отклонена.")
COMPLAINT_DUPLICATE = b("Повторные жалобы не принимаются.")
COMPLAINT_ACCEPTED = b("Жалоба принята ✅.")
COMPLAINT_START_BOT_FIRST = b("Сначала напишите /start.")
COMPLAINT_START_BOT_FIRST_PLAIN = "Сначала напишите /start."

ERROR_GENERIC = b("Что-то не задалось, попробуй ещё раз. Код ошибки: {code}")
ERROR_GENERIC_PLAIN = "Что-то не задалось, попробуй ещё раз. Код ошибки: {code}"

POST_REJECTED_TARGETED_INSULT = b(
    "Пост отклонён: прямое оскорбление конкретного человека."
)
ACCESS_RESTRICTED_TEMPORARY = b("Доступ к боту временно ограничен.")
ACCESS_RESTRICTED_UNTIL_ADMIN = b(
    "Доступ к боту ограничен до решения администратора."
)
STRIKE_APPEAL_PROMPT = b("Напиши причину обжалования одним сообщением.")
STRIKE_APPEAL_ACCEPTED = b("Обжалование принято.")
STRIKE_APPEAL_WITHDRAWN = b("Обжалование отозвано.")
STRIKE_APPEAL_ALREADY_PENDING = b("Обжалование уже на проверке.")
STRIKE_APPEAL_UNAVAILABLE = b("Обжалование недоступно.")
STRIKE_APPEAL_TOO_SHORT = b("Мало текста обжалования.")
STRIKE_APPEAL_TOO_LONG = b("Слишком длинный текст обжалования.")
STRIKE_APPEAL_REJECTED = b("Обжалование отклонено.")
STRIKE_APPEAL_APPROVED = b("Обжалование одобрено.")
ACCESS_RESTORED = b("Доступ к боту восстановлен.")
ADMIN_NOTIFICATION_DIGEST_TITLE = b("Новые события:")
ADMIN_NOTIFICATION_DIGEST_POSTS = "Посты на проверку: {count}"
ADMIN_NOTIFICATION_DIGEST_COMPLAINTS = "Жалобы на посты: {count}"
ADMIN_NOTIFICATION_DIGEST_STRIKE_APPEALS = "Апелляции на страйк: {count}"
GLOBAL_SUBMISSION_LIMIT = b(
    "Сейчас слишком много отправок. Попробуйте через {minutes} мин."
)
