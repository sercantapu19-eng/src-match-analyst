import unittest
from result_sync import matching_fixture_rows


def normalize(value):
    return " ".join(str(value or "").lower().replace("fc", "").split())


class FixtureIdentityMatchingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = {
            "home": "North FC", "away": "United FC",
            "league": "Premier League", "kickoff": "2026-11-24T18:00:00+03:00",
        }
        self.rows = [
            {"id": 1, "home": "North", "away": "United", "league": "Premier League",
             "kickoff": "2026-11-24T18:00:00+03:00"},
            {"id": 2, "home": "North", "away": "United", "league": "Cup",
             "kickoff": "2026-11-24T18:00:00+03:00"},
            {"id": 3, "home": "North", "away": "United", "league": "Premier League",
             "kickoff": "2026-11-24T18:10:00+03:00"},
        ]

    def test_matches_same_teams_league_and_nearby_kickoff(self):
        self.assertEqual([r["id"] for r in matching_fixture_rows(self.rows, self.fixture, normalize)], [1, 3])

    def test_different_league_is_not_a_match(self):
        self.assertEqual(matching_fixture_rows([self.rows[0]], dict(self.fixture, league="Cup"), normalize), [])

    def test_missing_league_or_kickoff_fails_closed(self):
        self.assertEqual(matching_fixture_rows(self.rows, dict(self.fixture, league=""), normalize), [])
        self.assertEqual(matching_fixture_rows(self.rows, dict(self.fixture, kickoff=""), normalize), [])

    def test_distant_kickoff_is_not_a_match(self):
        fixture = dict(self.fixture, kickoff="2026-11-25T18:00:00+03:00")
        self.assertEqual(matching_fixture_rows(self.rows, fixture, normalize), [])


if __name__ == "__main__":
    unittest.main()
