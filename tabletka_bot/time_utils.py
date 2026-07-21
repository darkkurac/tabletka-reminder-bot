import re
from datetime import datetime, time, timedelta

import pytz

TIME_RX = re.compile(r"^\s*(\d{1,2})\D(\d{1,2})\s*$")


def parse_hhmm_flexible(value: str) -> str | None:
    if len(value) > 16:
        return None
    stripped = value.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", stripped):
        hour, minute = map(int, stripped.split(":"))
    else:
        match = TIME_RX.match(stripped)
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour < 24 and 0 <= minute < 60:
        return f"{hour:02d}:{minute:02d}"
    return None


def _wall_time(now_local: datetime, hhmm: str, day) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    naive = datetime.combine(day, time(hour=hour, minute=minute))
    zone_name = getattr(now_local.tzinfo, "zone", None)
    if zone_name is None:
        return naive.replace(tzinfo=now_local.tzinfo)
    zone = pytz.timezone(zone_name)
    for _ in range(181):
        try:
            return zone.localize(naive, is_dst=None)
        except pytz.AmbiguousTimeError:
            return zone.localize(naive, is_dst=False)
        except pytz.NonExistentTimeError:
            naive += timedelta(minutes=1)
    raise RuntimeError("Не удалось определить локальное время приёма.")


def next_occurrence(now_local: datetime, hhmm: str) -> datetime:
    candidate = _wall_time(now_local, hhmm, now_local.date())
    if candidate <= now_local:
        candidate = _wall_time(now_local, hhmm, now_local.date() + timedelta(days=1))
    return candidate


def today_occurrence(now_local: datetime, hhmm: str) -> datetime:
    return _wall_time(now_local, hhmm, now_local.date())


def human_eta(delta: timedelta) -> str:
    total_minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"
