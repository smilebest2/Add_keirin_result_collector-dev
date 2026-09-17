import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

from .config import DB_PATH, ROOT_DIR
from .db import connect, init_db
from .live_odds import live_odds_recheck


JST = timezone(timedelta(hours=9))
DOCS_LIVE_ODDS_DIR = ROOT_DIR / "docs" / "data" / "live_odds"
RACE_ID_PATTERN = re.compile(r"^[0-9]{8}_[A-Z0-9]+_[0-9]{2}$")
DEFAULT_AUTO_WINDOW_MINUTES = 6
DEFAULT_AUTO_BUDGET_SECONDS = 55
DEFAULT_AUTO_MAX_RACES = 5
DEFAULT_AUTO_REQUEST_TIMEOUT = 8
DEFAULT_AUTO_RETRY = 1
DEFAULT_SKIP_EXISTING_MINUTES = 10


def safe_race_id(race_id: str) -> str:
    value = str(race_id or "").strip()
    if not RACE_ID_PATTERN.fullmatch(value):
        raise ValueError(f"unsupported race_id: {race_id}")
    return value


def parse_json(value, fallback):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return parsed if parsed is not None else fallback


def to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_buy_candidate(row: dict) -> bool:
    bet_type = row.get("recommended_bet_type") or ""
    features = parse_json(row.get("feature_json"), {})
    return bool(
        bet_type
        and bet_type != "見送り"
        and not row.get("skip_reason")
        and (row.get("confidence") or "C") in {"A", "B"}
        and (features.get("recommendation_source") or "") == "feature_line_mix"
        and (features.get("chaos_level") or "") != "high"
        and (to_int(row.get("similar_sample_count")) or 0) >= 30
        and to_float(row.get("similar_roi")) is not None
        and float(row.get("similar_roi")) >= 70
    )


def race_start_at(row: dict) -> datetime | None:
    race_date = row.get("race_date")
    start_time = row.get("start_time")
    if not race_date or not start_time:
        return None
    try:
        return datetime.strptime(f"{race_date} {start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    except ValueError:
        return None


def minutes_until_start(row: dict, now: datetime | None = None) -> float | None:
    start_at = race_start_at(row)
    if start_at is None:
        return None
    now = now or datetime.now(JST)
    return (start_at - now).total_seconds() / 60


def existing_snapshot_is_recent(race_id: str, output_dir: Path, now: datetime | None = None, skip_minutes: int = 0) -> bool:
    if skip_minutes <= 0:
        return False
    path = output_dir / f"{safe_race_id(race_id)}.json"
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    fetched_at = (payload.get("odds") or {}).get("fetched_at")
    if not fetched_at:
        return False
    try:
        fetched = datetime.fromisoformat(str(fetched_at))
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=JST)
    now = now or datetime.now(JST)
    return (now - fetched).total_seconds() < skip_minutes * 60


def write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return path


def snapshot_summary(payload: dict, path: Path) -> dict:
    race = payload.get("race") or {}
    odds = payload.get("odds") or {}
    decision = payload.get("decision") or {}
    return {
        "race_id": race.get("race_id"),
        "race_date": race.get("race_date"),
        "venue": race.get("venue"),
        "race_no": race.get("race_no"),
        "start_time": race.get("start_time"),
        "decision_label": decision.get("label"),
        "decision_status": decision.get("status"),
        "fetched_at": odds.get("fetched_at"),
        "odds_updated_at": odds.get("odds_updated_at"),
        "file": path.name,
    }


