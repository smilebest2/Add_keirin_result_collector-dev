import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db_guard import summarize, validate


class DbGuardTest(unittest.TestCase):
    def make_db(self, race_count: int) -> Path:
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        path = Path(handle.name)
        handle.close()
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE race_master (race_id TEXT, race_date TEXT)")
            conn.execute("CREATE TABLE race_schedule (race_id TEXT, race_date TEXT)")
            conn.execute("CREATE TABLE race_prediction (race_id TEXT, race_date TEXT)")
            conn.execute("CREATE TABLE race_prediction_result (race_id TEXT)")
            conn.executemany(
                "INSERT INTO race_master (race_id, race_date) VALUES (?, ?)",
                [(f"race-{index}", "2026-07-11") for index in range(race_count)],
            )
            conn.commit()
        finally:
            conn.close()
        return path

    def tearDown(self):
        for path in getattr(self, "paths", []):
            path.unlink(missing_ok=True)

    def test_summarize_counts_race_master_rows(self):
        self.paths = [self.make_db(3)]

        summary = summarize(self.paths[0])

        self.assertTrue(summary["exists"])
        self.assertEqual(summary["race_result_count"], 3)
        self.assertEqual(summary["latest_result_date"], "2026-07-11")

    def test_validate_allows_restored_db_with_non_decreasing_count(self):
        before = {"exists": True, "db_size_bytes": 2_000_000, "race_result_count": 100}
        after = {"exists": True, "db_size_bytes": 2_100_000, "race_result_count": 101}

        result = validate(before, after, "restored")

        self.assertTrue(result["ok"])
        self.assertEqual(result["reasons"], [])

    def test_validate_blocks_restore_failure_without_fresh_db_flag(self):
        before = {"exists": False, "db_size_bytes": 0, "race_result_count": 0}
        after = {"exists": True, "db_size_bytes": 2_000_000, "race_result_count": 72}

        result = validate(before, after, "download_failed")

        self.assertFalse(result["ok"])
        self.assertIn("DB artifact was not restored", result["reasons"][0])

    def test_validate_blocks_large_count_drop(self):
        before = {"exists": True, "db_size_bytes": 2_000_000, "race_result_count": 100}
        after = {"exists": True, "db_size_bytes": 2_100_000, "race_result_count": 80}

        result = validate(before, after, "restored")

        self.assertFalse(result["ok"])
        self.assertIn("race_master count dropped", result["reasons"][0])

    def test_validate_allows_explicit_fresh_db_when_it_has_rows(self):
        before = {"exists": False, "db_size_bytes": 0, "race_result_count": 0}
        after = {"exists": True, "db_size_bytes": 2_000_000, "race_result_count": 72}

        result = validate(before, after, "missing", allow_fresh_db=True)

        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
