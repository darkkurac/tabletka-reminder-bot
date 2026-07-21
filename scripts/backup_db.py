import argparse
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def backup_database(
    source: Path,
    destination_dir: Path,
    retention_days: int,
    now: datetime,
) -> Path:
    if not source.is_file():
        raise FileNotFoundError(source)
    if not 1 <= retention_days <= 3650:
        raise ValueError("retention_days must be between 1 and 3650")
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination_dir.chmod(0o700)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_id = f"{stamp}-{uuid4().hex}"
    temporary = destination_dir / f".meds-{backup_id}.sqlite3.tmp"
    final = destination_dir / f"meds-{backup_id}.sqlite3"
    temporary.unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(source)) as source_connection:
            with closing(sqlite3.connect(temporary)) as destination_connection:
                source_connection.backup(destination_connection)
                destination_connection.commit()
                integrity = destination_connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]
                if integrity != "ok":
                    raise RuntimeError(f"backup integrity check failed: {integrity}")
        temporary.chmod(0o600)
        temporary.replace(final)
        final.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    cutoff = now.timestamp() - retention_days * 86400
    for candidate in destination_dir.glob("meds-*.sqlite3"):
        if candidate != final and candidate.stat().st_mtime < cutoff:
            candidate.unlink()
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up a SQLite database")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--retention-days", type=int, required=True)
    args = parser.parse_args()
    result = backup_database(
        args.source,
        args.destination,
        args.retention_days,
        datetime.now(UTC),
    )
    print(result)


if __name__ == "__main__":
    main()
