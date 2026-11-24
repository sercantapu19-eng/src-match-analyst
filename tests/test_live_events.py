import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import app as application
from football_provider import FootballProvider


class LiveEventParserTests(unittest.TestCase):
    def test_parses_goals_cards_substitutions_penalties_and_var(self):
        events = FootballProvider.parse_match_events({
            "events": [
                {"id": "g1", "type": "goal", "minute": "12", "player": {"name": "Scorer"},
                 "team": {"name": "Alpha FC"}},
                {"id": "y1", "type": "yellow_card", "minute": "45+2", "player": "Booked",
                 "team": "Beta FC"},
                {"id": "s1", "type": "substitution", "minute": 63,
                 "player_in": {"name": "Incoming"}, "player_out": {"name": "Outgoing"},
                 "team": "Alpha FC"},
                {"id": "p1", "type": "penalty_missed", "minute": 77, "player": "Shooter"},
                {"id": "v1", "type": "var_goal_cancelled", "minute": 80, "player": "Scorer",
                 "related_event_id": "g1"},
            ]
        })
        by_id = {event["event_uid"]: event for event in events}
        self.assertEqual(by_id["sportscore:g1"]["kind"], "goal")
        self.assertTrue(by_id["sportscore:g1"]["counts_for_score"])
        self.assertEqual(by_id["sportscore:y1"]["minute"], "45+2")
        self.assertEqual(by_id["sportscore:s1"]["kind"], "substitution")
        self.assertIn("Incoming", by_id["sportscore:s1"]["detail"])
        self.assertEqual(by_id["sportscore:p1"]["kind"], "penalty_missed")
        self.assertEqual(by_id["sportscore:v1"]["kind"], "goal_cancelled")
        self.assertEqual(by_id["sportscore:v1"]["related_event_uid"], "sportscore:g1")

    def test_fallback_event_identity_is_stable(self):
        payload = {"events": [
            {"type": "goal", "minute": 21, "player": "Scorer", "team": "Alpha"}
        ]}
        first = FootballProvider.parse_match_events(payload)
        second = FootballProvider.parse_match_events(payload)
        self.assertEqual(first[0]["event_uid"], second[0]["event_uid"])
        self.assertTrue(first[0]["event_uid"].startswith("fallback:"))


class LiveEventCacheRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = application.DB_PATH
        self.old_database_url = os.environ.pop("DATABASE_URL", None)
        self.old_env_db_path = os.environ.get("DATABASE_PATH")
        self.db_path = os.path.join(self.tempdir.name, "live-events-test.db")
        os.environ["DATABASE_PATH"] = self.db_path
        application.DB_PATH = self.db_path
        application.init_db()
        with application.db() as conn:
            self.match_id = conn.execute(
                """INSERT INTO matches(home,away,league,kickoff,status,status_short,created_at)
                   VALUES(?,?,?,?,?,?,?) RETURNING id""",
                ("Alpha FC", "Beta FC", "Test League",
                 date.today().isoformat() + "T18:00:00+03:00", "live", "1H", "test"),
            ).fetchone()["id"]
        self.client = application.app.test_client()

    def tearDown(self):
        application.DB_PATH = self.old_db_path
        if self.old_database_url is not None:
            os.environ["DATABASE_URL"] = self.old_database_url
        if self.old_env_db_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self.old_env_db_path
        self.tempdir.cleanup()

    def test_same_match_viewers_share_provider_fetch_and_persist_events(self):
        class FakeProvider:
            live_calls = 0
            detail_calls = 0
            _normalize_search_name = staticmethod(FootballProvider._normalize_search_name)
            find_matching_live_fixture = staticmethod(FootballProvider.find_matching_live_fixture)
            parse_match_events = staticmethod(FootballProvider.parse_match_events)

            def __init__(self, api_key=None):
                pass

            def live_matches(self, limit=50):
                type(self).live_calls += 1
                return [{
                    "id": "alpha-vs-beta", "slug": "alpha-vs-beta",
                    "home": "Alpha FC", "away": "Beta FC", "status": "live",
                    "status_short": "1H", "elapsed_minute": 17,
                    "home_goals": 1, "away_goals": 0,
                    "kickoff": date.today().isoformat() + "T15:00:00+00:00",
                }]

            def match_detail(self, slug):
                type(self).detail_calls += 1
                return {"events": [{
                    "id": "goal-17", "type": "goal", "minute": 17,
                    "player": "Alpha Scorer", "team": "Alpha FC",
                }]}

        with patch.object(application, "FootballProvider", FakeProvider):
            first = self.client.get(f"/match/{self.match_id}/live-data")
            second = self.client.get(f"/match/{self.match_id}/live-data")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(FakeProvider.live_calls, 1)
        self.assertEqual(FakeProvider.detail_calls, 1)
        payload = first.get_json()
        self.assertEqual(payload["status"], "first_half")
        self.assertEqual(payload["minute"], 17)
        self.assertEqual(payload["score"], "1 – 0")
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["event_uid"], "sportscore:goal-17")
        self.assertEqual(payload["events"][0]["team"], "home")
        with application.db() as conn:
            stored = conn.execute(
                "SELECT COUNT(*) AS count FROM match_events WHERE match_id=?",
                (self.match_id,),
            ).fetchone()["count"]
        self.assertEqual(stored, 1)

    def test_date_fixture_fallback_matches_live_game_missing_from_widget_feed(self):
        class FakeProvider:
            detail_calls = 0
            _normalize_search_name = staticmethod(FootballProvider._normalize_search_name)
            find_matching_live_fixture = staticmethod(FootballProvider.find_matching_live_fixture)
            parse_match_events = staticmethod(FootballProvider.parse_match_events)

            def __init__(self, api_key=None):
                pass

            def live_matches(self, limit=50):
                return []

            def fixtures(self, day, status=None):
                self.assertion_day = day
                self.assertion_status = status
                return [{
                    "id": "sportscore-match-38887",
                    "slug": "buriram-united-vs-shanghai-port",
                    "home": "Alpha FC",
                    "away": "Beta FC",
                    "status": "live",
                    "status_short": "2H",
                    "elapsed_minute": 91,
                    "home_goals": 3,
                    "away_goals": 1,
                    "kickoff": day + "T18:00:00+03:00",
                }]

            def match_detail(self, slug):
                type(self).detail_calls += 1
                self.assertion_slug = slug
                return {"events": [{
                    "id": "goal-69", "type": "goal", "minute": 69,
                    "player": "Prince Ampem", "team": "Alpha FC",
                }]}

        with patch.object(application, "FootballProvider", FakeProvider):
            response = self.client.get(f"/match/{self.match_id}/live-data")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["score"], "3 – 1")
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["primary_player"], "Prince Ampem")
        self.assertEqual(FakeProvider.detail_calls, 1)
        with application.db() as conn:
            state = conn.execute(
                "SELECT provider_slug FROM match_live_state WHERE match_id=?",
                (self.match_id,),
            ).fetchone()
            stored = conn.execute(
                "SELECT COUNT(*) AS count FROM match_events WHERE match_id=?",
                (self.match_id,),
            ).fetchone()["count"]
        self.assertEqual(state["provider_slug"], "buriram-united-vs-shanghai-port")
        self.assertEqual(stored, 1)

    def test_team_schedule_recovers_slug_when_date_fixture_has_no_identifier(self):
        class FakeProvider:
            detail_calls = 0
            team_schedule_calls = 0
            _normalize_search_name = staticmethod(FootballProvider._normalize_search_name)
            find_matching_live_fixture = staticmethod(FootballProvider.find_matching_live_fixture)
            parse_match_events = staticmethod(FootballProvider.parse_match_events)

            def __init__(self, api_key=None):
                pass

            def live_matches(self, limit=50):
                return []

            def fixtures(self, day, status=None):
                return [{
                    "id": "", "slug": "", "home": "Alpha FC", "away": "Beta FC",
                    "status": "live", "status_short": "LIVE", "elapsed_minute": None,
                    "home_goals": 0, "away_goals": 0,
                    "kickoff": day + "T18:00:00+03:00",
                }]

            def team_fixtures_for_team(self, team_name, limit=30):
                type(self).team_schedule_calls += 1
                return [{
                    "id": "", "slug": "",
                    "home": "Alpha FC", "away": "Beta FC", "status": "scheduled",
                    "status_short": "NS", "elapsed_minute": None,
                    "home_goals": None, "away_goals": None,
                    "kickoff": "2026-11-24T18:00:00+03:00",
                }]

            def match_detail(self, slug):
                type(self).detail_calls += 1
                self.assertion_slug = slug
                return {"events": [{
                    "id": "goal-12", "type": "goal", "minute": 12,
                    "player": "Test Scorer", "team": "Alpha FC",
                }]}

        with patch.object(application, "FootballProvider", FakeProvider):
            response = self.client.get(f"/match/{self.match_id}/live-data")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["primary_player"], "Test Scorer")
        self.assertEqual(FakeProvider.detail_calls, 1)
        self.assertEqual(FakeProvider.team_schedule_calls, 1)
        with application.db() as conn:
            state = conn.execute(
                "SELECT provider_slug FROM match_live_state WHERE match_id=?",
                (self.match_id,),
            ).fetchone()
        self.assertEqual(state["provider_slug"], "alpha-fc-vs-beta-fc")

    def test_finished_match_recovers_missing_slug_from_team_schedule(self):
        with application.db() as conn:
            conn.execute("UPDATE matches SET status='finished',status_short='FT' WHERE id=?",
                         (self.match_id,))

        class FakeProvider:
            detail_calls = 0
            _normalize_search_name = staticmethod(FootballProvider._normalize_search_name)
            find_matching_live_fixture = staticmethod(FootballProvider.find_matching_live_fixture)
            match_slug_from_teams = staticmethod(FootballProvider.match_slug_from_teams)
            parse_match_events = staticmethod(FootballProvider.parse_match_events)

            def __init__(self, api_key=None):
                pass

            def team_fixtures_for_team(self, team_name, limit=30):
                return [{
                    "id": "", "slug": "", "home": "Alpha FC", "away": "Beta FC",
                    "status": "finished", "status_short": "FT",
                    "kickoff": date.today().isoformat() + "T18:00:00+03:00",
                }]

            def match_detail(self, slug):
                type(self).detail_calls += 1
                self.assertion_slug = slug
                return {"events": [{
                    "id": "final-goal", "type": "goal", "minute": 74,
                    "player": "Final Scorer", "team": "Beta FC",
                }]}

        with patch.object(application, "FootballProvider", FakeProvider):
            response = self.client.get(f"/match/{self.match_id}/live-data")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "finished")
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["primary_player"], "Final Scorer")
        self.assertEqual(FakeProvider.detail_calls, 1)
        with application.db() as conn:
            state = conn.execute(
                "SELECT provider_slug FROM match_live_state WHERE match_id=?",
                (self.match_id,),
            ).fetchone()
        self.assertEqual(state["provider_slug"], "alpha-fc-vs-beta-fc")

    def test_duplicate_event_upsert_and_explicit_var_cancellation_preserve_history(self):
        events = FootballProvider.parse_match_events({
            "events": [
                {"id": "goal-1", "type": "goal", "minute": 22, "player": "Scorer", "team": "Alpha FC"},
                {"id": "var-1", "type": "var_goal_cancelled", "minute": 23,
                 "related_event_id": "goal-1", "detail": "Ofsayt nedeniyle gol iptal edildi"},
            ]
        })
        cancellation = next(event for event in events if event["kind"] == "goal_cancelled")
        self.assertEqual(cancellation["related_event_uid"], "sportscore:goal-1")
        application._store_match_events(self.match_id, events, "Alpha FC", "Beta FC")
        application._store_match_events(self.match_id, events, "Alpha FC", "Beta FC")
        with application.db() as conn:
            rows = conn.execute(
                "SELECT event_uid,event_type,event_status,counts_for_score FROM match_events WHERE match_id=? ORDER BY event_type",
                (self.match_id,),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        goal = next(row for row in rows if row["event_uid"] == "sportscore:goal-1")
        self.assertEqual(goal["event_status"], "cancelled")
        self.assertFalse(bool(goal["counts_for_score"]))

    def test_finished_match_keeps_saved_event_history_without_live_api_call(self):
        with application.db() as conn:
            conn.execute("UPDATE matches SET status='finished',status_short='FT' WHERE id=?",
                         (self.match_id,))
        application._store_match_events(self.match_id, FootballProvider.parse_match_events({
            "events": [{"id": "g-final", "type": "goal", "minute": 90, "player": "Scorer"}]
        }), "Alpha FC", "Beta FC")
        response = self.client.get(f"/match/{self.match_id}/live-data")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "finished")
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["event_uid"], "sportscore:g-final")


if __name__ == "__main__":
    unittest.main()
