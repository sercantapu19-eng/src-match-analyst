import unittest
from odds_provider import ApiFootballOddsProvider, _team_match


class OddsProviderParserTests(unittest.TestCase):
    def test_parses_bookmaker_markets_and_suspended_prices(self):
        rows = ApiFootballOddsProvider.parse_odds({
            "response": [{
                "bookmakers": [{
                    "id": 8, "name": "Example Book",
                    "bets": [{
                        "id": 1, "name": "Match Winner",
                        "values": [
                            {"value": "Home", "odd": "2.10", "suspended": False},
                            {"value": "Draw", "odd": "3.20", "suspended": True},
                        ],
                    }],
                }],
            }],
        }, "prematch")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["bookmaker"], "Example Book")
        self.assertEqual(rows[0]["market"], "Match Winner")
        self.assertEqual(rows[0]["odd"], "2.1")
        self.assertTrue(rows[1]["suspended"])
        self.assertEqual(rows[0]["source"], "prematch")

    def test_team_matching_handles_accents_and_long_names(self):
        self.assertTrue(_team_match("Galatasaray", "Galatasaray SK"))
        self.assertTrue(_team_match("Bodo/Glimt", "Bodø Glimt"))
        self.assertFalse(_team_match("Galatasaray", "Aston Villa"))


if __name__ == "__main__":
    unittest.main()
