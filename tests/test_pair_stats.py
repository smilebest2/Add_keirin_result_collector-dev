import sqlite3
import unittest

from src.db import init_db
from src.pair_stats import pair_context_for_entries, refresh_racer_pair_stats


class PairStatsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def insert_result(self, race_id, race_date, entries):
        self.conn.execute(
            """
            INSERT INTO race_master (race_id, race_date, venue, race_no, detail_url, created_at)
            VALUES (?, ?, 'test', 1, '', '')
            """,
            (race_id, race_date),
        )
        self.conn.executemany(
            """
            INSERT INTO race_result (race_id, rank, car_no, racer_name, prefecture, term)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [(race_id, rank, car_no, name, pref, term) for rank, car_no, name, pref, term in entries],
        )
        self.conn.commit()

    def test_refresh_pair_stats_uses_only_prior_results(self):
        self.insert_result(
            "r1",
            "2026-06-20",
            [(1, 1, "A", "東京", 1), (2, 2, "B", "大阪", 2), (3, 3, "C", "愛知", 3)],
        )
        self.insert_result(
            "r2",
            "2026-06-22",
            [(1, 2, "B", "大阪", 2), (2, 1, "A", "東京", 1), (3, 3, "C", "愛知", 3)],
        )

        count = refresh_racer_pair_stats(self.conn, before_date="2026-06-21")
        self.assertEqual(count, 3)
        row = self.conn.execute(
            """
            SELECT races_together, both_top2_count, a_first_b_second_count
            FROM racer_pair_stats
            WHERE racer_key_a = 'A|東京|1' AND racer_key_b = 'B|大阪|2'
            """
        ).fetchone()
        self.assertEqual(row["races_together"], 1)
        self.assertEqual(row["both_top2_count"], 1)
        self.assertEqual(row["a_first_b_second_count"], 1)

    def test_pair_context_returns_weighted_rates_for_entries(self):
        self.insert_result(
            "r1",
            "2026-06-20",
            [(1, 1, "A", "東京", 1), (2, 2, "B", "大阪", 2), (4, 3, "C", "愛知", 3)],
        )
        refresh_racer_pair_stats(self.conn, before_date="2026-06-21")
        context = pair_context_for_entries(
            self.conn,
            [
                {"car_no": 1, "racer_name": "A", "prefecture": "東京", "term": 1},
                {"car_no": 2, "racer_name": "B", "prefecture": "大阪", "term": 2},
            ],
        )
        self.assertEqual(context[1]["pair_races"], 1)
        self.assertEqual(context[1]["pair_top2_rate"], 100)
        self.assertEqual(context[1]["pair_ahead_rate"], 100)
        self.assertEqual(context[2]["pair_ahead_rate"], 0)


if __name__ == "__main__":
    unittest.main()
