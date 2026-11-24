import os
import tempfile
import unittest
from unittest.mock import patch

import app as application
from football_provider import FootballProvider


class TeamNameResolutionTests(unittest.TestCase):
    def setUp(self):
        FootballProvider._unused = None
        import football_provider
        football_provider._TEAM_NAME_LOOKUP_CACHE.clear()

    def test_detects_opaque_provider_team_labels_only(self):
        self.assertTrue(FootballProvider.is_placeholder_team_name("Team zp5rzgh18vdq82w"))
        self.assertTrue(FootballProvider.is_placeholder_team_name("Team 1l4rjnehvopm7vx"))
        self.assertTrue(FootballProvider.is_placeholder_team_name("TEA"))
        self.assertFalse(FootballProvider.is_placeholder_team_name("Al-Sufiya SC"))
        self.assertFalse(FootballProvider.is_placeholder_team_name("Team A"))

    def test_fixture_parser_resolves_names_only_when_search_matches_exact_team_reference(self):
        class FakeResponse:
            ok = True
            status_code = 200

            def json(self):
                return {
                    "teams": [
                        {"id": "home-opaque-123456", "slug": "al-sufiya", "name": "Al-Sufiya SC"},
                        {"id": "away-opaque-123456", "slug": "al-alam", "name": "Al Alam SC"},
                    ]
                }

        class FakeSession:
            def __init__(self):
                self.queries = []

            def get(self, url, params, timeout):
                self.queries.append((url, params, timeout))
                return FakeResponse()

        provider = FootballProvider()
        fake = FakeSession()
        provider.session = fake
        rows = provider.parse_fixtures_payload({
            "fixtures": [{
                "id": "opaque-fixture",
                "home": {"id": "home-opaque-123456", "name": "Team home-opaque-123456"},
                "away": {"id": "away-opaque-123456", "name": "Team away-opaque-123456"},
                "competition": {"name": "Iraq Premier Division"},
                "date": "2026-11-24T11:00:00Z",
                "status": "live",
            }]
        })

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["home"], "Al-Sufiya SC")
        self.assertEqual(rows[0]["away"], "Al Alam SC")
        self.assertFalse(rows[0]["home_name_placeholder"])
        self.assertFalse(rows[0]["away_name_placeholder"])
        self.assertEqual(rows[0]["home_team_ref"], "home-opaque-123456")
        self.assertEqual(len(fake.queries), 2)
        self.assertTrue(all(query[2] == 3 for query in fake.queries))

    def test_fixture_parser_does_not_guess_when_search_result_has_different_id(self):
        class FakeResponse:
            ok = True
            status_code = 200

            def json(self):
                return {
                    "teams": [
                        {"id": "different-team-id", "slug": "al-sufiya", "name": "Al-Sufiya SC"},
                    ]
                }

        class FakeSession:
            def get(self, url, params, timeout):
                return FakeResponse()

        provider = FootballProvider()
        provider.session = FakeSession()
        rows = provider.parse_fixtures_payload({
            "fixtures": [{
                "id": "opaque-fixture",
                "home": {"id": "home-opaque-654321", "name": "Team home-opaque-654321"},
                "away": {"name": "Al Taji"},
                "competition": {"name": "Iraq Premier Division"},
                "date": "2026-11-24T11:00:00Z",
                "status": "scheduled",
            }]
        })

        self.assertEqual(rows[0]["home"], "Team home-opaque-654321")
        self.assertTrue(rows[0]["home_name_placeholder"])

    def test_parser_can_skip_external_name_lookups_for_bulk_or_offline_use(self):
        class NoNetworkSession:
            def get(self, *args, **kwargs):
                raise AssertionError("External lookup should not run")

        provider = FootballProvider()
        provider.session = NoNetworkSession()
        rows = provider.parse_fixtures_payload({
            "fixtures": [{
                "id": "offline-fixture",
                "home": "Team opaque-team-id-1234",
                "away": "Al Taji",
                "competition": "Iraq Premier Division",
                "date": "2026-11-24T11:00:00Z",
                "status": "scheduled",
            }]
        }, resolve_placeholders=False)

        self.assertEqual(rows[0]["home"], "Team opaque-team-id-1234")
        self.assertTrue(rows[0]["home_name_placeholder"])

    def test_existing_verified_names_are_not_overwritten_by_provider_placeholders(self):
        tempdir = tempfile.TemporaryDirectory()
        old_db_path = application.DB_PATH
        old_database_path = os.environ.get("DATABASE_PATH")
        database_path = os.path.join(tempdir.name, "team-names.db")
        application.DB_PATH = database_path
        os.environ["DATABASE_PATH"] = database_path
        try:
            application.init_db()
            kickoff = "2026-11-24T14:00:00+03:00"
            real_fixture = {
                "id": "same-fixture-id",
                "home": "Al-Sufiya SC",
                "away": "Al Alam SC",
                "league": "Iraq Premier Division",
                "kickoff": kickoff,
                "status": "scheduled",
            }
            placeholder_fixture = {
                **real_fixture,
                "home": "Team zp5rzgh18vdq82w",
                "away": "Team 1l4rjnehvopm7vx",
                "status": "live",
                "home_goals": 0,
                "away_goals": 0,
            }
            with application.db() as conn:
                first_id = application.upsert_fixture_record(conn, real_fixture)
                second_id = application.upsert_fixture_record(conn, placeholder_fixture)
                row = conn.execute(
                    "SELECT home,away,status,home_goals,away_goals FROM matches WHERE id=?",
                    (first_id,),
                ).fetchone()

            self.assertEqual(first_id, second_id)
            self.assertEqual(row["home"], "Al-Sufiya SC")
            self.assertEqual(row["away"], "Al Alam SC")
            self.assertEqual(row["status"], "live")
            self.assertEqual((row["home_goals"], row["away_goals"]), (0, 0))
        finally:
            application.DB_PATH = old_db_path
            if old_database_path is None:
                os.environ.pop("DATABASE_PATH", None)
            else:
                os.environ["DATABASE_PATH"] = old_database_path
            tempdir.cleanup()

    def test_team_display_filter_hides_provider_hash_labels(self):
        self.assertEqual(
            application.team_display("Team zp5rzgh18vdq82w"),
            "Takım adı doğrulanamadı",
        )
        self.assertEqual(application.team_display("Al-Sufiya SC"), "Al-Sufiya SC")


if __name__ == "__main__":
    unittest.main()
