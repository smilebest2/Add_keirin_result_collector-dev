import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import DB_PATH


MIN_DB_SIZE_BYTES = 1024 * 1024
COUNT_DROP_TOLERANCE = 0.05


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def scalar(conn: sqlite3.Connection, sql: str) -> Any:
    row = conn.execute(sql).fetchone()
    return row[0] if row else None


def table_count(conn: sqlite3.Connection, table_name: str) -> int:
    if not table_exists(conn, table_name):
        return 0
    return int(scalar(conn, f"SELECT COUNT(*) FROM {table_name}") or 0)


def latest_date(conn: sqlite3.Connection, table_name: str) -> str:
    if not table_exists(conn, table_name):
        return ""
    return str(scalar(conn, f"SELECT MAX(race_date) FROM {table_name}") or "")


def summarize(db_path: Path = DB_PATH) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "db_path": str(db_path),
        "exists": db_path.exists(),
        "db_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "race_result_count": 0,
        "race_schedule_count": 0,
        "prediction_count": 0,
        "prediction_result_count": 0,
        "latest_result_date": "",
        "latest_schedule_date": "",
        "latest_prediction_date": "",
    }
    if not db_path.exists():
        return summary
    conn = sqlite3.connect(db_path)
    try:
        summary.update(
            {
                "race_result_count": table_count(conn, "race_master"),
                "race_schedule_count": table_count(conn, "race_schedule"),
                "prediction_count": table_count(conn, "race_prediction"),
                "prediction_result_count": table_count(conn, "race_prediction_result"),
                "latest_result_date": latest_date(conn, "race_master"),
                "latest_schedule_date": latest_date(conn, "race_schedule"),
                "latest_prediction_date": latest_date(conn, "race_prediction"),
            }
        )
    finally:
        conn.close()
    return summary


def bool_text(value: bool) -> str:
    return "true" if value else "false"


def write_env(env_file: str | None, values: dict[str, Any], prefix: str = "") -> None:
    if not env_file:
        return
    lines = [f"{prefix}{key.upper()}={value}" for key, value in values.items()]
    with open(env_file, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def write_json(path: str | None, data: dict[str, Any]) -> None:
    if not path:
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate(
    before: dict[str, Any],
    after: dict[str, Any],
    restore_status: str,
    allow_fresh_db: bool = False,
) -> dict[str, Any]:
    reasons: list[str] = []
    restored = restore_status == "restored"
    before_count = int(before.get("race_result_count") or 0)
    after_count = int(after.get("race_result_count") or 0)
    after_size = int(after.get("db_size_bytes") or 0)

    if not after.get("exists"):
        reasons.append("data/keirin.db does not exist after the workflow")
    if after_size < MIN_DB_SIZE_BYTES:
        reasons.append(f"DB size is too small: {after_size} bytes")

    if restored:
        minimum_count = int(before_count * (1 - COUNT_DROP_TOLERANCE))
        if after_count < minimum_count:
            reasons.append(
                f"race_master count dropped from {before_count} to {after_count}"
            )
    elif not allow_fresh_db:
        reasons.append("DB artifact was not restored; refusing to publish a continuity backup")
    elif after_count <= 0:
        reasons.append("fresh DB was allowed but contains no race_master rows")

    result = {
        "ok": not reasons,
        "reasons": reasons,
        "restore_status": restore_status,
        "allow_fresh_db": allow_fresh_db,
        "before": before,
        "after": after,
    }
    return result


def append_markdown_summary(path: str | None, validation: dict[str, Any]) -> None:
    if not path:
        return
    before = validation["before"]
    after = validation["after"]
    lines = [
        "## DB artifact guard",
        "",
        f"- status: {'OK' if validation['ok'] else 'BLOCKED'}",
        f"- restore_status: {validation['restore_status']}",
        f"- allow_fresh_db: {bool_text(bool(validation['allow_fresh_db']))}",
        f"- race_master: {before.get('race_result_count', 0)} -> {after.get('race_result_count', 0)}",
        f"- db_size_bytes: {after.get('db_size_bytes', 0)}",
        f"- latest_result_date: {after.get('latest_result_date', '')}",
    ]
    if validation["reasons"]:
        lines.append("")
        lines.append("Reasons:")
        lines.extend(f"- {reason}" for reason in validation["reasons"])
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize and guard the Keirin SQLite DB artifact")
    subparsers = parser.add_subparsers(dest="command", required=True)

    summary_parser = subparsers.add_parser("summary")
    summary_parser.add_argument("--db-path", type=Path, default=DB_PATH)
    summary_parser.add_argument("--json-file")
    summary_parser.add_argument("--env-file")
    summary_parser.add_argument("--env-prefix", default="")

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--before", required=True)
    validate_parser.add_argument("--after", required=True)
    validate_parser.add_argument("--restore-status", required=True)
    validate_parser.add_argument("--allow-fresh-db", action="store_true")
    validate_parser.add_argument("--json-file")
    validate_parser.add_argument("--env-file")
    validate_parser.add_argument("--summary-file")

    args = parser.parse_args()
    if args.command == "summary":
        result = summarize(args.db_path)
        write_json(args.json_file, result)
        write_env(args.env_file, result, args.env_prefix)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return

    result = validate(
        load_json(args.before),
        load_json(args.after),
        args.restore_status,
        allow_fresh_db=args.allow_fresh_db,
    )
    write_json(args.json_file, result)
    write_env(args.env_file, {"db_guard_ok": bool_text(bool(result["ok"]))})
    append_markdown_summary(args.summary_file, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
