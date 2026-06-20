import unittest
import sqlite3

from src.db import init_db, save_schedule
from src.lineup_validation import normalize_lineup


class LineupValidationTest(unittest.TestCase):
    def test_valid_lineup_is_normalized(self):
        lineup = [
            {"car_no": "1", "line_no": "1", "line_position": "1"},
            {"car_no": "2", "line_no": "1", "line_position": "2"},
            {"car_no": "3", "line_no": "2", "line_position": "1"},
        ]
        self.assertEqual(
            [row["car_no"] for row in normalize_lineup(lineup, {1, 2, 3})],
            [1, 2, 3],
        )

    def test_duplicate_or_unknown_car_invalidates_whole_lineup(self):
        duplicate = [
            {"car_no": 1, "line_no": 1, "line_position": 1},
            {"car_no": 1, "line_no": 1, "line_position": 2},
        ]
        self.assertEqual(normalize_lineup(duplicate, {1, 2}), [])
        self.assertEqual(
            normalize_lineup(
                [
                    {"car_no": 1, "line_no": 1, "line_position": 1},
                    {"car_no": 3, "line_no": 1, "line_position": 2},
                ],
                {1, 2},
            ),
            [],
        )

    def test_polluted_numeric_region_is_rejected(self):
        self.assertEqual(
            normalize_lineup(
                [
                    {"car_no": 1, "line_no": 1, "line_position": 1},
                    {"car_no": 1, "line_no": 1, "line_position": 2},
                    {"car_no": 20, "line_no": 1, "line_position": 3},
                ]
            ),
            [],
        )

    def test_invalid_lineup_is_not_saved(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        race = {
            "race_id": "test-race",
            "race_date": "2026-06-20",
            "venue": "テスト場",
            "race_no": 1,
            "detail_url": "https://example.test/race/1",
            "lineup_text": "1 1 2",
            "lineup": [
                {"car_no": 1, "line_no": 1, "line_position": 1},
                {"car_no": 1, "line_no": 1, "line_position": 2},
                {"car_no": 2, "line_no": 1, "line_position": 3},
            ],
        }
        entries = [
            {"car_no": 1, "racer_name": "選手A"},
            {"car_no": 2, "racer_name": "選手B"},
        ]

        save_schedule(conn, race, entries)

        self.assertIsNone(
            conn.execute(
                "SELECT lineup_text FROM race_schedule WHERE race_id='test-race'"
            ).fetchone()["lineup_text"]
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM race_lineup_forecast WHERE race_id='test-race'"
            ).fetchone()[0],
            0,
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
