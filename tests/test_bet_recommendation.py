import sqlite3
import unittest

from src.db import init_db
from src.prediction import classify_bet_fit


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
    for bet_type in ("3連単", "3連複", "2車複", "ワイド")
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
            EMPTY_SIMILAR,
        )
        self.assertEqual(result["bet_type"], "3連単")
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
            EMPTY_SIMILAR,
        )
        self.assertEqual(result["bet_type"], "3連複")
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
            EMPTY_SIMILAR,
        )
        self.assertEqual(result["bet_type"], "2車複")
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
            EMPTY_SIMILAR,
        )
        self.assertEqual(result["bet_type"], "ワイド")
        self.assertEqual(result["combinations"], ["1=2", "1=3"])

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
        self.assertEqual(result["bet_type"], "見送り")
        self.assertIn("直近成績", result["skip_reason"])


if __name__ == "__main__":
    unittest.main()
