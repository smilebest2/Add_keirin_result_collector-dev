import sqlite3
import unittest

from src.analysis import render_outcomes
from src.db import init_db, save_race


class OutcomePageTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def save(self, race_no, results, trifecta, payout):
        race_id = f"20260620_11_{race_no:02d}"
        save_race(
            self.conn,
            {
                "race_id": race_id,
                "race_date": "2026-06-20",
                "venue": "函館",
                "race_no": race_no,
                "event_name": "出目テスト",
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
                "detail_url": f"https://example.test/{race_id}",
            },
            results,
            [
                {
                    "bet_type": "3連単",
                    "combination": trifecta,
                    "payout": payout,
                    "popularity": 1,
                }
            ],
        )

    def test_page_ranks_normal_outcomes_and_excludes_dead_heat(self):
        normal = [
            {"rank": 1, "car_no": 1, "racer_name": "選手一"},
            {"rank": 2, "car_no": 2, "racer_name": "選手二"},
            {"rank": 3, "car_no": 3, "racer_name": "選手三"},
        ]
        dead_heat = [
            {"rank": 1, "car_no": 2, "racer_name": "選手二"},
            {"rank": 1, "car_no": 7, "racer_name": "選手七"},
            {"rank": 3, "car_no": 5, "racer_name": "選手五"},
        ]
        self.save(1, normal, "1-2-3", 1000)
        self.save(2, normal, "1-2-3", 1200)
        self.save(3, dead_heat, "2-7-5", 1800)

        page = render_outcomes(self.conn)

        self.assertIn("出目分析", page)
        self.assertIn("3連単 出目ランキング TOP20", page)
        self.assertIn('id="outcome-bet-filter"', page)
        self.assertIn("1-2-3", page)
        self.assertNotIn("2-7-5", page)
        self.assertIn("<strong>2</strong>", page)
        self.assertIn("<strong>1</strong>", page)


if __name__ == "__main__":
    unittest.main()
