import logging
from collections.abc import Callable
from datetime import datetime, time, timedelta

import pytz
from telegram.error import Forbidden
from telegram.ext import ContextTypes

from tabletka_bot.database import Database
from tabletka_bot.handlers import ack_keyboard
from tabletka_bot.limits import TokenBucketLimiter
from tabletka_bot.time_utils import today_occurrence

log = logging.getLogger("medbot.scheduler")


class Scheduler:
    def __init__(
        self,
        database: Database,
        timezone,
        nag_interval_minutes: int,
        limiter: TokenBucketLimiter,
        now_local: Callable[[], datetime] | None = None,
    ):
        self.database = database
        self.timezone = timezone
        self.nag_interval_minutes = nag_interval_minutes
        self.limiter = limiter
        self.now_local = now_local or (lambda: datetime.now(self.timezone))

    @staticmethod
    def _job_queue(source):
        if source.job_queue is None:
            raise RuntimeError("JobQueue не инициализирован.")
        return source.job_queue

    def schedule_nag_job(
        self,
        source,
        chat_id: int,
        session_id: int,
        first: timedelta,
    ) -> None:
        job_queue = self._job_queue(source)
        name = f"nag_{session_id}"
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()
        job_queue.run_repeating(
            callback=self.nag_user,
            interval=timedelta(minutes=self.nag_interval_minutes),
            first=first,
            data={"chat_id": chat_id, "session_id": session_id},
            name=name,
        )

    async def reschedule_chat(self, source, chat_id: int) -> None:
        job_queue = self._job_queue(source)
        prefix = f"daily_{chat_id}_"
        for job in job_queue.jobs():
            if job.name and job.name.startswith(prefix):
                job.schedule_removal()
        rows = self.database.daily_intakes_for_chat(chat_id)
        for intake_time_id, hhmm in rows:
            hour, minute = map(int, hhmm.split(":"))
            job_queue.run_daily(
                callback=self.trigger_intake,
                time=time(hour=hour, minute=minute, tzinfo=self.timezone),
                data={"chat_id": chat_id, "intake_time_id": intake_time_id},
                name=f"daily_{chat_id}_{intake_time_id}",
            )
        log.info("Rescheduled %d daily job(s) for chat %s", len(rows), chat_id)

    async def trigger_intake(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = int(context.job.data["chat_id"])
        intake_time_id = int(context.job.data["intake_time_id"])
        intake = self.database.intake_by_id(intake_time_id)
        if intake is None or intake.chat_id != chat_id:
            context.job.schedule_removal()
            log.warning("Removed invalid daily job for chat %s intake %s", chat_id, intake_time_id)
            return
        scheduled_local = today_occurrence(self.now_local(), intake.hhmm)
        scheduled_utc = scheduled_local.astimezone(pytz.utc)
        session_id, created = self.database.create_session(
            intake_time_id,
            scheduled_utc,
            chat_id,
        )
        if not created:
            return
        self.schedule_nag_job(
            context,
            chat_id,
            session_id,
            first=timedelta(minutes=self.nag_interval_minutes),
        )
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Пора принять: {intake.med_name} (время {intake.hhmm}).",
                reply_markup=ack_keyboard(session_id),
            )
        except Forbidden:
            for job in context.job_queue.get_jobs_by_name(f"nag_{session_id}"):
                job.schedule_removal()
            self.database.delete_session(session_id, chat_id)
            context.job.schedule_removal()
            log.warning("Removed unreachable daily job for chat %s", chat_id)
            return

    async def nag_user(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = int(context.job.data["chat_id"])
        session_id = int(context.job.data["session_id"])
        session = self.database.session_by_id(session_id, chat_id)
        if session is None or session.acknowledged:
            context.job.schedule_removal()
            return
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "Прием еще не подтвержден.\n"
                    f"Препарат: {session.med_name}.\n"
                    "Нажмите кнопку после приема."
                ),
                reply_markup=ack_keyboard(session_id),
            )
        except Forbidden:
            self.database.delete_session(session_id, chat_id)
            context.job.schedule_removal()
            log.warning("Removed unreachable nag job for chat %s session %s", chat_id, session_id)
            return
        self.database.mark_session_nagged(session_id, chat_id)

    async def periodic_cleanup(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        removed = self.database.cleanup()
        self.limiter.prune()
        log.info(
            "Maintenance removed sessions=%s medications=%s users=%s",
            removed["sessions"],
            removed["medications"],
            removed["users"],
        )

    async def restore_jobs(self, application) -> None:
        job_queue = self._job_queue(application)
        for chat_id in self.database.all_chat_ids():
            await self.reschedule_chat(application, chat_id)
        for session in self.database.get_all_pending_sessions():
            self.schedule_nag_job(
                application,
                session.chat_id,
                session.session_id,
                first=timedelta(seconds=10),
            )
        for job in job_queue.get_jobs_by_name("maintenance_cleanup"):
            job.schedule_removal()
        job_queue.run_repeating(
            callback=self.periodic_cleanup,
            interval=timedelta(days=1),
            first=timedelta(hours=1),
            name="maintenance_cleanup",
        )
