import unittest
from football_provider import FootballProvider

class FakeResponse:
    status_code = 200
    ok = True
    text = ""

    def json(self):
        return {
            "fixtures": [
                {
                    "id": 123,
                    "home": {"name": "Home FC"},
                    "away": {"name": "Away FC"},
                    "competition": {"name": "Test League"},
                    "date": "2026-11-20T12:00:00Z",
                    "status": "finished",
                    "status_short": "FT",
                    "home_score": 2,
                    "away_score": 1,
                    "halftime": {"home": 1, "away": 0}
                },
                {
                    "id": 124,
                    "home": "Nested Home",
                    "away": "Nested Away",
                    "competition": "Test League",
                    "date": "2026-11-20T15:00:00Z",
                    "status": {"short": "FT", "long": "Match Finished"},
                    "score": {"full_time": {"home": "3", "away": "2"}, "half_time": {"home": "1", "away": "1"}}
                }
            ]
        }

class FakeSession:
    def __init__(self):
        self.params = None

    def get(self, url, params, timeout):
        self.params = params
        self.timeout = timeout
        return FakeResponse()

class FootballProviderTests(unittest.TestCase):
    def test_finished_fixture_and_half_time_scores_are_parsed(self):
        provider = FootballProvider()
        fake = FakeSession()
        provider.session = fake

        rows = provider.fixtures("2026-11-20", status="finished")

        self.assertEqual(fake.params["status"], "finished")
        self.assertEqual(fake.timeout, 10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "finished")
        self.assertEqual(rows[0]["home_goals"], 2)
        self.assertEqual(rows[0]["away_goals"], 1)
        self.assertEqual(rows[0]["ht_home"], 1)
        self.assertEqual(rows[0]["ht_away"], 0)
        self.assertEqual(rows[1]["status"], "finished")
        self.assertEqual(rows[1]["home_goals"], 3)
        self.assertEqual(rows[1]["away_goals"], 2)
        self.assertEqual(rows[1]["ht_home"], 1)
        self.assertEqual(rows[1]["ht_away"], 1)

    def test_team_schedule_payload_parses_widget_match_shape(self):
        provider = FootballProvider()
        rows = provider.parse_fixtures_payload({
            "team": {"name": "Başakşehir", "slug": "istanbul-basaksehir"},
            "matches": [{
                "slug": "basaksehir-vs-corum",
                "home": {"name": "Başakşehir", "score": 2},
                "away": {"name": "Çorum FK", "score": 1},
                "league": {"name": "Turkish Super League"},
                "time": "2026-11-01T17:00:00Z",
                "status": "FT",
                "status_text": "Finished",
                "home_score": 2,
                "away_score": 1,
                "halftime": {"home": 1, "away": 0}
            }]
        })
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "basaksehir-vs-corum")
        self.assertEqual(rows[0]["status"], "finished")
        self.assertEqual(rows[0]["home_goals"], 2)
        self.assertEqual(rows[0]["away_goals"], 1)
        self.assertEqual(rows[0]["ht_home"], 1)
        self.assertEqual(rows[0]["ht_away"], 0)

    def test_team_search_accepts_only_exact_unique_team_slug(self):
        provider = FootballProvider()
        class SearchSession:
            def get(self, url, params, timeout):
                class Response:
                    status_code = 200
                    ok = True
                    text = ""
                    def json(self):
                        return {"teams": [
                            {"name": "Başakşehir", "slug": "basaksehir"},
                            {"name": "Başakşehirspor", "slug": "basaksehirspor"}
                        ]}
                self.params = params
                self.timeout = timeout
                return Response()
        fake = SearchSession()
        provider.session = fake
        self.assertEqual(provider.search_team_slug("Başakşehir"), "basaksehir")
        self.assertEqual(fake.params["q"], "Başakşehir")
        self.assertEqual(provider._normalize_search_name("İstanbul Başakşehir Futbol Kulübü"), "basaksehir")
        self.assertEqual(provider._normalize_search_name("Arca Çorum FK"), "arca corum")

if __name__ == "__main__":
    unittest.main()
