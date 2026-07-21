import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytz

from tabletka_bot.models import IntakeInfo, SessionInfo

SCHEMA_VERSION = 3


class CapacityError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(pytz.utc)


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = pytz.utc.localize(value)
    return value.astimezone(pytz.utc).isoformat()


def parse_db_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return pytz.utc.localize(parsed)
    return parsed.astimezone(pytz.utc)


class Database:
    def __init__(
        self,
        path: Path,
        session_retention_days: int,
        pending_restore_hours: int,
        max_medications: int,
        max_times_per_medication: int,
        max_total_times: int = 50,
    ):
        self.path = path
        self.session_retention_days = session_retention_days
        self.pending_restore_hours = pending_restore_hours
        self.max_medications = max_medications
        self.max_times_per_medication = max_times_per_medication
        self.max_total_times = max_total_times

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return any(row["name"] == column for row in rows)

    def init(self) -> None:
        with self.connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS users("
                "id INTEGER PRIMARY KEY, chat_id INTEGER UNIQUE NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS medications("
                "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, "
                "UNIQUE(user_id, name), "
                "FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS intake_times("
                "id INTEGER PRIMARY KEY, medication_id INTEGER NOT NULL, hhmm TEXT NOT NULL, "
                "UNIQUE(medication_id, hhmm), "
                "FOREIGN KEY(medication_id) REFERENCES medications(id) ON DELETE CASCADE)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS sessions("
                "id INTEGER PRIMARY KEY, intake_time_id INTEGER NOT NULL, scheduled_utc TEXT NOT NULL, "
                "acknowledged INTEGER NOT NULL DEFAULT 0, created_at_utc TEXT, acknowledged_at_utc TEXT, "
                "last_nag_utc TEXT, chat_id INTEGER, "
                "FOREIGN KEY(intake_time_id) REFERENCES intake_times(id) ON DELETE CASCADE)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            columns = {
                "created_at_utc": "TEXT",
                "acknowledged_at_utc": "TEXT",
                "last_nag_utc": "TEXT",
                "chat_id": "INTEGER",
            }
            for column, definition in columns.items():
                if not self._has_column(connection, "sessions", column):
                    connection.execute(f"ALTER TABLE sessions ADD COLUMN {column} {definition}")
            connection.execute(
                "UPDATE sessions SET created_at_utc=scheduled_utc WHERE created_at_utc IS NULL"
            )
            connection.execute(
                "UPDATE sessions SET chat_id=("
                "SELECT u.chat_id FROM intake_times t "
                "JOIN medications m ON m.id=t.medication_id "
                "JOIN users u ON u.id=m.user_id "
                "WHERE t.id=sessions.intake_time_id) "
                "WHERE chat_id IS NULL"
            )
            connection.execute(
                "DELETE FROM sessions WHERE id NOT IN ("
                "SELECT MIN(id) FROM sessions GROUP BY intake_time_id, scheduled_utc)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_unique_schedule "
                "ON sessions(intake_time_id, scheduled_utc)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_medications_user_name ON medications(user_id, name)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_intake_times_med_hhmm ON intake_times(medication_id, hhmm)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_pending ON sessions(acknowledged, scheduled_utc)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_chat ON sessions(chat_id)"
            )
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def integrity_check(self) -> str:
        with self.connection() as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            return str(row[0])

    def get_or_create_user(self, chat_id: int) -> int:
        with self.connection() as connection:
            connection.execute("INSERT OR IGNORE INTO users(chat_id) VALUES(?)", (chat_id,))
            row = connection.execute("SELECT id FROM users WHERE chat_id=?", (chat_id,)).fetchone()
            return int(row[0])

    def get_user_id_by_chat(self, chat_id: int) -> int | None:
        with self.connection() as connection:
            row = connection.execute("SELECT id FROM users WHERE chat_id=?", (chat_id,)).fetchone()
            return int(row[0]) if row else None

    def add_medication_time(self, user_id: int, med_name: str, hhmm: str) -> tuple[int, int]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            medication = connection.execute(
                "SELECT id FROM medications WHERE user_id=? AND name=?",
                (user_id, med_name),
            ).fetchone()
            medication_id = int(medication[0]) if medication is not None else None
            if medication_id is not None:
                intake = connection.execute(
                    "SELECT id FROM intake_times WHERE medication_id=? AND hhmm=?",
                    (medication_id, hhmm),
                ).fetchone()
                if intake is not None:
                    return medication_id, int(intake[0])
            if medication is None:
                count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM medications WHERE user_id=?",
                        (user_id,),
                    ).fetchone()[0]
                )
                if count >= self.max_medications:
                    raise CapacityError(f"Можно добавить не больше {self.max_medications} препаратов.")
            total_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM intake_times t "
                    "JOIN medications m ON m.id=t.medication_id WHERE m.user_id=?",
                    (user_id,),
                ).fetchone()[0]
            )
            if total_count >= self.max_total_times:
                raise CapacityError(
                    f"Можно добавить не больше {self.max_total_times} времён приёма."
                )
            if medication_id is None:
                cursor = connection.execute(
                    "INSERT INTO medications(user_id, name) VALUES(?,?)",
                    (user_id, med_name),
                )
                medication_id = int(cursor.lastrowid)
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM intake_times WHERE medication_id=?",
                    (medication_id,),
                ).fetchone()[0]
            )
            if count >= self.max_times_per_medication:
                raise CapacityError(
                    f"Для одного препарата можно добавить не больше {self.max_times_per_medication} времён."
                )
            cursor = connection.execute(
                "INSERT INTO intake_times(medication_id, hhmm) VALUES(?,?)",
                (medication_id, hhmm),
            )
            return medication_id, int(cursor.lastrowid)

    def get_user_schedule(self, user_id: int) -> list[tuple[str, str]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT m.name, t.hhmm FROM medications m "
                "JOIN intake_times t ON t.medication_id=m.id "
                "WHERE m.user_id=? ORDER BY m.name COLLATE NOCASE, t.hhmm",
                (user_id,),
            ).fetchall()
            return [(str(row[0]), str(row[1])) for row in rows]

    def get_user_meds_with_ids(self, user_id: int) -> list[tuple[int, str]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id, name FROM medications WHERE user_id=? ORDER BY name COLLATE NOCASE",
                (user_id,),
            ).fetchall()
            return [(int(row[0]), str(row[1])) for row in rows]

    def get_med_by_id(self, user_id: int, med_id: int) -> tuple[int, str] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, name FROM medications WHERE user_id=? AND id=?",
                (user_id, med_id),
            ).fetchone()
            return (int(row[0]), str(row[1])) if row else None

    def get_times_for_med_id(self, user_id: int, med_id: int) -> list[tuple[int, str]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT t.id, t.hhmm FROM intake_times t "
                "JOIN medications m ON m.id=t.medication_id "
                "WHERE m.user_id=? AND m.id=? ORDER BY t.hhmm",
                (user_id, med_id),
            ).fetchall()
            return [(int(row[0]), str(row[1])) for row in rows]

    def get_all_times_for_med(self, user_id: int, med_name: str) -> list[str]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT t.hhmm FROM medications m "
                "JOIN intake_times t ON t.medication_id=m.id "
                "WHERE m.user_id=? AND m.name=? ORDER BY t.hhmm",
                (user_id, med_name),
            ).fetchall()
            return [str(row[0]) for row in rows]

    def create_session(
        self,
        intake_time_id: int,
        scheduled_utc: datetime,
        chat_id: int,
    ) -> tuple[int, bool]:
        scheduled = utc_iso(scheduled_utc)
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO sessions("
                "intake_time_id, scheduled_utc, acknowledged, created_at_utc, chat_id) "
                "VALUES(?,?,0,?,?)",
                (intake_time_id, scheduled, utc_iso(utc_now()), chat_id),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                "SELECT id FROM sessions WHERE intake_time_id=? AND scheduled_utc=?",
                (intake_time_id, scheduled),
            ).fetchone()
            if row is None:
                raise RuntimeError("Не удалось создать сеанс приёма.")
            return int(row[0]), created

    def acknowledge_session(self, session_id: int, chat_id: int) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET acknowledged=1, acknowledged_at_utc=? "
                "WHERE id=? AND chat_id=? AND acknowledged=0",
                (utc_iso(utc_now()), session_id, chat_id),
            )
            return cursor.rowcount == 1

    def mark_session_nagged(self, session_id: int, chat_id: int) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET last_nag_utc=? WHERE id=? AND chat_id=? AND acknowledged=0",
                (utc_iso(utc_now()), session_id, chat_id),
            )
            return cursor.rowcount == 1

    def delete_session(self, session_id: int, chat_id: int) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE id=? AND chat_id=?",
                (session_id, chat_id),
            )
            return cursor.rowcount == 1

    def intake_by_id(self, intake_time_id: int) -> IntakeInfo | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT t.id, m.name, t.hhmm, m.user_id, u.chat_id "
                "FROM intake_times t "
                "JOIN medications m ON m.id=t.medication_id "
                "JOIN users u ON u.id=m.user_id WHERE t.id=?",
                (intake_time_id,),
            ).fetchone()
            if row is None:
                return None
            return IntakeInfo(
                intake_time_id=int(row[0]),
                med_name=str(row[1]),
                hhmm=str(row[2]),
                user_id=int(row[3]),
                chat_id=int(row[4]),
            )

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> SessionInfo:
        scheduled = parse_db_datetime(str(row[6]))
        if scheduled is None:
            raise RuntimeError("В базе отсутствует время сеанса.")
        return SessionInfo(
            session_id=int(row[0]),
            intake_time_id=int(row[1]),
            med_name=str(row[2]),
            hhmm=str(row[3]),
            user_id=int(row[4]),
            chat_id=int(row[5]),
            scheduled_utc=scheduled,
            acknowledged=bool(row[7]),
        )

    def session_by_id(self, session_id: int, chat_id: int | None = None) -> SessionInfo | None:
        query = (
            "SELECT s.id, s.intake_time_id, m.name, t.hhmm, m.user_id, "
            "COALESCE(s.chat_id, u.chat_id), s.scheduled_utc, s.acknowledged "
            "FROM sessions s "
            "JOIN intake_times t ON t.id=s.intake_time_id "
            "JOIN medications m ON m.id=t.medication_id "
            "JOIN users u ON u.id=m.user_id WHERE s.id=?"
        )
        params: tuple[int, ...] = (session_id,)
        if chat_id is not None:
            query += " AND COALESCE(s.chat_id, u.chat_id)=?"
            params = (session_id, chat_id)
        with self.connection() as connection:
            row = connection.execute(query, params).fetchone()
            return self._session_from_row(row) if row else None

    def _pending_sessions(self, chat_id: int | None, now_utc: datetime | None) -> list[SessionInfo]:
        cutoff = (now_utc or utc_now()) - timedelta(hours=self.pending_restore_hours)
        query = (
            "SELECT s.id, s.intake_time_id, m.name, t.hhmm, m.user_id, "
            "COALESCE(s.chat_id, u.chat_id), s.scheduled_utc, s.acknowledged "
            "FROM sessions s "
            "JOIN intake_times t ON t.id=s.intake_time_id "
            "JOIN medications m ON m.id=t.medication_id "
            "JOIN users u ON u.id=m.user_id "
            "WHERE s.acknowledged=0 AND s.scheduled_utc>=?"
        )
        params: tuple[object, ...] = (utc_iso(cutoff),)
        if chat_id is not None:
            query += " AND COALESCE(s.chat_id, u.chat_id)=?"
            params = (utc_iso(cutoff), chat_id)
        query += " ORDER BY s.scheduled_utc"
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
            return [self._session_from_row(row) for row in rows]

    def get_pending_sessions(
        self,
        chat_id: int,
        now_utc: datetime | None = None,
    ) -> list[SessionInfo]:
        return self._pending_sessions(chat_id, now_utc)

    def get_all_pending_sessions(self, now_utc: datetime | None = None) -> list[SessionInfo]:
        return self._pending_sessions(None, now_utc)

    def delete_intake_time(self, user_id: int, intake_time_id: int) -> tuple[str, str, int] | None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT m.name, t.hhmm, m.id FROM intake_times t "
                "JOIN medications m ON m.id=t.medication_id "
                "WHERE m.user_id=? AND t.id=?",
                (user_id, intake_time_id),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "DELETE FROM intake_times WHERE id=? AND medication_id IN ("
                "SELECT id FROM medications WHERE user_id=?)",
                (intake_time_id, user_id),
            )
            return str(row[0]), str(row[1]), int(row[2])

    def delete_medication(self, user_id: int, med_id: int) -> str | None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT name FROM medications WHERE user_id=? AND id=?",
                (user_id, med_id),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "DELETE FROM medications WHERE user_id=? AND id=?",
                (user_id, med_id),
            )
            return str(row[0])

    def count_times_for_med_id(self, user_id: int, med_id: int) -> int:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM intake_times t "
                "JOIN medications m ON m.id=t.medication_id "
                "WHERE m.user_id=? AND m.id=?",
                (user_id, med_id),
            ).fetchone()
            return int(row[0])

    def daily_intakes_for_chat(self, chat_id: int) -> list[tuple[int, str]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT t.id, t.hhmm FROM users u "
                "JOIN medications m ON m.user_id=u.id "
                "JOIN intake_times t ON t.medication_id=m.id "
                "WHERE u.chat_id=?",
                (chat_id,),
            ).fetchall()
            return [(int(row[0]), str(row[1])) for row in rows]

    def all_chat_ids(self) -> list[int]:
        with self.connection() as connection:
            rows = connection.execute("SELECT chat_id FROM users ORDER BY id").fetchall()
            return [int(row[0]) for row in rows]

    def cleanup(self, now_utc: datetime | None = None) -> dict[str, int]:
        cutoff = (now_utc or utc_now()) - timedelta(days=self.session_retention_days)
        with self.connection() as connection:
            removed: dict[str, int] = {}
            cursor = connection.execute(
                "DELETE FROM sessions WHERE scheduled_utc<?",
                (utc_iso(cutoff),),
            )
            removed["sessions"] = cursor.rowcount
            cursor = connection.execute(
                "DELETE FROM medications WHERE NOT EXISTS ("
                "SELECT 1 FROM intake_times WHERE intake_times.medication_id=medications.id)"
            )
            removed["medications"] = cursor.rowcount
            cursor = connection.execute(
                "DELETE FROM users WHERE NOT EXISTS ("
                "SELECT 1 FROM medications WHERE medications.user_id=users.id)"
            )
            removed["users"] = cursor.rowcount
            return removed

    def counts(self) -> dict[str, int]:
        with self.connection() as connection:
            return {
                "users": int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]),
                "medications": int(
                    connection.execute("SELECT COUNT(*) FROM medications").fetchone()[0]
                ),
                "intake_times": int(
                    connection.execute("SELECT COUNT(*) FROM intake_times").fetchone()[0]
                ),
                "sessions": int(
                    connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                ),
            }
