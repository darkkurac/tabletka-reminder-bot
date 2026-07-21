from collections.abc import Awaitable, Callable
from datetime import datetime

from telegram import Chat, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from tabletka_bot.database import CapacityError, Database, utc_now
from tabletka_bot.limits import (
    InputError,
    TokenBucketLimiter,
    normalize_medication_name,
    parse_callback_id,
)
from tabletka_bot.models import SessionInfo
from tabletka_bot.time_utils import human_eta, next_occurrence, parse_hhmm_flexible, today_occurrence

ASK_MED, ASK_TIME = range(2)
RescheduleCallback = Callable[[ContextTypes.DEFAULT_TYPE, int], Awaitable[None]]


def make_guard(limiter: TokenBucketLimiter):
    async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if chat is None or chat.type != Chat.PRIVATE:
            raise ApplicationHandlerStop
        if limiter.allow(chat.id):
            return
        if limiter.should_notify(chat.id):
            if update.callback_query is not None:
                await update.callback_query.answer(
                    "Слишком много запросов. Попробуйте немного позже.",
                    show_alert=True,
                )
            elif update.effective_message is not None:
                await update.effective_message.reply_text(
                    "Слишком много запросов. Попробуйте немного позже."
                )
        raise ApplicationHandlerStop

    return guard


def format_schedule(rows: list[tuple[str, str]]) -> str:
    if not rows:
        return "Пока ничего нет. Добавьте напоминание через /add."
    lines = ["Ваши напоминания:"]
    current_medication = None
    for name, hhmm in rows:
        if name != current_medication:
            current_medication = name
            lines.extend(["", f"• {name}:"])
        lines.append(f"  - {hhmm}")
    return "\n".join(lines) + "\n"


def remove_med_keyboard(medications: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(name, callback_data=f"rm_med:{med_id}")]
        for med_id, name in medications
    ]
    rows.append([InlineKeyboardButton("Закрыть", callback_data="rm_close")])
    return InlineKeyboardMarkup(rows)


