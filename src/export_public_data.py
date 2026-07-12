import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DB_PATH, ROOT_DIR
from .db import connect, init_db


JST = timezone(timedelta(hours=9))
DOCS_DATA_DIR = ROOT_DIR / "docs" / "data"


def rows(conn, sql: str, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def scalar(conn, sql: str, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def write_json(path: Path, payload) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")
    return path.stat().st_size


def prediction_type_summary(conn) -> list[dict]:
    return rows(
        conn,
        """
        SELECT p.prediction_type,
               COUNT(*) AS predictions,
               SUM(r.hit_exact) AS exact_hits,
               ROUND(AVG(r.hit_exact) * 100, 3) AS exact_rate,
               ROUND(AVG(r.hit_1st) * 100, 3) AS first_rate,
               ROUND(AVG(r.hit_top3_count), 3) AS avg_top3_count,
               SUM(r.stake_amount) AS stake_total,
               SUM(r.return_amount) AS return_total,
               ROUND(SUM(r.return_amount) * 100.0 / NULLIF(SUM(r.stake_amount), 0), 3) AS roi
        FROM race_prediction p
        JOIN race_prediction_result r ON r.prediction_id = p.id
        GROUP BY p.prediction_type
        ORDER BY p.prediction_type
        """,
    )


def daily_counts(conn, days: int) -> list[dict]:
    return rows(
        conn,
        """
        WITH dates AS (
            SELECT race_date FROM race_schedule
            UNION
            SELECT race_date FROM race_master
            UNION
            SELECT race_date FROM race_prediction
        )
        SELECT d.race_date,
               COALESCE(s.races, 0) AS scheduled_races,
               COALESCE(m.races, 0) AS result_races,
               COALESCE(p.predictions, 0) AS predictions,
               COALESCE(pr.results, 0) AS prediction_results
        FROM dates d
        LEFT JOIN (
            SELECT race_date, COUNT(*) AS races
            FROM race_schedule
            GROUP BY race_date
        ) s ON s.race_date = d.race_date
        LEFT JOIN (
            SELECT race_date, COUNT(*) AS races
            FROM race_master
            GROUP BY race_date
        ) m ON m.race_date = d.race_date
        LEFT JOIN (
            SELECT race_date, COUNT(*) AS predictions
            FROM race_prediction
            GROUP BY race_date
        ) p ON p.race_date = d.race_date
        LEFT JOIN (
            SELECT p.race_date, COUNT(*) AS results
            FROM race_prediction_result r
            JOIN race_prediction p ON p.id = r.prediction_id
            GROUP BY p.race_date
        ) pr ON pr.race_date = d.race_date
        ORDER BY d.race_date DESC
        LIMIT ?
        """,
        (days,),
    )


def latest_predictions(conn) -> dict:
    target_date = scalar(conn, "SELECT MAX(race_date) FROM race_prediction")
    if not target_date:
        return {"race_date": None, "rows": []}
    return {
        "race_date": target_date,
        "rows": rows(
            conn,
            """
            SELECT p.race_id, p.race_date, p.prediction_type,
                   p.predicted_1st, p.predicted_2nd, p.predicted_3rd,
                   p.confidence, ROUND(p.score, 3) AS score,
                   p.reason_text,
                   s.venue, s.race_no, s.race_title, s.start_time,
                   ROUND(c.confidence_score, 4) AS race_confidence,
                   c.confidence_stars,
                   ROUND(v.volatility_probability, 4) AS volatility_probability,
                   v.volatility_bucket
            FROM race_prediction p
            LEFT JOIN race_schedule s ON s.race_id = p.race_id
            LEFT JOIN race_confidence c ON c.race_id = p.race_id
            LEFT JOIN race_volatility_features v ON v.race_id = p.race_id
            WHERE p.race_date = ?
            ORDER BY s.venue, s.race_no, p.prediction_type
            """,
            (target_date,),
        ),
    }


def latest_prediction_results(conn) -> dict:
    target_date = scalar(
        conn,
        """
        SELECT MAX(p.race_date)
        FROM race_prediction p
        JOIN race_prediction_result r ON r.prediction_id = p.id
        """,
    )
    if not target_date:
        return {"race_date": None, "rows": []}
    return {
        "race_date": target_date,
        "rows": rows(
            conn,
            """
            SELECT p.race_id, p.race_date, p.prediction_type,
                   p.predicted_1st, p.predicted_2nd, p.predicted_3rd,
                   r.actual_1st, r.actual_2nd, r.actual_3rd,
                   r.hit_exact, r.hit_1st, r.hit_top3_count,
                   r.payout, r.stake_amount, r.return_amount, ROUND(r.roi, 3) AS roi,
                   COALESCE(s.venue, m.venue) AS venue,
                   COALESCE(s.race_no, m.race_no) AS race_no,
                   COALESCE(s.race_title, m.race_title) AS race_title
            FROM race_prediction p
            JOIN race_prediction_result r ON r.prediction_id = p.id
            LEFT JOIN race_schedule s ON s.race_id = p.race_id
            LEFT JOIN race_master m ON m.race_id = p.race_id
            WHERE p.race_date = ?
            ORDER BY COALESCE(s.venue, m.venue), COALESCE(s.race_no, m.race_no), p.prediction_type
            """,
            (target_date,),
        ),
    }


def race_index(conn, days: int) -> list[dict]:
    return rows(
        conn,
        """
        SELECT m.race_id, m.race_date, m.venue, m.race_no,
               m.race_title, m.race_class, m.start_time,
               p.payout AS trifecta_payout,
               c.confidence_score,
               v.volatility_probability
        FROM race_master m
        LEFT JOIN payout p
          ON p.race_id = m.race_id
         AND p.bet_type LIKE '3%'
        LEFT JOIN race_confidence c ON c.race_id = m.race_id
        LEFT JOIN race_volatility_features v ON v.race_id = m.race_id
        WHERE m.race_date IN (
            SELECT race_date
            FROM race_master
            GROUP BY race_date
            ORDER BY race_date DESC
            LIMIT ?
        )
        ORDER BY m.race_date DESC, m.venue, m.race_no
        """,
        (days,),
    )


def export_public_data(output_dir: Path = DOCS_DATA_DIR, recent_days: int = 30) -> dict:
    generated_at = datetime.now(JST).isoformat(timespec="seconds")
    with connect() as conn:
        init_db(conn)
        files: dict[str, int] = {}
        summary = {
            "generated_at": generated_at,
            "db_size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
            "latest_schedule_date": scalar(conn, "SELECT MAX(race_date) FROM race_schedule"),
            "latest_result_date": scalar(conn, "SELECT MAX(race_date) FROM race_master"),
            "latest_prediction_date": scalar(conn, "SELECT MAX(race_date) FROM race_prediction"),
            "latest_prediction_result_date": scalar(conn, """
                SELECT MAX(p.race_date)
                FROM race_prediction p
                JOIN race_prediction_result r ON r.prediction_id = p.id
            """),
            "latest_prediction_result_checked_at": scalar(conn, "SELECT MAX(checked_at) FROM race_prediction_result"),
            "race_schedule_count": scalar(conn, "SELECT COUNT(*) FROM race_schedule") or 0,
            "race_result_count": scalar(conn, "SELECT COUNT(*) FROM race_master") or 0,
            "prediction_count": scalar(conn, "SELECT COUNT(*) FROM race_prediction") or 0,
            "prediction_result_count": scalar(conn, "SELECT COUNT(*) FROM race_prediction_result") or 0,
            "unevaluated_prediction_count": scalar(conn, """
                SELECT COUNT(*)
                FROM race_prediction p
                LEFT JOIN race_prediction_result r ON r.prediction_id = p.id
                WHERE r.id IS NULL
            """) or 0,
            "result_races_without_predictions": scalar(conn, """
                SELECT COUNT(DISTINCT m.race_id)
                FROM race_master m
                LEFT JOIN race_prediction p ON p.race_id = m.race_id
                WHERE p.id IS NULL
            """) or 0,
        }
        payloads = {
            "public_summary.json": summary,
            "daily_counts.json": daily_counts(conn, recent_days),
            "prediction_type_summary.json": prediction_type_summary(conn),
            "latest_predictions.json": latest_predictions(conn),
            "latest_prediction_results.json": latest_prediction_results(conn),
            "race_index.json": race_index(conn, recent_days),
        }
        for filename, payload in payloads.items():
            files[filename] = write_json(output_dir / filename, payload)
        manifest = {
            "generated_at": generated_at,
            "format_version": 1,
            "recent_days": recent_days,
            "files": files,
            "summary": summary,
            "note": "Public GitHub Pages data. The SQLite DB is intentionally excluded from Pages data.",
        }
        files["manifest.json"] = write_json(output_dir / "manifest.json", manifest)
        manifest["files"] = files
        return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Export lightweight public JSON data for GitHub Pages")
    parser.add_argument("--output-dir", type=Path, default=DOCS_DATA_DIR)
    parser.add_argument("--recent-days", type=int, default=30)
    args = parser.parse_args()
    print(json.dumps(export_public_data(args.output_dir, args.recent_days), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
