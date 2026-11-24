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
    def test_nested_match_object_preserves_provider_slug(self):
        provider = FootballProvider()
        rows = provider.parse_fixtures_payload({
            "matches": [{
                "match": {"slug": "slovan-bratislava-u19-vs-real-betis-u19", "id": 7420812},
                "home": {"name": "Slovan Bratislava U19"},
                "away": {"name": "Real Betis U19"},
                "status": "live", "home_score": 1, "away_score": 2,
                "date": "2026-11-24T13:00:00Z"
            }]
        })
        self.assertEqual(rows[0]["slug"], "slovan-bratislava-u19-vs-real-betis-u19")
        self.assertEqual(rows[0]["id"], "7420812")

    def test_match_page_url_can_supply_slug_without_using_team_ids(self):
        provider = FootballProvider()
        rows = provider.parse_fixtures_payload({
            "matches": [{
                "home": {"name": "Slovan Bratislava U19", "id": "home-team-id"},
                "away": {"name": "Real Betis U19", "id": "away-team-id"},
                "match_url": "https://sportscore.com/football/slovan-bratislava-u19-vs-real-betis-u19",
                "status": "live"
            }]
        })
        self.assertEqual(rows[0]["slug"], "slovan-bratislava-u19-vs-real-betis-u19")
        self.assertEqual(rows[0]["id"], "slovan-bratislava-u19-vs-real-betis-u19")

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

    def test_live_widget_feed_parses_minute_and_filters_non_live_matches(self):
        provider = FootballProvider()
        class LiveWidgetResponse:
            status_code = 200
            ok = True
            text = ""
            def json(self):
                return {"matches": [
                    {"slug": "team-a-vs-team-b", "home": "Team A", "away": "Team B",
                     "date": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                     "status": "live", "status_text": "Live 67'", "home_score": 1, "away_score": 1},
                    {"slug": "team-c-vs-team-d", "home": "Team C", "away": "Team D",
                     "date": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                     "status": "finished", "status_text": "FT", "home_score": 2, "away_score": 0},
                    {"slug": "team-e-vs-team-f", "home": "Team E", "away": "Team F",
                     "date": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                     "status": "live", "status_text": "1st Half", "home_score": 0, "away_score": 0}
                ]}
        class LiveWidgetSession:
            def get(self, url, params, timeout):
                self.url = url
                self.params = params
                self.timeout = timeout
                return LiveWidgetResponse()
        fake = LiveWidgetSession()
        provider.session = fake
        rows = provider.live_matches()
        self.assertEqual(fake.url, "https://sportscore.com/api/widget/matches/")
        self.assertEqual(fake.params, {"sport": "football", "limit": 50})
        self.assertEqual(fake.timeout, 10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["elapsed_minute"], 67)
        self.assertEqual(rows[0]["status"], "live")
        self.assertIsNone(rows[1]["elapsed_minute"])
        self.assertEqual(rows[1]["status"], "live")


    def test_live_widget_rejects_seven_hour_old_and_undated_rows(self):
        from datetime import datetime, timedelta, timezone
        provider = FootballProvider()
        now = datetime.now(timezone.utc)
        class StaleLiveResponse:
            status_code = 200
            ok = True
            text = ""
            def json(self):
                return {"matches": [
                    {"slug": "old-match", "home": "Old A", "away": "Old B",
                     "date": (now - timedelta(hours=7)).isoformat(),
                     "status": "live", "status_text": "Live 72'", "home_score": 1, "away_score": 0},
                    {"slug": "no-date", "home": "Unknown A", "away": "Unknown B",
                     "status": "live", "status_text": "Live 12'", "home_score": 0, "away_score": 0},
                    {"slug": "finished", "home": "Done A", "away": "Done B",
                     "date": (now - timedelta(hours=2)).isoformat(),
                     "status": "finished", "status_text": "FT", "home_score": 2, "away_score": 1}
                ]}
        class StaleLiveSession:
            def get(self, url, params, timeout):
                return StaleLiveResponse()
        provider.session = StaleLiveSession()
        self.assertEqual(provider.live_matches(), [])

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
        self.assertEqual(rows[0]["slug"], "basaksehir-vs-corum")
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


    def test_live_minute_parser_accepts_additional_widget_clock_shapes(self):
        provider = FootballProvider()
        rows = provider.parse_fixtures_payload({
            "matches": [
                {"id": "a", "home": "A", "away": "B", "status": "live",
                 "status": {"short": "1H", "elapsedTime": 28}},
                {"id": "b", "home": "C", "away": "D", "status": "live",
                 "time": {"display": "1st Half · 37:18"}},
                {"id": "c", "home": "E", "away": "F", "status": "live",
                 "timer": {"text": "Live 45+3'"}},
                {"id": "d", "home": "G", "away": "H", "status": "live",
                 "status_text": "Second Half - 71 min"}
            ]
        })
        self.assertEqual([row["elapsed_minute"] for row in rows], [28, 37, "45+3", 71])
        self.assertEqual([row["status"] for row in rows], ["live"] * 4)


    def test_match_timeline_normalizes_common_football_events(self):
        payload={"match":{"timeline":[
            {"minute":7,"type":"goal","player":{"name":"Yamauchi, Hinata"},"team":{"name":"Kashiwa"}},
            {"minute":9,"type":"corner"},
            {"minute":44,"type":"yellow_card","player":"Max Burgess"},
            {"minute":45,"type":"half_time"},
            {"minute":51,"type":"offside","player":"Player A"},
            {"minute":64,"type":"substitution","player_in":{"name":"Mao Hosoya"},"player_out":{"name":"Shoji Fujio"}},
            {"minute":70,"type":"red_card","player":"Player B"},
            {"minute":76,"type":"second_half"}
        ]}}
        events=FootballProvider.parse_match_events(payload)
        self.assertEqual([e["kind"] for e in events],["goal","corner","yellow_card","halftime","offside","substitution","red_card","second_half"])
        self.assertEqual(events[0]["detail"],"Yamauchi, Hinata")
        self.assertEqual(events[5]["detail"],"↓ Shoji Fujio  ↑ Mao Hosoya")
        self.assertEqual(events[7]["label"],"İkinci yarı başladı")

    def test_goal_event_extracts_nested_scorer_assist_and_team(self):
        events = FootballProvider.parse_match_events({
            "match": {"events": [{
                "minute": 34,
                "type": "goal",
                "player": {"id": "p1", "firstName": "Moussa", "lastName": "Diallo"},
                "assist": {"player": {"name": "Ibrahima Sarr"}},
                "team": {"name": "ASFA Dakar"},
                "team_side": "home",
            }]}
        })
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["primary_player"], "Moussa Diallo")
        self.assertEqual(events[0]["secondary_player"], "Ibrahima Sarr")
        self.assertEqual(events[0]["team"], "ASFA Dakar")
        self.assertEqual(events[0]["team_side"], "home")
        self.assertIn("Moussa Diallo", events[0]["detail"])
        self.assertIn("Asist: Ibrahima Sarr", events[0]["detail"])

    def test_live_fixture_match_uses_unique_exact_home_away_and_kickoff(self):
        fixtures = [
            {"id":"newcastle-jets-vs-kashiwa", "home":"Newcastle United Jets",
             "away":"Kashiwa Reysol", "status":"live",
             "kickoff":"2026-11-24T10:45:00+03:00", "elapsed_minute":76},
            {"id":"other-match", "home":"Newcastle", "away":"Kashiwa",
             "status":"live", "kickoff":"2026-11-24T10:45:00+03:00"}
        ]
        found = FootballProvider.find_matching_live_fixture(
            fixtures, "Newcastle United Jets", "Kashiwa Reysol",
            "2026-11-24T10:45:00+03:00")
        self.assertEqual(found["id"], "newcastle-jets-vs-kashiwa")
        self.assertEqual(found["elapsed_minute"], 76)

    def test_live_fixture_match_refuses_ambiguous_exact_pair_without_kickoff(self):
        fixtures = [
            {"id":"a", "home":"Newcastle Jets", "away":"Kashiwa Reysol", "status":"live"},
            {"id":"b", "home":"Newcastle Jets", "away":"Kashiwa Reysol", "status":"live"}
        ]
        self.assertIsNone(FootballProvider.find_matching_live_fixture(
            fixtures, "Newcastle Jets", "Kashiwa Reysol"))

    def test_live_fixture_match_tolerates_provider_club_suffixes(self):
        fixtures = [{
            "id":"chattogram-city-vs-brothers-union",
            "slug":"chattogram-city-vs-brothers-union",
            "home":"Chattogram City", "away":"Brothers Union Club",
            "status":"live", "kickoff":"2026-11-24T10:30:00+06:00",
            "elapsed_minute":32
        }]
        found = FootballProvider.find_matching_live_fixture(
            fixtures, "Chattogram City FC", "Brothers Union",
            "2026-11-24T10:30:00+06:00")
        self.assertEqual(found["slug"], "chattogram-city-vs-brothers-union")

    def test_live_fixture_match_does_not_match_different_token_names(self):
        fixtures = [{
            "id":"chattogram-vs-brothers", "home":"Chattogram Abahani",
            "away":"Brothers Union", "status":"live"
        }]
        self.assertIsNone(FootballProvider.find_matching_live_fixture(
            fixtures, "Chattogram City FC", "Brothers Union"))

    def test_match_slug_is_derived_in_sportscore_documented_format(self):
        self.assertEqual(
            FootballProvider.match_slug_from_teams("Shashemene Ketema", "Bishoftu Ketema"),
            "shashemene-ketema-vs-bishoftu-ketema",
        )
        self.assertEqual(
            FootballProvider.match_slug_from_teams("São Paulo U20 Women", "Criciúma U20 (W)"),
            "sao-paulo-u20-women-vs-criciuma-u20-w",
        )
        self.assertIsNone(FootballProvider.match_slug_from_teams("", "Bishoftu Ketema"))

if __name__ == "__main__":
    unittest.main()
