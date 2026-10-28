import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _TOKEN_RE.sub("<redacted>", super().format(record))


def setup_logging(log_path: Path) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = _RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError:
        logging.getLogger(__name__).warning(
            "Не удалось открыть файл лога %s; используется только stdout",
            log_path,
            exc_info=True,
        )
    else:
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    logging.getLogger("aiogram").setLevel(logging.WARNING)