def remove_times_keyboard(med_id: int, times: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"Удалить {hhmm}", callback_data=f"rm_time:{time_id}")]
        for time_id, hhmm in times
    ]
    rows.append(
        [InlineKeyboardButton("Удалить препарат целиком", callback_data=f"rm_all:{med_id}")]
    )
    rows.append(
        [
            InlineKeyboardButton("Назад", callback_data="rm_back"),
            InlineKeyboardButton("Закрыть", callback_data="rm_close"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def ack_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Подтвердить прием", callback_data=f"ack:{session_id}")]]
    )


def format_session_local(session: SessionInfo, timezone) -> str:
    return session.scheduled_utc.astimezone(timezone).strftime("%d.%m %H:%M")


class Handlers:
    ASK_MED = ASK_MED
    ASK_TIME = ASK_TIME

    def __init__(self, database: Database, timezone, reschedule_chat: RescheduleCallback):
        self.database = database
        self.timezone = timezone
        self.reschedule_chat = reschedule_chat

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = self.database.get_or_create_user(update.effective_chat.id)
        rows = self.database.get_user_schedule(user_id)
        context.user_data.pop("onboarding", None)
        if rows:
            await update.message.reply_text(
                "Бот уже готов напоминать о приемах.\n\n"
                f"{format_schedule(rows)}\n"
                "Команды: /add, /list, /today, /pending, /remove, /help",
                reply_markup=ReplyKeyboardRemove(),
            )
            return ConversationHandler.END
        await update.message.reply_text(
            "Привет! Я буду напоминать о таблетках.\n"
            "Давайте добавим первый препарат.\n\n"
            "Напишите название препарата, например: Витамин D.",
            reply_markup=ReplyKeyboardRemove(),
        )
        context.user_data["onboarding"] = True
        return ASK_MED

    async def ask_time(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            medication = normalize_medication_name(update.message.text)
        except InputError as exc:
            await update.message.reply_text(str(exc))
            return ASK_MED
        context.user_data["med_name"] = medication
        await update.message.reply_text(
            "Во сколько принимать? Напишите время в формате 24ч: HH:MM, например 08:30.\n"
            "Также подойдут: 8:30, 8.30, 8-30, 8 30."
        )
        return ASK_TIME

    async def save_pair(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        medication = context.user_data.get("med_name")
        if not medication:
            await update.message.reply_text(
                "Не вижу название препарата. Начните заново через /add."
            )
            return ConversationHandler.END
        hhmm = parse_hhmm_flexible(update.message.text)
        if not hhmm:
            await update.message.reply_text(
                "Не понял время. Пример: 08:30. Также можно 8.30, 8-30 или 8 30."
            )
            return ASK_TIME
        user_id = self.database.get_or_create_user(update.effective_chat.id)
        try:
            self.database.add_medication_time(user_id, medication, hhmm)
        except CapacityError as exc:
            await update.message.reply_text(str(exc))
            return ASK_TIME
        await update.message.reply_text(
            f"Готово. Добавлено: {medication} - {hhmm}.\n"
            "Еще одно время или препарат можно добавить через /add.\n"
            "Текущее расписание: /list"
        )
        await self.reschedule_chat(context, update.effective_chat.id)
        context.user_data.pop("onboarding", None)
        context.user_data.pop("med_name", None)
        return ConversationHandler.END

    async def add_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.pop("onboarding", None)
        await update.message.reply_text("Название препарата?")
        return ASK_MED

    async def list_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = self.database.get_or_create_user(update.effective_chat.id)
        await update.message.reply_text(format_schedule(self.database.get_user_schedule(user_id)))

    async def today_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = self.database.get_or_create_user(update.effective_chat.id)
        rows = self.database.get_user_schedule(user_id)
        if not rows:
            await update.message.reply_text(
                "На сегодня ничего нет. Добавьте напоминание через /add."
            )
            return
        now_local = datetime.now(self.timezone)
        items = []
        for name, hhmm in rows:
            occurrence = today_occurrence(now_local, hhmm)
            status = "прошло" if occurrence <= now_local else f"через {human_eta(occurrence - now_local)}"
            items.append((occurrence, f"{hhmm} - {name} ({status})"))
        text = "Сегодня:\n" + "\n".join(
            f"• {line}" for _, line in sorted(items, key=lambda item: item[0])
        )
        await update.message.reply_text(text)

    async def pending_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        pending = self.database.get_pending_sessions(update.effective_chat.id)
        if not pending:
            await update.message.reply_text("Неподтвержденных приемов сейчас нет.")
            return
        lines = []
        for session in pending:
            elapsed = human_eta(utc_now() - session.scheduled_utc)
            lines.append(
                f"• {session.med_name} - {format_session_local(session, self.timezone)} "
                f"({elapsed} назад)"
            )
        await update.message.reply_text("Неподтвержденные приемы:\n" + "\n".join(lines))

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Команды:\n"
            "/start - начать работу или показать текущее состояние\n"
            "/add - добавить препарат или время\n"
            "/list - показать расписание\n"
            "/today - показать приемы на сегодня\n"
            "/pending - показать неподтвержденные приемы\n"
            "/remove - удалить время или препарат\n"
            "/cancel - отменить текущее действие\n"
            "/help - помощь"
        )

    async def cancel_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        await update.message.reply_text("Отменено.")
        return ConversationHandler.END

    async def remove_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = self.database.get_or_create_user(update.effective_chat.id)
        medications = self.database.get_user_meds_with_ids(user_id)
        if not medications:
            await update.message.reply_text(
                "У вас пока нет препаратов. Добавьте через /add."
            )
            return
        await update.message.reply_text(
            "Выберите препарат для удаления:",
            reply_markup=remove_med_keyboard(medications),
        )

    async def remove_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data or ""
        if data not in {"rm_close", "rm_back"} and not any(
            data.startswith(prefix) for prefix in ("rm_med:", "rm_time:", "rm_all:")
        ):
            await query.answer("Некорректная кнопка.", show_alert=True)
            return
        identifier = None
        if data.startswith("rm_med:"):
            identifier = parse_callback_id(data, "rm_med:")
        elif data.startswith("rm_time:"):
            identifier = parse_callback_id(data, "rm_time:")
        elif data.startswith("rm_all:"):
            identifier = parse_callback_id(data, "rm_all:")
        if data not in {"rm_close", "rm_back"} and identifier is None:
            await query.answer("Некорректная кнопка.", show_alert=True)
            return
        await query.answer()
        chat_id = query.message.chat_id if query.message else update.effective_chat.id
        user_id = self.database.get_or_create_user(chat_id)
        if data == "rm_close":
            if query.message:
                await query.edit_message_reply_markup(reply_markup=None)
            return
        if data == "rm_back":
            medications = self.database.get_user_meds_with_ids(user_id)
            if not medications:
                await query.edit_message_text(
                    "У вас пока нет препаратов. Добавьте через /add."
                )
                return
            await query.edit_message_text(
                "Выберите препарат для удаления:",
                reply_markup=remove_med_keyboard(medications),
            )
            return
        if data.startswith("rm_med:"):
            medication = self.database.get_med_by_id(user_id, identifier)
            if not medication:
                await query.edit_message_text(
                    "Этот препарат уже удален или недоступен."
                )
                return
            _, medication_name = medication
            times = self.database.get_times_for_med_id(user_id, identifier)
            if not times:
                await query.edit_message_text(
                    f"У препарата {medication_name} больше нет времен приема."
                )
                return
            await query.edit_message_text(
                f"Препарат: {medication_name}\nВыберите, что удалить:",
                reply_markup=remove_times_keyboard(identifier, times),
            )
            return
        if data.startswith("rm_time:"):
            deleted = self.database.delete_intake_time(user_id, identifier)
            if not deleted:
                await query.edit_message_text("Это время уже удалено или недоступно.")
                return
            medication_name, hhmm, medication_id = deleted
            if self.database.count_times_for_med_id(user_id, medication_id) == 0:
                self.database.delete_medication(user_id, medication_id)
                result = (
                    f"Удалено время {hhmm}. У препарата {medication_name} больше нет времен, "
                    "поэтому препарат тоже удален."
                )
            else:
                result = f"Удалено время {hhmm} у препарата {medication_name}."
            await self.reschedule_chat(context, chat_id)
            await query.edit_message_text(result)
            return
        medication = self.database.get_med_by_id(user_id, identifier)
        if not medication:
            await query.edit_message_text("Этот препарат уже удален или недоступен.")
            return
        _, medication_name = medication
        self.database.delete_medication(user_id, identifier)
        await self.reschedule_chat(context, chat_id)
        await query.edit_message_text(
            f"Удален препарат {medication_name} со всеми временами."
        )

    async def button_ack(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data or ""
        session_id = parse_callback_id(data, "ack:")
        if session_id is None:
            await query.answer("Некорректная кнопка.", show_alert=True)
            return
        await query.answer()
        chat_id = query.message.chat_id if query.message else update.effective_chat.id
        session = self.database.session_by_id(session_id, chat_id)
        if session is None:
            if query.message:
                if hasattr(query, "edit_message_reply_markup"):
                    await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text("Это напоминание уже неактуально.")
            return
        if session.acknowledged:
            if query.message:
                if hasattr(query, "edit_message_reply_markup"):
                    await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text("Этот прием уже подтвержден.")
            return
        changed = self.database.acknowledge_session(session_id, chat_id)
        if not changed:
            if query.message:
                await query.message.reply_text("Этот прием уже подтвержден.")
            return
        if context.job_queue is not None:
            for job in context.job_queue.get_jobs_by_name(f"nag_{session_id}"):
                job.schedule_removal()
        times = self.database.get_all_times_for_med(session.user_id, session.med_name)
        now_local = datetime.now(self.timezone)
        if times:
            candidates = [(next_occurrence(now_local, hhmm), hhmm) for hhmm in times]
            next_datetime, next_hhmm = min(candidates, key=lambda item: item[0])
            reply = (
                f"Прием подтвержден. Следующий прием через {human_eta(next_datetime - now_local)} "
                f"(в {next_hhmm})."
            )
        else:
            reply = "Прием подтвержден. Для этого препарата больше нет времен приема."
        if query.message:
            if hasattr(query, "edit_message_reply_markup"):
                await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(reply)


def register_handlers(application: Application, handlers: Handlers) -> None:
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", handlers.start),
            CommandHandler("add", handlers.add_cmd),
        ],
        states={
            ASK_MED: [MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.ask_time)],
            ASK_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.save_pair)],
        },
        fallbacks=[
            CommandHandler("help", handlers.help_cmd),
            CommandHandler("cancel", handlers.cancel_cmd),
        ],
        name="add_flow",
        persistent=False,
    )
    application.add_handler(conversation)
    application.add_handler(CommandHandler("list", handlers.list_cmd))
    application.add_handler(CommandHandler("today", handlers.today_cmd))
    application.add_handler(CommandHandler("pending", handlers.pending_cmd))
    application.add_handler(CommandHandler("remove", handlers.remove_cmd))
    application.add_handler(CommandHandler("help", handlers.help_cmd))
    application.add_handler(CommandHandler("cancel", handlers.cancel_cmd))
    application.add_handler(CallbackQueryHandler(handlers.remove_callback, pattern=r"^rm_"))
    application.add_handler(CallbackQueryHandler(handlers.button_ack, pattern=r"^ack:"))
