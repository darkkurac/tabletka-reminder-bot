import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import AIORateLimiter, Application, ContextTypes, TypeHandler

from tabletka_bot.config import Settings
from tabletka_bot.database import Database
from tabletka_bot.handlers import Handlers, make_guard, register_handlers
from tabletka_bot.limits import TokenBucketLimiter
from tabletka_bot.scheduler import Scheduler

BASE_DIR = Path(__file__).resolve().parent.parent
log = logging.getLogger("medbot")


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    return Settings.from_env(BASE_DIR, os.environ)


def make_database(settings: Settings) -> Database:
    return Database(
        settings.db_path,
        settings.session_retention_days,
        settings.pending_restore_hours,
        settings.max_medications,
        settings.max_times_per_medication,
        settings.max_total_times,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if error is None:
        log.error("Unknown error during update handling")
        return
    log.error(
        "Error during update handling",
        exc_info=(type(error), error, error.__traceback__),
    )


def build_application(settings: Settings) -> Application:
    database = make_database(settings)
    database.init()
    limiter = TokenBucketLimiter(
        settings.rate_limit_capacity,
        settings.rate_limit_refill_seconds,
    )
    scheduler = Scheduler(
        database,
        settings.timezone,
        settings.nag_interval_minutes,
        limiter,
    )
    handlers = Handlers(database, settings.timezone, scheduler.reschedule_chat)
    outbound_limiter = AIORateLimiter(
        overall_max_rate=25,
        overall_time_period=1,
        group_max_rate=15,
        group_time_period=60,
    )
    application = (
        Application.builder()
        .token(settings.bot_token)
        .rate_limiter(outbound_limiter)
        .concurrent_updates(False)
        .post_init(scheduler.restore_jobs)
        .build()
    )
    application.bot_data["database"] = database
    application.bot_data["limiter"] = limiter
    application.bot_data["scheduler"] = scheduler
    application.add_handler(TypeHandler(Update, make_guard(limiter)), group=-1)
    register_handlers(application, handlers)
    application.add_error_handler(on_error)
    return application


def run_startup_check(settings: Settings) -> int:
    database = make_database(settings)
    database.init()
    integrity = database.integrity_check()
    if integrity != "ok":
        print(f"ERROR: целостность базы {integrity}")
        return 1
    counts = database.counts()
    print("OK: конфигурация прочитана")
    print(f"OK: таймзона {settings.timezone_name}")
    print(f"OK: база {settings.db_path}")
    print(f"OK: целостность базы {integrity}")
    print(
        "OK: записи в БД "
        f"users={counts['users']}, medications={counts['medications']}, "
        f"intake_times={counts['intake_times']}, sessions={counts['sessions']}"
    )
    print(f"OK: повторные напоминания каждые {settings.nag_interval_minutes} мин")
    return 0


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Telegram medication reminder bot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="check config and database without polling",
    )
    args = parser.parse_args()
    try:
        settings = load_settings()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1) from exc
    if args.check:
        raise SystemExit(run_startup_check(settings))
    application = build_application(settings)
    application.run_polling(
        allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY],
    )
