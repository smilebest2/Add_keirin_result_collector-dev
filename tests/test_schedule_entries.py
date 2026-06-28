import unittest
import sys
import types

requests_stub = types.ModuleType("requests")
requests_stub.RequestException = Exception
sys.modules.setdefault("requests", requests_stub)

playwright_stub = types.ModuleType("playwright")
playwright_sync_stub = types.ModuleType("playwright.sync_api")
playwright_sync_stub.TimeoutError = TimeoutError
playwright_sync_stub.sync_playwright = lambda: None
sys.modules.setdefault("playwright", playwright_stub)
sys.modules.setdefault("playwright.sync_api", playwright_sync_stub)

bs4_stub = types.ModuleType("bs4")
bs4_stub.BeautifulSoup = object
sys.modules.setdefault("bs4", bs4_stub)

from src.schedule import entry_car_no, parse_detailed_entry_cells


class ScheduleEntryParsingTest(unittest.TestCase):
    def test_extracts_detailed_racecard_metrics_from_table_cells(self):
        cells = [
            "1", "1", "飯田風音\n埼玉 L1 24歳 120期", "", "54.78", "1", "4", "2", "両",
            "2", "7", "4", "4", "10", "7", "4", "7", "35.6", "60.6", "75.0",
            "3.79", "自力自在。",
        ]

        meta = parse_detailed_entry_cells(cells, 2)

        self.assertEqual(meta["score"], 54.78)
        self.assertEqual(meta["start_count"], 1)
        self.assertEqual(meta["home_count"], 4)
        self.assertEqual(meta["back_count"], 2)
        self.assertEqual(meta["win_rate"], 35.6)
        self.assertEqual(meta["quinella_rate"], 60.6)
        self.assertEqual(meta["trifecta_rate"], 75.0)
        self.assertEqual(meta["gear_ratio"], 3.79)
        self.assertEqual(meta["leg_type"], "両")
        self.assertEqual(meta["escape_count"], 2)
        self.assertEqual(meta["makuri_count"], 7)
        self.assertEqual(meta["sashi_count"], 4)
        self.assertEqual(meta["mark_count"], 4)
        self.assertEqual(meta["first_count"], 10)
        self.assertEqual(meta["second_count"], 7)
        self.assertEqual(meta["third_count"], 4)
        self.assertEqual(meta["outside_count"], 7)
        self.assertEqual(meta["comment"], "自力自在。")
        self.assertEqual(entry_car_no(cells, 2), 1)

    def test_extracts_detailed_metrics_without_frame_cell(self):
        cells = [
            "7", "中西叶美\n愛知 L1 29歳 112期", "", "44.20", "9", "7", "0", "両",
            "0", "0", "0", "1", "0", "1", "0", "23", "0.0", "4.1", "4.1",
            "3.77", "流れを見て。",
        ]

        meta = parse_detailed_entry_cells(cells, 1)

        self.assertEqual(meta["score"], 44.20)
        self.assertEqual(meta["win_rate"], 0.0)
        self.assertEqual(meta["quinella_rate"], 4.1)
        self.assertEqual(meta["trifecta_rate"], 4.1)
        self.assertEqual(entry_car_no(cells, 1), 7)


if __name__ == "__main__":
    unittest.main()
