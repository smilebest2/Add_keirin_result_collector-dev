import json
import unittest

from src.prediction import (
    TRIFECTA,
    TYPE_ANA_LINE_MIX,
    TYPE_ANA_PICKUP,
    TYPE_LINE_BREAK,
    TYPE_LINE_BREAK_PICKUP,
    bet_combinations,
    pick_feature_combo,
)


def feature_row(
    car_no,
    feature_score,
    line_no,
    position,
    age=30,
    top3_score=70,
    style_escape=0,
    style_dash=0,
    style_mark=0,
):
    return {
        "car_no": car_no,
        "racer_name": f"選手{car_no}",
        "age": age,
        "feature_available": 1,
        "feature_score": feature_score,
        "top3_score": top3_score,
        "base_score": 60 + feature_score,
        "entry_feature": {
            "line_no": line_no,
            "line_position": position,
            "line_is_head": 1 if position == 1 else 0,
            "is_second": 1 if position == 2 else 0,
            "line_member_count": 2,
            "line_strength": 80 - line_no,
            "line_strength_rank": line_no,
            "race_score_rank": car_no,
            "race_top3_rank": car_no,
            "score_minus_race_avg": feature_score,
            "top3_minus_race_avg": feature_score / 2,
            "win_rate_minus_race_avg": feature_score / 3,
            "score_gap_top": max(0, 20 - feature_score),
            "score_gap_second": max(0, 15 - feature_score),
            "style_escape": style_escape,
            "style_dash": style_dash,
            "style_mark": style_mark,
            "leader_age": age,
        },
    }


class LineStrategyPredictionTest(unittest.TestCase):
    def test_ana_line_mix_keeps_axis_and_adds_other_line(self):
        scored = [
            feature_row(1, 30, 1, 1, top3_score=90),
            feature_row(2, 22, 1, 2, top3_score=85, style_mark=1),
            feature_row(3, 21, 2, 1, top3_score=82, style_dash=1),
            feature_row(4, 12, 2, 2, top3_score=78, style_mark=1),
        ]

        combo, _score, _reason, _detail, detail_json = pick_feature_combo(TYPE_ANA_LINE_MIX, scored)
        detail = json.loads(detail_json)

        self.assertEqual(combo[0], 1)
        self.assertIn(2, combo)
        self.assertTrue(any(car in combo for car in (3, 4)))
        self.assertEqual(detail["context"]["strategy"], "main_line_plus_other_line")

    def test_line_break_avoids_top_two_favorite_heads(self):
        scored = [
            feature_row(1, 30, 1, 1, age=22, top3_score=90, style_escape=1),
            feature_row(3, 29, 2, 1, age=23, top3_score=88, style_dash=1),
            feature_row(2, 24, 1, 2, age=31, top3_score=86, style_mark=1),
            feature_row(4, 23, 2, 2, age=33, top3_score=84, style_mark=1),
            feature_row(5, 20, 3, 1, age=28, top3_score=82, style_dash=1),
        ]

        combo, _score, _reason, _detail, detail_json = pick_feature_combo(TYPE_LINE_BREAK, scored)
        detail = json.loads(detail_json)

        self.assertNotIn(1, combo)
        self.assertNotIn(3, combo)
        self.assertGreater(detail["context"]["favorite_collapse_risk"], 0)
        self.assertEqual(detail["context"]["strategy"], "favorite_line_break")

    def test_line_break_bets_include_trifecta_box_and_support_bets(self):
        bets = bet_combinations([2, 4, 5], TYPE_LINE_BREAK)

        self.assertEqual(len(bets[TRIFECTA]), 6)
        self.assertEqual(bets["3連複"], ["2=4=5"])
        self.assertEqual(len(bets["ワイド"]), 3)

    def test_ana_pickup_only_when_young_or_close_different_heads(self):
        scored = [
            feature_row(1, 30, 1, 1, age=22, top3_score=90, style_escape=1),
            feature_row(3, 29, 2, 1, age=23, top3_score=88, style_dash=1),
            feature_row(2, 24, 1, 2, age=31, top3_score=86, style_mark=1),
            feature_row(4, 20, 2, 2, age=33, top3_score=84, style_mark=1),
        ]

        combo, _score, _reason, _detail, detail_json = pick_feature_combo(TYPE_ANA_PICKUP, scored)
        detail = json.loads(detail_json)

        self.assertEqual(combo[0], 1)
        self.assertEqual(detail["context"]["pickup_reason"], "young_diff_head_or_diff_head_gap_lt3")

    def test_line_break_pickup_skips_young_different_heads(self):
        scored = [
            feature_row(1, 30, 1, 1, age=22, top3_score=90, style_escape=1),
            feature_row(3, 29, 2, 1, age=23, top3_score=88, style_dash=1),
            feature_row(2, 24, 1, 2, age=31, top3_score=86, style_mark=1),
            feature_row(4, 20, 2, 2, age=33, top3_score=84, style_mark=1),
        ]

        combo, _score, reason, _detail, _detail_json = pick_feature_combo(TYPE_LINE_BREAK_PICKUP, scored)

        self.assertEqual(combo, [])
        self.assertIn("該当しない", reason)

    def test_line_break_pickup_accepts_close_non_young_gap(self):
        scored = [
            feature_row(1, 30, 1, 1, age=28, top3_score=90, style_escape=1),
            feature_row(3, 29, 2, 1, age=29, top3_score=88, style_dash=1),
            feature_row(2, 24, 1, 2, age=31, top3_score=86, style_mark=1),
            feature_row(4, 20, 2, 2, age=33, top3_score=84, style_mark=1),
        ]

        combo, _score, _reason, _detail, detail_json = pick_feature_combo(TYPE_LINE_BREAK_PICKUP, scored)
        detail = json.loads(detail_json)

        self.assertEqual(len(combo), 3)
        self.assertEqual(detail["context"]["pickup_reason"], "top1_top2_gap_lt3_without_young_diff_head")


if __name__ == "__main__":
    unittest.main()
