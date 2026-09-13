import json
import tempfile
import unittest
from pathlib import Path

from src.live_odds_export import safe_race_id, write_live_odds_snapshot


class LiveOddsExportTest(unittest.TestCase):
    def test_rejects_unsafe_race_id(self):
        with self.assertRaises(ValueError):
            safe_race_id("../keirin.db")

    def test_writes_snapshot_and_index(self):
        payload = {
            "ok": True,
            "race": {
                "race_id": "20260912_KAW_01",
                "race_date": "2026-09-12",
                "venue": "川崎",
                "race_no": 1,
                "start_time": "15:10",
            },
            "odds": {
                "fetched_at": "2026-09-12T14:55:00+09:00",
                "odds_updated_at": "2026-09-12T14:54:00+09:00",
            },
            "decision": {
                "label": "妙味なし",
                "status": "no_value",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            path = write_live_odds_snapshot(payload, output_dir)

            self.assertEqual(path.name, "20260912_KAW_01.json")
            self.assertTrue(path.exists())

            index = json.loads((output_dir / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["snapshots"][0]["race_id"], "20260912_KAW_01")
            self.assertEqual(index["snapshots"][0]["decision_label"], "妙味なし")


if __name__ == "__main__":
    unittest.main()
