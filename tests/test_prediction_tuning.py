import unittest
from unittest.mock import patch

from src.prediction import MODEL_VERSION, entry_scores, lineup_position


class PredictionTuningTest(unittest.TestCase):
    @patch("src.prediction.lineup_positions", return_value={1: 1})
    @patch(
        "src.prediction.car_context",
        return_value={
            "venue_win_rate": 25,
            "same_venue_yesterday": True,
            "yesterday_top3": 20,
        },
    )
    @patch(
        "src.prediction.racer_history",
        return_value={
            "starts": 10,
            "avg_rank": 3,
            "win_rate": 10,
            "top2_rate": 30,
            "top3_rate": 50,
            "venue_starts": 5,
            "venue_top3_rate": 40,
            "upset_score": 0,
            "fade_score": 0,
            "activity_score": 10,
        },
    )
    @patch(
        "src.prediction.rows",
        return_value=[
            {
                "race_id": "test-race",
                "car_no": 1,
                "racer_name": "選手A",
                "score": 50,
                "win_rate": 20,
                "quinella_rate": 40,
                "trifecta_rate": 60,
            }
        ],
    )
    def test_entry_and_history_rates_are_kept_separate(
        self,
        _rows,
        _history,
        _context,
        _lineup,
    ):
        scored = entry_scores(
            object(),
            {"race_id": "test-race", "venue": "テスト場"},
            "2026-06-20",
        )

        self.assertEqual(MODEL_VERSION, "explainable-v3")
        self.assertEqual(scored[0]["entry_win_rate"], 20)
        self.assertEqual(scored[0]["entry_top2_rate"], 40)
        self.assertEqual(scored[0]["entry_top3_rate"], 60)
        self.assertEqual(scored[0]["win_rate"], 10)
        self.assertEqual(scored[0]["top2_rate"], 30)
        self.assertEqual(scored[0]["top3_rate"], 50)
        self.assertAlmostEqual(scored[0]["base_score"], 70.4)

    def test_corrupt_lineup_is_ignored(self):
        class Connection:
            def execute(self, sql, params=()):
                if "race_lineup_forecast" in sql:
                    return Cursor(
                        [
                            {"car_no": 1, "line_no": 1, "line_position": 1},
                            {"car_no": 1, "line_no": 1, "line_position": 20},
                            {"car_no": 2, "line_no": 1, "line_position": 2},
                        ]
                    )
                return Cursor([{"car_no": 1}, {"car_no": 2}])

        self.assertIsNone(lineup_position(Connection(), "test-race", 1))

    def test_valid_lineup_position_is_returned(self):
        class Connection:
            def execute(self, sql, params=()):
                if "race_lineup_forecast" in sql:
                    return Cursor(
                        [
                            {"car_no": 1, "line_no": 1, "line_position": 1},
                            {"car_no": 2, "line_no": 1, "line_position": 2},
                            {"car_no": 3, "line_no": 2, "line_position": 1},
                        ]
                    )
                return Cursor([{"car_no": 1}, {"car_no": 2}, {"car_no": 3}])

        self.assertEqual(lineup_position(Connection(), "test-race", 2), 2)


class Cursor:
    def __init__(self, values):
        self.values = values

    def fetchall(self):
        return self.values


if __name__ == "__main__":
    unittest.main()
