import re
from dataclasses import dataclass
from time import monotonic


class InputError(ValueError):
    pass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class TokenBucketLimiter:
    def __init__(self, capacity: int, refill_seconds: int, notify_seconds: int = 60):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_seconds <= 0:
            raise ValueError("refill_seconds must be positive")
        if notify_seconds < 0:
            raise ValueError("notify_seconds must be non-negative")
        self.capacity = capacity
        self.refill_seconds = refill_seconds
        self.notify_seconds = notify_seconds
        self.buckets: dict[int, _Bucket] = {}
        self.notified: dict[int, float] = {}

    def allow(self, key: int, now: float | None = None) -> bool:
        moment = monotonic() if now is None else now
        bucket = self.buckets.setdefault(key, _Bucket(float(self.capacity), moment))
        elapsed = max(0.0, moment - bucket.updated)
        bucket.tokens = min(self.capacity, bucket.tokens + elapsed / self.refill_seconds)
        bucket.updated = moment
        if bucket.tokens < 1.0:
            return False
        bucket.tokens -= 1.0
        return True

    def should_notify(self, key: int, now: float | None = None) -> bool:
        moment = monotonic() if now is None else now
        previous = self.notified.get(key)
        if previous is not None and moment - previous < self.notify_seconds:
            return False
        self.notified[key] = moment
        return True

    def prune(self, now: float | None = None, idle_seconds: int = 600) -> None:
        moment = monotonic() if now is None else now
        cutoff = moment - idle_seconds
        self.buckets = {
            key: bucket
            for key, bucket in self.buckets.items()
            if bucket.updated >= cutoff
        }
        self.notified = {
            key: notified_at
            for key, notified_at in self.notified.items()
            if notified_at >= cutoff
        }

    @property
    def state_size(self) -> int:
        return len(self.buckets) + len(self.notified)


def normalize_medication_name(value: str) -> str:
    if len(value) > 200 or any(ord(character) < 32 for character in value):
        raise InputError("Название должно быть не длиннее 80 символов.")
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized or len(normalized) > 80:
        raise InputError("Название должно быть от 1 до 80 символов.")
    return normalized


def parse_callback_id(data: str, prefix: str) -> int | None:
    if len(data) > 64 or not data.startswith(prefix):
        return None
    raw = data[len(prefix):]
    if not raw.isascii() or not raw.isdigit():
        return None
    value = int(raw)
    return value if 0 < value <= 9_223_372_036_854_775_807 else None
