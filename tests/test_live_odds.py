import json
import unittest

from src.live_odds import evaluate_recommendation_with_odds


def recommendation(bet_type="ワイド", combinations=None, hit_rate=48.0, confidence="A", chaos="low"):
    return {
        "recommended_bet_type": bet_type,
        "combinations_json": json.dumps(combinations or ["1=2", "1=3"], ensure_ascii=False),
        "confidence": confidence,
        "similar_sample_count": 40,
        "similar_hit_rate": hit_rate,
        "similar_roi": 110.0,
        "feature_json": json.dumps({
            "chaos_level": chaos,
            "recommendation_source": "feature_line_mix",
        }, ensure_ascii=False),
    }


def odds_snapshot(odds_by_combination):
    return {
        "bets": {
            "ワイド": {
                combination: {
                    "combination": combination,
                    "odds_label": f"{odds:.1f}",
                    "value_odds": odds,
                    "popularity": index,
                    "absent": False,
                }
                for index, (combination, odds) in enumerate(odds_by_combination.items(), start=1)
            }
        }
    }


class LiveOddsEvaluationTest(unittest.TestCase):
    def test_keeps_recommendation_when_odds_have_value(self):
        result = evaluate_recommendation_with_odds(
            recommendation(),
            odds_snapshot({"1=2": 6.0, "1=3": 7.0}),
        )

        self.assertEqual(result["label"], "買い候補")
        self.assertGreater(result["candidate"]["expected_roi"], 115)

    def test_marks_low_price_recommendation_as_no_value(self):
        result = evaluate_recommendation_with_odds(
            recommendation(),
            odds_snapshot({"1=2": 1.2, "1=3": 1.5}),
        )

        self.assertEqual(result["label"], "妙味なし")
        self.assertLess(result["candidate"]["average_odds"], result["candidate"]["break_even_odds"])

    def test_does_not_promote_morning_skip_by_odds_only(self):
        result = evaluate_recommendation_with_odds(
            recommendation(bet_type="見送り", combinations=[]),
            odds_snapshot({"1=2": 20.0, "1=3": 30.0}),
        )

        self.assertEqual(result["label"], "見送り")
        self.assertEqual(result["candidate"]["combinations"], [])


if __name__ == "__main__":
    unittest.main()
