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

    def test_common_live_status_variants_are_classified_as_live(self):
        provider = FootballProvider()
        class LiveResponse:
            status_code = 200
            ok = True
            text = ""
            def json(self):
                return {"fixtures": [
                    {"id": 301, "home": "Team A", "away": "Team B", "status": "In Progress",
                     "home_score": 1, "away_score": 2},
                    {"id": 302, "home": "Team C", "away": "Team D", "status": {"long": "1st Half", "short": "1H"},
                     "home_score": 0, "away_score": 1},
                    {"id": 303, "home": "Team E", "away": "Team F", "status": "Unknown",
                     "home_score": 2, "away_score": 0, "elapsed": 54},
                    {"id": 304, "home": "Team G", "away": "Team H", "status": "Not Started",
                     "home_score": 0, "away_score": 0}
                ]}
        class LiveSession:
            def get(self, url, params, timeout):
                return LiveResponse()
        provider.session = LiveSession()
        rows = provider.fixtures("2026-11-23")
        self.assertEqual([row["status"] for row in rows], ["live", "live", "live", "scheduled"])
        self.assertEqual(rows[2]["elapsed_minute"], 54)

    def test_live_minute_is_parsed_from_sportscore_status_text_and_timer(self):
        provider = FootballProvider()
        rows = provider.parse_fixtures_payload({
            "matches": [
                {"slug": "match-37", "home": "Team A", "away": "Team B",
                 "status": "live", "status_text": "37'", "home_score": 1, "away_score": 0},
                {"slug": "match-45", "home": "Team C", "away": "Team D",
                 "status": {"short": "2H", "elapsed_minutes": 63},
                 "timer": {"elapsed": 63}, "home_score": 0, "away_score": 0},
                {"slug": "match-added", "home": "Team E", "away": "Team F",
                 "status": "live", "status_text": "45+2", "home_score": 0, "away_score": 0}
            ]
        })
        self.assertEqual([row["elapsed_minute"] for row in rows], [37, 63, "45+2"])
        self.assertEqual([row["status"] for row in rows], ["live", "live", "live"])

    def test_fixtures_reads_status_text_clock_when_elapsed_field_is_missing(self):
        provider = FootballProvider()
        class ClockResponse:
            status_code = 200
            ok = True
            text = ""
            def json(self):
                return {"fixtures": [{
                    "id": 991, "home": "Team A", "away": "Team B",
                    "status": "live", "status_text": "72'", "home_score": 2, "away_score": 1
                }]}
        class ClockSession:
            def get(self, url, params, timeout):
                return ClockResponse()
        provider.session = ClockSession()
        rows = provider.fixtures("2026-11-23", status="live")
        self.assertEqual(rows[0]["elapsed_minute"], 72)
        self.assertEqual(rows[0]["status"], "live")

    def test_compact_finished_status_codes_are_not_discarded(self):
        provider = FootballProvider()
        class CompactStatusResponse:
            status_code = 200
            ok = True
            text = ""
            def json(self):
                return {"fixtures": [
                    {"id": 201, "home": "Brentford", "away": "Everton",
                     "competition": "English Premier League", "date": "2026-11-20T12:00:00Z",
                     "status": "FT", "home_score": 2, "away_score": 1},
                    {"id": 202, "home": "Brentford", "away": "Everton",
                     "competition": "English Premier League", "date": "2026-11-21T12:00:00Z",
                     "status": "AET", "home_score": 1, "away_score": 0},
                    {"id": 203, "home": "Brentford", "away": "Everton",
                     "competition": "English Premier League", "date": "2026-11-22T12:00:00Z",
                     "status": "PEN", "home_score": 1, "away_score": 1}
                ]}
        class CompactStatusSession:
            def get(self, url, params, timeout):
                return CompactStatusResponse()
        provider.session = CompactStatusSession()
        rows = provider.fixtures("2026-11-20", status="finished")
        self.assertEqual([row["status"] for row in rows], ["finished", "finished", "finished"])
        self.assertEqual([row["home_goals"] for row in rows], [2, 1, 1])
        self.assertEqual([row["away_goals"] for row in rows], [1, 0, 1])

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
        self.assertEqual(provider._normalize_search_name("Arca Çorum FK"), "corum")

if __name__ == "__main__":
    unittest.main()
