import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src.live_odds_export import (
    JST,
    buy_candidate_rows,
    export_buy_candidate_live_odds,
    is_buy_candidate,
    safe_race_id,
    write_live_odds_snapshot,
)


class LiveOddsExportTest(unittest.TestCase):
    def test_rejects_unsafe_race_id(self):
        with self.assertRaises(ValueError):
            safe_race_id("../keirin.db")

    def test_writes_snapshot_and_index(self):
        payload = {
            "ok": True,
            "race": {
                "race_id": "20260912_KAW_01",
                "race_date": "2026-09-12",
                "venue": "川崎",
                "race_no": 1,
                "start_time": "15:10",
            },
            "odds": {
                "fetched_at": "2026-09-12T14:55:00+09:00",
                "odds_updated_at": "2026-09-12T14:54:00+09:00",
            },
            "decision": {
                "label": "妙味なし",
                "status": "no_value",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            path = write_live_odds_snapshot(payload, output_dir)

            self.assertEqual(path.name, "20260912_KAW_01.json")
            self.assertTrue(path.exists())

            index = json.loads((output_dir / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["snapshots"][0]["race_id"], "20260912_KAW_01")
            self.assertEqual(index["snapshots"][0]["decision_label"], "妙味なし")

    def test_buy_candidate_requires_operational_filters(self):
        base = {
            "recommended_bet_type": "ワイド",
            "confidence": "A",
            "skip_reason": "",
            "similar_sample_count": 40,
            "similar_roi": 85.0,
            "feature_json": json.dumps({
                "recommendation_source": "feature_line_mix",
                "chaos_level": "low",
            }, ensure_ascii=False),
        }

        self.assertTrue(is_buy_candidate(base))
        self.assertFalse(is_buy_candidate({**base, "confidence": "C"}))
        self.assertFalse(is_buy_candidate({**base, "similar_roi": 69.9}))
        self.assertFalse(is_buy_candidate({
            **base,
            "feature_json": json.dumps({
                "recommendation_source": "feature_line_mix",
                "chaos_level": "high",
            }, ensure_ascii=False),
        }))

    def test_buy_candidate_rows_filters_to_near_start(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE race_bet_recommendation (
                race_id TEXT,
                race_date TEXT,
                recommended_bet_type TEXT,
                combinations_json TEXT,
                confidence TEXT,
                skip_reason TEXT,
                similar_sample_count INTEGER,
                similar_roi REAL,
                feature_json TEXT
            );
            CREATE TABLE race_schedule (
                race_id TEXT,
                race_date TEXT,
                venue TEXT,
                race_no INTEGER,
                start_time TEXT,
                detail_url TEXT
            );
            """
        )
        feature_json = json.dumps({
            "recommendation_source": "feature_line_mix",
            "chaos_level": "low",
        }, ensure_ascii=False)
        rows = [
            ("20260912_KAW_01", "2026-09-12", "ワイド", "A", "", 40, 85.0, feature_json, "15:05"),
            ("20260912_KAW_02", "2026-09-12", "ワイド", "A", "", 40, 85.0, feature_json, "15:30"),
            ("20260912_KAW_03", "2026-09-12", "見送り", "A", "", 40, 85.0, feature_json, "15:04"),
        ]
        for race_id, race_date, bet_type, confidence, skip_reason, samples, roi, features, start_time in rows:
            conn.execute(
                """
                INSERT INTO race_bet_recommendation
                    (race_id, race_date, recommended_bet_type, confidence, skip_reason,
                     similar_sample_count, similar_roi, feature_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (race_id, race_date, bet_type, confidence, skip_reason, samples, roi, features),
            )
            conn.execute(
                """
                INSERT INTO race_schedule
                    (race_id, race_date, venue, race_no, start_time, detail_url)
                VALUES (?, ?, '川崎', 1, ?, 'https://example.test/racecard')
                """,
                (race_id, race_date, start_time),
            )

        candidates = buy_candidate_rows(
            conn,
            target_date="2026-09-12",
            now=datetime(2026, 9, 12, 15, 0, tzinfo=JST),
            from_minutes=0,
            window_minutes=6,
        )

        self.assertEqual([row["race_id"] for row in candidates], ["20260912_KAW_01"])
        conn.close()

    def test_auto_export_uses_open_connection_for_candidates(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE race_bet_recommendation (
                race_id TEXT,
                race_date TEXT,
                recommended_bet_type TEXT,
                combinations_json TEXT,
                confidence TEXT,
                skip_reason TEXT,
                similar_sample_count INTEGER,
                similar_roi REAL,
                feature_json TEXT
            );
            CREATE TABLE race_schedule (
                race_id TEXT,
                race_date TEXT,
                venue TEXT,
                race_no INTEGER,
                start_time TEXT,
                detail_url TEXT
            );
            """
        )
        feature_json = json.dumps({
            "recommendation_source": "feature_line_mix",
            "chaos_level": "low",
        }, ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO race_bet_recommendation
                (race_id, race_date, recommended_bet_type, combinations_json, confidence,
                 skip_reason, similar_sample_count, similar_roi, feature_json)
            VALUES ('20260912_KAW_01', '2026-09-12', 'ワイド', '["1=2"]',
                    'A', '', 40, 85.0, ?)
            """,
            (feature_json,),
        )
        conn.execute(
            """
            INSERT INTO race_schedule
                (race_id, race_date, venue, race_no, start_time, detail_url)
            VALUES ('20260912_KAW_01', '2026-09-12', '川崎', 1,
                    '15:05', 'https://example.test/racecard')
            """
        )

        @contextmanager
        def fake_connect(_db_path):
            try:
                yield conn
            finally:
                conn.close()

        def fake_export(active_conn, race_id, output_dir, retry=None, timeout=None):
            active_conn.execute("SELECT 1").fetchone()
            path = output_dir / f"{race_id}.json"
            path.write_text("{}", encoding="utf-8")
            return path

        with tempfile.TemporaryDirectory() as tmp:
            with patch("src.live_odds_export.connect", fake_connect), \
                 patch("src.live_odds_export.init_db", lambda _conn: None), \
                 patch("src.live_odds_export.export_live_odds_with_connection", side_effect=fake_export):
                result = export_buy_candidate_live_odds(
                    db_path="unused.sqlite",
                    output_dir=Path(tmp),
                    target_date="2026-09-12",
                    now=datetime(2026, 9, 12, 15, 0, tzinfo=JST),
                    budget_seconds=20,
                    request_timeout=2,
                    retry=1,
                )

        self.assertEqual(result["exported"], [{"race_id": "20260912_KAW_01", "file": "20260912_KAW_01.json"}])
        self.assertEqual(result["errors"], [])


if __name__ == "__main__":
    unittest.main()
