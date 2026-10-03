import sqlite3
import unittest

from src.db import init_db
from src.prediction import ensure_prediction_bets


class PredictionBetIncrementalTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def insert_prediction(self, race_id: str) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO race_prediction
                (race_id, race_date, prediction_type, predicted_1st,
                 predicted_2nd, predicted_3rd, created_at)
            VALUES (?, '2026-10-03', '本命予想', 1, 2, 3, '2026-10-03T07:30:00')
            """,
            (race_id,),
        )
        return int(cursor.lastrowid)

    def test_only_predictions_without_bets_are_processed(self):
        existing_id = self.insert_prediction("existing")
        missing_id = self.insert_prediction("missing")
        self.conn.execute(
            """
            INSERT INTO race_prediction_bet
                (prediction_id, race_id, race_date, prediction_type,
                 bet_type, combination, stake_amount, created_at)
            VALUES (?, 'existing', '2026-10-03', '本命予想',
                    '3連単', '1-2-3', 100, '2026-10-03T07:30:00')
            """,
            (existing_id,),
        )

        saved = ensure_prediction_bets(self.conn)

        self.assertEqual(saved, 7)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM race_prediction_bet WHERE prediction_id = ?",
                (existing_id,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM race_prediction_bet WHERE prediction_id = ?",
                (missing_id,),
            ).fetchone()[0],
            7,
        )
        self.assertEqual(ensure_prediction_bets(self.conn), 0)


if __name__ == "__main__":
    unittest.main()
