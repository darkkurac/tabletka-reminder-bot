import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytz

TOKEN_RX = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")


def _bounded_int(environ: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} должен быть целым числом, сейчас: {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} должен быть от {minimum} до {maximum}, сейчас: {value}")
    return value


@dataclass(frozen=True)
class Settings:
    bot_token: str
    timezone_name: str
    timezone: object
    db_path: Path
    nag_interval_minutes: int
    session_retention_days: int
    pending_restore_hours: int
    max_medications: int
    max_times_per_medication: int
    max_total_times: int
    rate_limit_capacity: int
    rate_limit_refill_seconds: int

    @classmethod
    def from_env(cls, base_dir: Path, environ: Mapping[str, str]) -> "Settings":
        token = environ.get("BOT_TOKEN", "")
        if not TOKEN_RX.fullmatch(token):
            raise RuntimeError("BOT_TOKEN выглядит некорректно. Укажите реальный токен из BotFather.")
        timezone_name = environ.get("TZ", "Asia/Krasnoyarsk")
        try:
            timezone = pytz.timezone(timezone_name)
        except pytz.UnknownTimeZoneError as exc:
            raise RuntimeError(f"Неверная таймзона TZ={timezone_name!r}.") from exc
        db_path = Path(environ.get("DB_PATH", "meds.sqlite3")).expanduser()
        if not db_path.is_absolute():
            db_path = base_dir / db_path
        return cls(
            bot_token=token,
            timezone_name=timezone_name,
            timezone=timezone,
            db_path=db_path,
            nag_interval_minutes=_bounded_int(environ, "NAG_INTERVAL_MINUTES", 30, 1, 1440),
            session_retention_days=_bounded_int(environ, "SESSION_RETENTION_DAYS", 30, 1, 3650),
            pending_restore_hours=_bounded_int(environ, "PENDING_RESTORE_HOURS", 24, 1, 336),
            max_medications=50,
            max_times_per_medication=10,
            max_total_times=50,
            rate_limit_capacity=_bounded_int(environ, "RATE_LIMIT_CAPACITY", 8, 1, 100),
            rate_limit_refill_seconds=_bounded_int(environ, "RATE_LIMIT_REFILL_SECONDS", 3, 1, 60),
        )
