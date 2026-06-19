import sqlite3
import unittest

from src.db import init_db, save_race
from src.prediction import evaluate_predictions


class DeadHeatHandlingTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        race = {
            "race_id": "20260620_11_01",
            "race_date": "2026-06-20",
            "venue": "函館",
            "race_no": 1,
            "event_name": "同着テスト",
            "race_title": "A級",
            "race_class": "A1",
            "start_time": "10:00",
            "deadline_time": None,
            "status": "確定",
            "distance": 1625,
            "laps": 4,
            "weather": None,
            "temperature": None,
            "wind_direction": None,
            "wind_speed": None,
            "lineup_text": None,
            "race_comment": None,
            "detail_url": "https://example.test/race",
        }
        results = [
            {"rank": 1, "car_no": 2, "racer_name": "選手二"},
            {"rank": 1, "car_no": 7, "racer_name": "選手七"},
            {"rank": 3, "car_no": 5, "racer_name": "選手五"},
        ]
        payouts = [
            {
                "bet_type": "3連単",
                "combination": "2-7-5",
                "payout": 1200,
                "popularity": 3,
            },
            {
                "bet_type": "3連単",
                "combination": "7-2-5",
                "payout": 1600,
                "popularity": 5,
            },
        ]
        save_race(self.conn, race, results, payouts)

    def tearDown(self):
        self.conn.close()

    def add_prediction(self, prediction_type, combination):
        self.conn.execute(
            """
            INSERT INTO race_prediction (
                race_id, race_date, prediction_type,
                predicted_1st, predicted_2nd, predicted_3rd,
                stake_amount, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 100, ?)
            """,
            (
                "20260620_11_01",
                "2026-06-20",
                prediction_type,
                *combination,
                "2026-06-20T09:00:00",
            ),
        )
        self.conn.commit()

    def test_dead_heat_is_saved_on_race_master(self):
        dead_heat = self.conn.execute(
            "SELECT dead_heat FROM race_master WHERE race_id=?",
            ("20260620_11_01",),
        ).fetchone()[0]
        self.assertEqual(dead_heat, 1)

    def test_each_official_trifecta_combination_is_exact_hit(self):
        self.add_prediction("本命予想", (2, 7, 5))
        self.add_prediction("穴予想", (7, 2, 5))

        self.assertEqual(evaluate_predictions(self.conn), 2)
        evaluated = self.conn.execute(
            """
            SELECT p.prediction_type, r.*
            FROM race_prediction_result r
            JOIN race_prediction p ON p.id=r.prediction_id
            ORDER BY p.prediction_type
            """
        ).fetchall()

        self.assertTrue(all(row["hit_exact"] == 1 for row in evaluated))
        self.assertTrue(all(row["hit_1st"] == 1 for row in evaluated))
        self.assertTrue(all(row["hit_top2"] == 1 for row in evaluated))
        self.assertTrue(all(row["hit_top3_count"] == 3 for row in evaluated))
        self.assertTrue(all(row["dead_heat"] == 1 for row in evaluated))
        self.assertTrue(all(row["actual_1st_candidates"] == "2,7" for row in evaluated))
        self.assertTrue(all(row["actual_2nd_candidates"] == "" for row in evaluated))
        self.assertTrue(all(row["actual_3rd_candidates"] == "5" for row in evaluated))
        self.assertEqual(
            {row["payout"] for row in evaluated},
            {1200, 1600},
        )

    def test_non_payout_combination_is_not_exact_hit(self):
        self.add_prediction("行動パターン予想", (5, 2, 7))

        self.assertEqual(evaluate_predictions(self.conn), 1)
        evaluated = self.conn.execute(
            "SELECT * FROM race_prediction_result"
        ).fetchone()
        self.assertEqual(evaluated["hit_exact"], 0)
        self.assertEqual(evaluated["hit_1st"], 0)
        self.assertEqual(evaluated["return_amount"], 0)
        self.assertIsNone(evaluated["payout"])


if __name__ == "__main__":
    unittest.main()
