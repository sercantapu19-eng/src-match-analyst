import unittest

from openligadb_provider import OpenLigaDBProvider


class OpenLigaDBNormalizationTests(unittest.TestCase):
    def test_normalizes_full_time_and_half_time(self):
        raw = {
            "MatchID": 123,
            "MatchDateTimeUTC": "2026-11-23T17:00:00Z",
            "Team1": {"TeamName": "Home FC"},
            "Team2": {"TeamName": "Away FC"},
            "MatchResults": [
                {"ResultTypeID": 1, "ResultOrderID": 1, "ResultName": "Halbzeit",
                 "PointsTeam1": 1, "PointsTeam2": 0},
                {"ResultTypeID": 2, "ResultOrderID": 2, "ResultName": "Endergebnis",
                 "PointsTeam1": 2, "PointsTeam2": 1},
            ],
        }
        result = OpenLigaDBProvider.normalize_match(raw, "test")
        self.assertEqual(result["id"], "123")
        self.assertEqual(result["home"], "Home FC")
        self.assertEqual(result["away"], "Away FC")
        self.assertEqual((result["ht_home"], result["ht_away"]), (1, 0))
        self.assertEqual((result["home_goals"], result["away_goals"]), (2, 1))
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["provider"], "openligadb")

    def test_missing_scores_remain_missing_not_zero(self):
        result = OpenLigaDBProvider.normalize_match({
            "MatchID": 456,
            "Team1": {"TeamName": "Home"},
            "Team2": {"TeamName": "Away"},
            "MatchResults": [],
        }, "test")
        self.assertIsNone(result["home_goals"])
        self.assertIsNone(result["away_goals"])
        self.assertEqual(result["status"], "scheduled")

    def test_rejects_path_injection_in_league_shortcut(self):
        provider = OpenLigaDBProvider()
        with self.assertRaises(ValueError):
            provider.matches("../invalid", 2026)


if __name__ == "__main__":
    unittest.main()