def write_index(output_dir: Path = DOCS_LIVE_ODDS_DIR) -> Path:
    snapshots = []
    output_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(output_dir.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        snapshots.append(snapshot_summary(payload, path))
    snapshots.sort(key=lambda item: (item.get("fetched_at") or "", item.get("race_id") or ""), reverse=True)
    return write_json(
        output_dir / "index.json",
        {
            "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
            "snapshots": snapshots,
        },
    )


def write_live_odds_snapshot(payload: dict, output_dir: Path = DOCS_LIVE_ODDS_DIR) -> Path:
    race = payload.get("race") or {}
    race_id = safe_race_id(race.get("race_id") or "")
    path = write_json(output_dir / f"{race_id}.json", payload)
    write_index(output_dir)
    return path


def export_live_odds_with_connection(
    conn,
    race_id: str,
    output_dir: Path = DOCS_LIVE_ODDS_DIR,
    retry: int | None = None,
    timeout: float | None = None,
) -> Path:
    race_id = safe_race_id(race_id)
    payload = live_odds_recheck(conn, race_id, retry=retry, timeout=timeout)
    return write_live_odds_snapshot(payload, output_dir)


def export_live_odds(
    race_id: str,
    db_path=DB_PATH,
    output_dir: Path = DOCS_LIVE_ODDS_DIR,
    retry: int | None = None,
    timeout: float | None = None,
) -> Path:
    race_id = safe_race_id(race_id)
    with connect(db_path) as conn:
        init_db(conn)
        return export_live_odds_with_connection(
            conn,
            race_id,
            output_dir=output_dir,
            retry=retry,
            timeout=timeout,
        )


def buy_candidate_rows(
    conn,
    target_date: str | None = None,
    now: datetime | None = None,
    from_minutes: float = 0,
    window_minutes: float = DEFAULT_AUTO_WINDOW_MINUTES,
) -> list[dict]:
    target_date = target_date or datetime.now(JST).date().isoformat()
    now = now or datetime.now(JST)
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT r.race_id, r.race_date, r.recommended_bet_type,
                   r.combinations_json, r.confidence, r.skip_reason,
                   r.similar_sample_count, r.similar_roi, r.feature_json,
                   s.venue, s.race_no, s.start_time, s.detail_url
            FROM race_bet_recommendation r
            JOIN race_schedule s ON s.race_id = r.race_id
            WHERE r.race_date = ?
              AND r.recommended_bet_type IS NOT NULL
              AND r.recommended_bet_type != ''
              AND r.recommended_bet_type != '見送り'
            ORDER BY s.start_time, s.venue, s.race_no
            """,
            (target_date,),
        ).fetchall()
    ]
    candidates = []
    for row in rows:
        minutes = minutes_until_start(row, now)
        if minutes is None:
            continue
        if from_minutes <= minutes <= window_minutes and is_buy_candidate(row):
            row["minutes_until_start"] = minutes
            candidates.append(row)
    return candidates


def export_buy_candidate_live_odds(
    db_path=DB_PATH,
    output_dir: Path = DOCS_LIVE_ODDS_DIR,
    target_date: str | None = None,
    from_minutes: float = 0,
    window_minutes: float = DEFAULT_AUTO_WINDOW_MINUTES,
    max_races: int = DEFAULT_AUTO_MAX_RACES,
    budget_seconds: float = DEFAULT_AUTO_BUDGET_SECONDS,
    request_timeout: float = DEFAULT_AUTO_REQUEST_TIMEOUT,
    retry: int = DEFAULT_AUTO_RETRY,
    skip_existing_minutes: int = DEFAULT_SKIP_EXISTING_MINUTES,
    strict: bool = False,
    now: datetime | None = None,
    ensure_schema: bool = False,
) -> dict:
    started = monotonic()
    deadline = started + max(1, budget_seconds)
    now = now or datetime.now(JST)
    candidates = []
    exported = []
    skipped = []
    errors = []
    try:
        with connect(db_path) as conn:
            if ensure_schema:
                init_db(conn)
            candidates = buy_candidate_rows(
                conn,
                target_date=target_date,
                now=now,
                from_minutes=from_minutes,
                window_minutes=window_minutes,
            )
            for candidate in candidates[:max(0, max_races)]:
                race_id = candidate["race_id"]
                remaining = deadline - monotonic()
                if remaining < max(5, min(request_timeout, 10)):
                    skipped.append({"race_id": race_id, "reason": "budget_exhausted"})
                    break
                if existing_snapshot_is_recent(race_id, output_dir, now=now, skip_minutes=skip_existing_minutes):
                    skipped.append({"race_id": race_id, "reason": "recent_snapshot_exists"})
                    continue
                try:
                    path = export_live_odds_with_connection(
                        conn,
                        race_id,
                        output_dir=output_dir,
                        retry=retry,
                        timeout=min(request_timeout, max(1, remaining - 1)),
                    )
                    exported.append({"race_id": race_id, "file": path.name})
                except Exception as exc:
                    errors.append({"race_id": race_id, "error": str(exc)})
                    if strict:
                        raise
    except Exception as exc:
        if strict:
            raise
        errors.append({"race_id": None, "error": str(exc)})
    return {
        "ok": not errors or not strict,
        "target_date": target_date or now.date().isoformat(),
        "candidate_count": len(candidates),
        "exported": exported,
        "skipped": skipped,
        "errors": errors,
        "elapsed_seconds": round(monotonic() - started, 3),
        "budget_seconds": budget_seconds,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch one race live odds and publish a static JSON snapshot")
    parser.add_argument("--race-id")
    parser.add_argument("--auto-buy-candidates", action="store_true")
    parser.add_argument("--date", help="Target race date in YYYY-MM-DD. Default: today")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--output-dir", type=Path, default=DOCS_LIVE_ODDS_DIR)
    parser.add_argument("--from-minutes", type=float, default=0)
    parser.add_argument("--window-minutes", type=float, default=DEFAULT_AUTO_WINDOW_MINUTES)
    parser.add_argument("--max-races", type=int, default=DEFAULT_AUTO_MAX_RACES)
    parser.add_argument("--budget-seconds", type=float, default=DEFAULT_AUTO_BUDGET_SECONDS)
    parser.add_argument("--request-timeout", type=float)
    parser.add_argument("--retry", type=int)
    parser.add_argument("--skip-existing-minutes", type=int, default=DEFAULT_SKIP_EXISTING_MINUTES)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--ensure-schema", action="store_true")
    args = parser.parse_args()

    if args.auto_buy_candidates:
        result = export_buy_candidate_live_odds(
            db_path=args.db,
            output_dir=args.output_dir,
            target_date=args.date,
            from_minutes=args.from_minutes,
            window_minutes=args.window_minutes,
            max_races=args.max_races,
            budget_seconds=args.budget_seconds,
            request_timeout=args.request_timeout if args.request_timeout is not None else DEFAULT_AUTO_REQUEST_TIMEOUT,
            retry=args.retry if args.retry is not None else DEFAULT_AUTO_RETRY,
            skip_existing_minutes=args.skip_existing_minutes,
            strict=args.strict,
            ensure_schema=args.ensure_schema,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if not args.race_id:
        parser.error("--race-id is required unless --auto-buy-candidates is set")
    path = export_live_odds(
        args.race_id,
        db_path=args.db,
        output_dir=args.output_dir,
        retry=args.retry,
        timeout=args.request_timeout,
    )
    print(path)


if __name__ == "__main__":
    main()
