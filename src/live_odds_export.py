import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DB_PATH, ROOT_DIR
from .db import connect, init_db
from .live_odds import live_odds_recheck


JST = timezone(timedelta(hours=9))
DOCS_LIVE_ODDS_DIR = ROOT_DIR / "docs" / "data" / "live_odds"
RACE_ID_PATTERN = re.compile(r"^[0-9]{8}_[A-Z0-9]+_[0-9]{2}$")


def safe_race_id(race_id: str) -> str:
    value = str(race_id or "").strip()
    if not RACE_ID_PATTERN.fullmatch(value):
        raise ValueError(f"unsupported race_id: {race_id}")
    return value


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


def export_live_odds(race_id: str, db_path=DB_PATH, output_dir: Path = DOCS_LIVE_ODDS_DIR) -> Path:
    race_id = safe_race_id(race_id)
    with connect(db_path) as conn:
        init_db(conn)
        payload = live_odds_recheck(conn, race_id)
    return write_live_odds_snapshot(payload, output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch one race live odds and publish a static JSON snapshot")
    parser.add_argument("--race-id", required=True)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--output-dir", type=Path, default=DOCS_LIVE_ODDS_DIR)
    args = parser.parse_args()

    path = export_live_odds(args.race_id, db_path=args.db, output_dir=args.output_dir)
    print(path)


if __name__ == "__main__":
    main()
