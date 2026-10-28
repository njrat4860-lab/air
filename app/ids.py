import re
import secrets
import string

from app.db import Database

_LETTERS = string.ascii_uppercase
_DIGITS = string.digits
_CODE_RE = re.compile(r"^[A-Z]{3}\d{3}$")


def is_valid_code(code: object) -> bool:
    return isinstance(code, str) and _CODE_RE.fullmatch(code) is not None


def _random_code() -> str:
    letters = "".join(secrets.choice(_LETTERS) for _ in range(3))
    digits = "".join(secrets.choice(_DIGITS) for _ in range(3))
    return f"{letters}{digits}"


async def generate_unique_code(db: Database) -> str:
    for _ in range(50):
        code = _random_code()
        async with await db.conn.execute(
            "SELECT 1 FROM posts WHERE public_code = ?", (code,)
        ) as cur:
            exists = await cur.fetchone()
        if not exists:
            return code
    raise RuntimeError("не удалось подобрать свободный код поста")
