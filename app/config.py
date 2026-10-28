import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_WORD_MIN_LEN = 12


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    channel_id: int
    db_path: Path
    log_path: Path
    secret_word: str = field(repr=False)
    admin_ids: frozenset[int] = frozenset()
    secret_session_minutes: int = 10
    complaint_min_len: int = 5
    complaint_max_len: int = 500


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


def _validate_bot_token(token: str) -> None:
    bot_id, separator, secret_part = token.partition(":")
    if not separator or not bot_id.isdigit() or len(secret_part) < 20:
        raise RuntimeError("BOT_TOKEN имеет неверный формат")


def _parse_admin_ids(raw: str) -> frozenset[int]:
    if not raw:
        return frozenset()
    admin_ids = set()
    for part in raw.replace(";", ",").split(","):
        value = part.strip()
        if not value:
            continue
        try:
            admin_id = int(value)
        except ValueError:
            raise RuntimeError(
                "ADMIN_IDS должен содержать целые Telegram ID через запятую"
            ) from None
        if admin_id <= 0:
            raise RuntimeError("ADMIN_IDS должен содержать положительные Telegram ID")
        admin_ids.add(admin_id)
    return frozenset(admin_ids)


def load_config() -> Config:
    token = _required_env("BOT_TOKEN")
    channel_raw = _required_env("CHANNEL_ID")
    secret_word = _required_env("SECRET_WORD")

    _validate_bot_token(token)
    if len(secret_word) < SECRET_WORD_MIN_LEN:
        raise RuntimeError(
            f"SECRET_WORD должен содержать не менее {SECRET_WORD_MIN_LEN} символов"
        )
    try:
        channel_id = int(channel_raw)
    except ValueError:
        raise RuntimeError("CHANNEL_ID должен быть целым числом") from None
    if channel_id == 0:
        raise RuntimeError("CHANNEL_ID не должен быть равен 0")
    admin_ids = _parse_admin_ids(os.getenv("ADMIN_IDS", "").strip())

    data_dir_raw = os.getenv("DATA_DIR", "").strip()
    if data_dir_raw:
        data_dir = Path(data_dir_raw).expanduser()
        db_path = data_dir / "bot.db"
        log_path = data_dir / "logs" / "bot.log"
    else:
        db_path = BASE_DIR / "data" / "bot.db"
        log_path = BASE_DIR / "logs" / "bot.log"

    return Config(
        bot_token=token,
        channel_id=channel_id,
        db_path=db_path,
        log_path=log_path,
        secret_word=secret_word,
        admin_ids=admin_ids,
    )
