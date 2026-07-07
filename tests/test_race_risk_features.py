import unittest
import types
from unittest.mock import patch

from src.race_risk_features import apply_lightgbm_volatility, build_for_race


class RaceRiskFeaturesTest(unittest.TestCase):
    def test_build_for_race_outputs_bounded_scores(self):
        race = {
            "race_id": "20260705_TEST_01",
            "race_date": "2026-07-05",
            "venue": "TEST",
            "race_no": 1,
        }
        entries = [
            {
                "car_no": 1,
                "line_no": 1,
                "line_strength": 120,
                "line_member_count": 2,
                "score_minus_race_avg": 8,
                "top3_minus_race_avg": 14,
                "win_rate_minus_race_avg": 10,
                "race_score_rank": 1,
                "race_top3_rank": 1,
                "line_strength_rank": 1,
                "score_gap_top": 0,
                "score_gap_second": -2,
                "age_minus_race_avg": -2,
                "leader_score": 110,
                "leader_second_score_gap": 0,
            },
            {
                "car_no": 2,
                "line_no": 1,
                "line_strength": 120,
                "line_member_count": 2,
                "score_minus_race_avg": 4,
                "top3_minus_race_avg": 8,
                "win_rate_minus_race_avg": 5,
                "race_score_rank": 2,
                "race_top3_rank": 2,
                "line_strength_rank": 1,
                "score_gap_top": 4,
                "score_gap_second": 0,
                "age_minus_race_avg": 1,
                "leader_score": 110,
                "leader_second_score_gap": 4,
            },
            {
                "car_no": 3,
                "line_no": 2,
                "line_strength": 96,
                "line_member_count": 1,
                "score_minus_race_avg": -3,
                "top3_minus_race_avg": -4,
                "win_rate_minus_race_avg": -2,
                "race_score_rank": 3,
                "race_top3_rank": 3,
                "line_strength_rank": 2,
                "score_gap_top": 11,
                "score_gap_second": 7,
                "age_minus_race_avg": 4,
                "leader_score": 99,
                "leader_second_score_gap": 0,
            },
        ]

        confidence, volatility = build_for_race(race, entries, payout=12000, threshold=10000, class_values=[21, 22, 23])

        self.assertEqual(confidence["top1_car_no"], 1)
        self.assertGreaterEqual(confidence["confidence_score"], 0)
        self.assertLessEqual(confidence["confidence_score"], 1)
        self.assertGreaterEqual(volatility["volatility_probability"], 0)
        self.assertLessEqual(volatility["volatility_probability"], 1)
        self.assertEqual(volatility["high_payout"], 1)

    def test_lightgbm_failure_falls_back_without_raising(self):
        class BrokenClassifier:
            def __init__(self, **_kwargs):
                raise RuntimeError("missing optional dependency")

        records = [
            {
                "trifecta_payout": 1000 + index,
                "high_payout": index % 2,
                "line_member_variance": 0.1,
                "line_strength_gap": 1.0,
                "score_minus_race_avg_variance": 2.0,
                "win_rate_variance": 3.0,
                "class_variance": 0.0,
                "age_variance": 4.0,
                "leader_score_gap": 1.0,
                "second_score_gap": 0.5,
                "tanki_count": 1,
                "line_count": 3,
            }
            for index in range(100)
        ]

        def fake_import(name, globals_=None, locals_=None, fromlist=(), level=0):
            if name == "lightgbm":
                return types.SimpleNamespace(LGBMClassifier=BrokenClassifier)
            return original_import(name, globals_, locals_, fromlist, level)

        original_import = __import__
        with patch("builtins.__import__", fake_import):
            self.assertFalse(apply_lightgbm_volatility(records))


if __name__ == "__main__":
    unittest.main()
