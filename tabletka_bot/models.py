from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class IntakeInfo:
    intake_time_id: int
    med_name: str
    hhmm: str
    user_id: int
    chat_id: int


@dataclass(frozen=True)
class SessionInfo:
    session_id: int
    intake_time_id: int
    med_name: str
    hhmm: str
    user_id: int
    chat_id: int
    scheduled_utc: datetime
    acknowledged: bool
