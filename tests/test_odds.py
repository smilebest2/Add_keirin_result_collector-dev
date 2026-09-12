import json
import unittest

from src.odds import odds_url_from_detail_url, parse_race_odds_html


def sample_html() -> str:
    state = {
        "tanStackQuery": {
            "queries": [
                {
                    "queryKey": ["keirin/race/odds", "FETCH_KEIRIN_RACE_ODDS", {"cupId": "2026091034"}],
                    "state": {
                        "data": {
                            "trifecta": [
                                {
                                    "key": [1, 2, 3],
                                    "odds": 976.9,
                                    "minOdds": 0,
                                    "maxOdds": 0,
                                    "oddsStr": "976.9",
                                    "popularityOrder": 157,
                                    "unitPrice": 100,
                                    "absent": False,
                                }
                            ],
                            "trio": [
                                {
                                    "key": [3, 1, 2],
                                    "odds": 40.5,
                                    "minOdds": 0,
                                    "maxOdds": 0,
                                    "oddsStr": "40.5",
                                    "popularityOrder": 16,
                                    "unitPrice": 100,
                                    "absent": False,
                                }
                            ],
                            "quinellaPlace": [
                                {
                                    "key": [2, 1],
                                    "odds": 0,
                                    "minOdds": 16.7,
                                    "maxOdds": 18.9,
                                    "oddsStr": "0.0",
                                    "popularityOrder": 20,
                                    "unitPrice": 100,
                                    "absent": False,
                                }
                            ],
                            "oddsUpdatedAt": 1789197052,
                            "oddsDelayed": True,
                            "finalOdds": False,
                            "isAggregated": True,
                        }
                    },
                }
            ]
        }
    }
    return f"<script>window.__PRELOADED_STATE__ = {json.dumps(state, ensure_ascii=False)};\nwindow.__CONFIG__ = {{}};</script>"


class OddsParsingTest(unittest.TestCase):
    def test_builds_odds_url_from_racecard_url(self):
        self.assertEqual(
            odds_url_from_detail_url("https://www.winticket.jp/keirin/sasebo/racecard/2026091285/1/1"),
            "https://www.winticket.jp/keirin/sasebo/odds/2026091285/1/1",
        )

    def test_parses_preloaded_state_odds(self):
        snapshot = parse_race_odds_html(sample_html(), "https://example.test/odds")

        self.assertEqual(snapshot["counts"]["3連単"], 1)
        self.assertEqual(snapshot["bets"]["3連単"]["1-2-3"]["value_odds"], 976.9)
        self.assertEqual(snapshot["bets"]["3連複"]["1=2=3"]["popularity"], 16)
        self.assertEqual(snapshot["bets"]["ワイド"]["1=2"]["value_odds"], 16.7)
        self.assertEqual(snapshot["bets"]["ワイド"]["1=2"]["odds_label"], "16.7-18.9")
        self.assertTrue(snapshot["odds_delayed"])
        self.assertFalse(snapshot["final_odds"])


if __name__ == "__main__":
    unittest.main()
