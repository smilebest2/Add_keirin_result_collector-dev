import sqlite3
import unittest

from src.db import init_db
from src.prediction import BET_TYPES, TRIFECTA, classify_bet_fit, recommendation_scored


TWO_PAIR, _TWO_EXACT, WIDE, TRIO, _TRIFECTA_FROM_LIST = BET_TYPES


def scored_row(car_no, base_score, starts=10, top2=55, top3=60, recent_top3=60):
    return {
        "car_no": car_no,
        "base_score": base_score,
        "starts": starts,
        "top2_rate": top2,
        "top3_rate": top3,
        "recent_starts": min(starts, 10),
        "recent_top3_rate": recent_top3,
        "score": 80 + car_no,
    }


EMPTY_SIMILAR = {
    bet_type: {"sample_count": 0, "hit_rate": None, "roi": None}
    for bet_type in (TRIFECTA, TRIO, TWO_PAIR, WIDE)
}

SIMILAR_EVIDENCE = {
    bet_type: {"sample_count": 30, "hit_rate": 20, "roi": 100}
    for bet_type in (TRIFECTA, TRIO, TWO_PAIR, WIDE)
}


class BetRecommendationTest(unittest.TestCase):
    def test_schema_contains_recommendation_table(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        table = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='race_bet_recommendation'
            """
        ).fetchone()
        self.assertIsNotNone(table)
        conn.close()

    def test_classifies_clear_order_as_trifecta(self):
        scored = [
            scored_row(1, 90),
            scored_row(2, 70),
            scored_row(3, 55),
            scored_row(4, 45),
        ]
        result = classify_bet_fit(
            scored,
            {"available": True, "line_count": 3, "bunsen_count": 3, "axis_followers": 1},
            SIMILAR_EVIDENCE,
        )
        self.assertEqual(result["bet_type"], TRIFECTA)
        self.assertEqual(result["combinations"], ["1-2-3"])

    def test_classifies_stable_top_three_as_trio(self):
        scored = [
            scored_row(1, 80),
            scored_row(2, 75),
            scored_row(3, 70),
            scored_row(4, 50),
        ]
        result = classify_bet_fit(
            scored,
            {"available": True, "line_count": 3, "bunsen_count": 2, "axis_followers": 0},
            SIMILAR_EVIDENCE,
        )
        self.assertEqual(result["bet_type"], TRIO)
        self.assertEqual(result["combinations"], ["1=2=3"])

    def test_classifies_clear_top_two_as_quinella(self):
        scored = [
            scored_row(1, 80),
            scored_row(2, 79),
            scored_row(3, 60),
            scored_row(4, 58),
        ]
        result = classify_bet_fit(
            scored,
            {"available": True, "line_count": 3, "bunsen_count": 2, "axis_followers": 0},
            SIMILAR_EVIDENCE,
        )
        self.assertEqual(result["bet_type"], TWO_PAIR)
        self.assertEqual(result["combinations"], ["1=2"])

    def test_classifies_strong_axis_as_two_wide_bets(self):
        scored = [
            scored_row(1, 80, top3=80, recent_top3=80),
            scored_row(2, 75),
            scored_row(3, 70),
            scored_row(4, 68),
        ]
        result = classify_bet_fit(
            scored,
            {"available": True, "line_count": 4, "bunsen_count": 2, "axis_followers": 0},
            SIMILAR_EVIDENCE,
        )
        self.assertEqual(result["bet_type"], WIDE)
        self.assertEqual(result["combinations"], ["1=2", "1=3"])

    def test_low_similar_sample_is_skipped(self):
        scored = [
            scored_row(1, 90),
            scored_row(2, 70),
            scored_row(3, 55),
            scored_row(4, 45),
        ]
        result = classify_bet_fit(
            scored,
            {"available": True, "line_count": 3, "bunsen_count": 3, "axis_followers": 1},
            EMPTY_SIMILAR,
        )
        self.assertEqual(result["combinations"], [])
        self.assertTrue(result["skip_reason"])

    def test_high_chaos_race_is_skipped(self):
        scored = [
            scored_row(1, 80, starts=2, top2=25, top3=30, recent_top3=25),
            scored_row(2, 79, starts=2, top2=25, top3=30, recent_top3=25),
            scored_row(3, 78, starts=2, top2=25, top3=30, recent_top3=25),
            scored_row(4, 77, starts=2, top2=25, top3=30, recent_top3=25),
        ]
        result = classify_bet_fit(
            scored,
            {"available": False, "line_count": None, "bunsen_count": None, "axis_followers": None},
            EMPTY_SIMILAR,
        )
        self.assertEqual(result["combinations"], [])
        self.assertEqual(result["features"]["chaos_level"], "high")
        self.assertTrue(result["skip_reason"])

    def test_missing_history_and_lineup_is_skipped(self):
        scored = [
            scored_row(1, 80, starts=2),
            scored_row(2, 75, starts=2),
            scored_row(3, 70, starts=2),
            scored_row(4, 68, starts=2),
        ]
        result = classify_bet_fit(
            scored,
            {"available": False, "line_count": None, "bunsen_count": None, "axis_followers": None},
            EMPTY_SIMILAR,
        )
        self.assertEqual(result["combinations"], [])
        self.assertTrue(result["skip_reason"])


class OperationalRecommendationTest(unittest.TestCase):
    def test_low_similar_roi_is_skipped_even_with_enough_samples(self):
        low_roi_evidence = {
            bet_type: {"sample_count": 30, "hit_rate": 20, "roi": 50}
            for bet_type in SIMILAR_EVIDENCE
        }
        scored = [
            scored_row(1, 90),
            scored_row(2, 70),
            scored_row(3, 55),
            scored_row(4, 45),
        ]
        result = classify_bet_fit(
            scored,
            {"available": True, "line_count": 3, "bunsen_count": 3, "axis_followers": 1},
            low_roi_evidence,
        )

        self.assertEqual(result["combinations"], [])
        self.assertEqual(result["features"]["operational_filter"], "similar_roi_below_floor")

    def test_recommendation_scored_prefers_feature_line_mix_when_available(self):
        scored = [
            {
                **scored_row(1, 90),
                "feature_available": 1,
                "feature_score": 1,
                "entry_feature": {"race_score_rank": 1, "line_strength_rank": 1},
                "top3_score": 80,
            },
            {
                **scored_row(2, 70),
                "feature_available": 1,
                "feature_score": 20,
                "entry_feature": {
                    "race_score_rank": 1,
                    "line_strength_rank": 1,
                    "is_second": 1,
                    "line_strength": 80,
                },
                "top3_score": 85,
            },
            {
                **scored_row(3, 55),
                "feature_available": 1,
                "feature_score": 18,
                "entry_feature": {"race_score_rank": 2, "line_strength_rank": 1},
                "top3_score": 82,
            },
            {
                **scored_row(4, 45),
                "feature_available": 1,
                "feature_score": 5,
                "entry_feature": {"race_score_rank": 4, "line_strength_rank": 3},
                "top3_score": 70,
            },
        ]
        operational, source = recommendation_scored(scored)

        self.assertEqual(source, "feature_line_mix")
        self.assertEqual(
            int(max(operational, key=lambda row: row["base_score"])["car_no"]),
            2,
        )


if __name__ == "__main__":
    unittest.main()
