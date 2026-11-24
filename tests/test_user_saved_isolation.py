import os
import tempfile
import unittest
from datetime import date

import app as application
import auth


class SavedMatchIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = application.DB_PATH
        self.old_env_db_path = os.environ.get("DATABASE_PATH")
        database_path = os.path.join(self.tempdir.name, "multi-user-test.db")
        os.environ["DATABASE_PATH"] = database_path
        application.DB_PATH = database_path
        application.init_db()
        auth.init_auth_db()

        with auth._db() as conn:
            self.user_one = auth._execute(
                conn,
                "INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?) RETURNING id",
                ("one@example.com", "test-hash", "test"),
            ).fetchone()[0]
            self.user_two = auth._execute(
                conn,
                "INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?) RETURNING id",
                ("two@example.com", "test-hash", "test"),
            ).fetchone()[0]

        with application.db() as conn:
            self.match_one = conn.execute(
                "INSERT INTO matches(home,away,kickoff,status,created_at) VALUES(?,?,?,?,?) RETURNING id",
                ("Alpha FC", "Beta FC", date.today().isoformat() + "T23:50:00+03:00", "scheduled", "test"),
            ).fetchone()["id"]
            self.match_two = conn.execute(
                "INSERT INTO matches(home,away,kickoff,status,created_at) VALUES(?,?,?,?,?) RETURNING id",
                ("Gamma FC", "Delta FC", date.today().isoformat() + "T23:55:00+03:00", "scheduled", "test"),
            ).fetchone()["id"]

        self.client = application.app.test_client()

    def tearDown(self):
        application.DB_PATH = self.old_db_path
        if self.old_env_db_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self.old_env_db_path
        self.tempdir.cleanup()

    def _login_as(self, user_id, email):
        with self.client.session_transaction() as session:
            session.clear()
            session["user_id"] = user_id
            session["user_email"] = email
            session["_csrf_token"] = "isolation-test-token"

    def _save(self, match_id):
        return self.client.post(
            f"/match/{match_id}/save",
            headers={"X-CSRF-Token": "isolation-test-token"},
        )

    def test_saved_matches_are_isolated_between_accounts(self):
        self._login_as(self.user_one, "one@example.com")
        self.assertEqual(self._save(self.match_one).status_code, 302)
        first_account_page = self.client.get("/saved").get_data(as_text=True)
        self.assertIn("Alpha FC", first_account_page)
        self.assertNotIn("Gamma FC", first_account_page)

        self._login_as(self.user_two, "two@example.com")
        self.assertEqual(self._save(self.match_two).status_code, 302)
        second_account_page = self.client.get("/saved").get_data(as_text=True)
        self.assertIn("Gamma FC", second_account_page)
        self.assertNotIn("Alpha FC", second_account_page)

    def test_homepage_fixture_card_has_add_action_and_shows_saved_state(self):
        self._login_as(self.user_one, "one@example.com")
        day = date.today().isoformat()
        page = self.client.get(f"/?date={day}&view=upcoming").get_data(as_text=True)
        self.assertIn("＋ Ekle", page)
        self.assertIn(f'action="/match/{self.match_one}/save"', page)

        response = self.client.post(
            f"/match/{self.match_one}/save",
            data={
                "csrf_token": "isolation-test-token",
                "next": "bulletin",
                "day": day,
                "view": "upcoming",
            },
            headers={"X-CSRF-Token": "isolation-test-token"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/?date={day}&view=upcoming", response.headers["Location"])

        saved_page = self.client.get(f"/?date={day}&view=upcoming").get_data(as_text=True)
        self.assertIn("✓ Eklendi", saved_page)
        saved_row = saved_page.split(f'<tr data-match-id="{self.match_one}">', 1)[1].split("</tr>", 1)[0]
        other_row = saved_page.split(f'<tr data-match-id="{self.match_two}">', 1)[1].split("</tr>", 1)[0]
        self.assertNotIn("＋ Ekle", saved_row)
        self.assertIn("＋ Ekle", other_row)

    def test_guest_sees_add_action_that_leads_to_login(self):
        day = date.today().isoformat()
        page = self.client.get(f"/?date={day}&view=upcoming").get_data(as_text=True)
        self.assertIn("＋ Ekle", page)
        self.assertIn(f'href="/login?next=/match/{self.match_one}"', page)

    def test_daily_top_ten_cards_can_be_added_to_saved_matches(self):
        from unittest.mock import patch

        self._login_as(self.user_one, "one@example.com")
        day = date.today().isoformat()
        pick = {
            "id": self.match_one,
            "home": "Alpha FC",
            "away": "Beta FC",
            "league": "Test League",
            "kickoff": day + "T18:00:00+03:00",
            "market": "2,5 Gol",
            "prediction": "Alt",
            "confidence": 97,
        }

        with patch.object(application, "_daily_top_ten", return_value=([pick.copy()], 5)):
            page = self.client.get(f"/?date={day}&view=upcoming").get_data(as_text=True)
        self.assertIn('class="daily-ten-save-button">＋ Ekle</button>', page)
        self.assertIn(f'action="/match/{self.match_one}/save"', page)

        response = self.client.post(
            f"/match/{self.match_one}/save",
            data={
                "csrf_token": "isolation-test-token",
                "next": "bulletin",
                "day": day,
                "view": "upcoming",
            },
            headers={"X-CSRF-Token": "isolation-test-token"},
        )
        self.assertEqual(response.status_code, 302)
        with patch.object(application, "_daily_top_ten", return_value=([pick.copy()], 5)):
            saved_page = self.client.get(f"/?date={day}&view=upcoming").get_data(as_text=True)
        self.assertIn('class="daily-ten-saved">✓ Eklendi</span>', saved_page)
        self.assertNotIn('class="daily-ten-save-button">＋ Ekle</button>', saved_page)

        self._login_as(self.user_two, "two@example.com")
        with patch.object(application, "_daily_top_ten", return_value=([pick.copy()], 5)):
            other_account_page = self.client.get(f"/?date={day}&view=upcoming").get_data(as_text=True)
        self.assertIn('class="daily-ten-save-button">＋ Ekle</button>', other_account_page)
        self.assertNotIn('class="daily-ten-saved">✓ Eklendi</span>', other_account_page)

    def test_guest_daily_top_ten_add_action_redirects_to_login(self):
        from unittest.mock import patch

        day = date.today().isoformat()
        pick = {
            "id": self.match_one,
            "home": "Alpha FC",
            "away": "Beta FC",
            "league": "Test League",
            "kickoff": day + "T18:00:00+03:00",
            "market": "2,5 Gol",
            "prediction": "Alt",
            "confidence": 97,
        }
        with patch.object(application, "_daily_top_ten", return_value=([pick], 5)):
            page = self.client.get(f"/?date={day}&view=upcoming").get_data(as_text=True)
        self.assertIn('class="daily-ten-save-button" href="/login?next=/match/', page)
        self.assertIn("＋ Ekle</a>", page)

    def test_dashboard_removes_duplicate_metrics_and_daily_ten_is_collapsible(self):
        self._login_as(self.user_one, "one@example.com")
        day = date.today().isoformat()
        page = self.client.get(f"/?date={day}&view=upcoming").get_data(as_text=True)
        metric_grid = page.split('<section class="metric-grid">', 1)[1].split("</section>", 1)[0]
        self.assertNotIn("Bugünün Maçları", metric_grid)
        self.assertNotIn("Günün 10'lusu", metric_grid)
        self.assertIn('<details class="daily-ten-disclosure panel"', page)
        self.assertNotIn('id="gunun-10lusu" open', page)

    def test_live_metric_expands_to_show_teams_minute_and_score(self):
        self._login_as(self.user_one, "one@example.com")
        with application.db() as conn:
            conn.execute(
                "UPDATE matches SET status='live',elapsed_minute=67,home_goals=2,away_goals=1,league='Test League' WHERE id=?",
                (self.match_one,),
            )
        day = date.today().isoformat()
        page = self.client.get(f"/?date={day}&view=upcoming").get_data(as_text=True)
        self.assertIn('<details class="metric-card metric-green metric-disclosure">', page)
        self.assertIn("Alpha FC – Beta FC", page)
        self.assertIn("67'", page)
        self.assertIn("2 – 1", page)
        self.assertIn("Test League", page)

    def test_detail_featured_goal_prediction_displays_only_alt_or_ust(self):
        with application.db() as conn:
            conn.execute(
                "UPDATE matches SET status='finished',over25=?,prediction_confidence=? WHERE id=?",
                ("Alt 2,5", '{"over25":82}', self.match_one),
            )
        page = self.client.get(f"/match/{self.match_one}").get_data(as_text=True)
        self.assertIn("<td>2,5 Gol</td><td><b>Alt</b></td>", page)
        self.assertNotIn("<b>Alt 2,5</b>", page)

    def test_results_page_restores_green_win_and_red_loss_indicators(self):
        with application.db() as conn:
            conn.execute(
                "UPDATE matches SET status='finished',home_goals=2,away_goals=1 WHERE id=?",
                (self.match_one,),
            )
            conn.executemany(
                "INSERT INTO results(match_id,market,prediction,actual,outcome) VALUES(?,?,?,?,?)",
                [
                    (self.match_one, "Maç Sonucu", "MS 1", "MS 1", "won"),
                    (self.match_one, "KG", "Var", "Yok", "lost"),
                ],
            )
        page = self.client.get("/results").get_data(as_text=True)
        self.assertIn('class="result-outcome result-won"', page)
        self.assertIn("✓", page)
        self.assertIn('class="result-outcome result-lost"', page)
        self.assertIn("×", page)

    def test_homepage_has_team_name_search_for_fixture_rows(self):
        self._login_as(self.user_one, "one@example.com")
        day = date.today().isoformat()
        page = self.client.get(f"/?date={day}&view=upcoming").get_data(as_text=True)
        self.assertIn('id="fixtureTeamSearch"', page)
        self.assertIn('placeholder="Takım adı yaz, maçı bul..."', page)
        self.assertIn("fixtureSearchStatus", page)
        self.assertIn("row.querySelector('.team-pair')", page)
        self.assertIn("Bu takım adına göre maç bulunamadı.", page)

    def test_save_returns_to_match_and_detail_has_navigation_links(self):
        self._login_as(self.user_one, "one@example.com")
        response = self._save(self.match_one)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/match/{self.match_one}")

        detail = self.client.get(f"/match/{self.match_one}").get_data(as_text=True)
        self.assertIn("Bültene dön", detail)
        self.assertIn("Kaydedilen maçlar", detail)
        self.assertIn("★ Kaydedildi", detail)

    def test_saved_page_has_mobile_friendly_return_and_remove_actions(self):
        self._login_as(self.user_one, "one@example.com")
        self._save(self.match_one)
        response = self.client.get("/saved")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Bültene dön", page)
        self.assertIn("Alpha FC", page)
        self.assertIn("Listemden çıkar", page)

    def test_individual_coupon_prediction_saves_without_redirect_and_is_account_scoped(self):
        self._login_as(self.user_one, "one@example.com")
        with application.db() as conn:
            conn.execute(
                "UPDATE matches SET market_probabilities=? WHERE id=?",
                ('{"goals":{"2.5":{"over":0.82,"under":0.18}}}', self.match_one),
            )
        response = self.client.post(
            f"/match/{self.match_one}/save-prediction",
            data={"csrf_token":"isolation-test-token","market_key":"goals","selection_key":"over"},
            headers={"X-CSRF-Token":"isolation-test-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["saved"])
        with application.db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM user_saved_predictions WHERE user_id=? AND match_id=?",
                (self.user_one, self.match_one),
            ).fetchone()[0]
        self.assertEqual(count, 1)
        page = self.client.get("/saved").get_data(as_text=True)
        self.assertIn("Kaydettiğim tahminler", page)
        self.assertIn("Toplam Gol (2,5)", page)
        self.assertIn("Üst", page)
        self.assertNotIn("goals · over", page)
        self.assertNotIn("btts ·", page)

        self._login_as(self.user_two, "two@example.com")
        other_page = self.client.get("/saved").get_data(as_text=True)
        self.assertNotIn("Alpha FC", other_page)

    def test_saved_prediction_market_names_are_turkish(self):
        self._login_as(self.user_one, "one@example.com")
        with application.db() as conn:
            conn.executemany(
                "INSERT INTO user_saved_predictions(user_id,match_id,market_key,selection_key,saved_at) VALUES(?,?,?,?,?)",
                [
                    (self.user_one,self.match_one,"btts","Var","test"),
                    (self.user_one,self.match_one,"match_result","1","test"),
                ],
            )
        page = self.client.get("/saved").get_data(as_text=True)
        self.assertIn("Karşılıklı Gol (KG) · Var", page)
        self.assertIn("Maç Sonucu · MS 1", page)
        self.assertNotIn("btts · Var", page)
        self.assertNotIn("match_result · 1", page)

    def test_individual_prediction_save_is_idempotent(self):
        self._login_as(self.user_one, "one@example.com")
        with application.db() as conn:
            conn.execute(
                "UPDATE matches SET market_probabilities=? WHERE id=?",
                ('{"btts":{"Var":0.75,"Yok":0.25}}', self.match_one),
            )
        for _ in range(2):
            response = self.client.post(
                f"/match/{self.match_one}/save-prediction",
                data={"csrf_token":"isolation-test-token","market_key":"btts","selection_key":"Var"},
                headers={"X-CSRF-Token":"isolation-test-token"},
            )
            self.assertEqual(response.status_code, 200)
        with application.db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM user_saved_predictions WHERE user_id=? AND match_id=? AND market_key='btts' AND selection_key='Var'",
                (self.user_one, self.match_one),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_results_page_groups_markets_under_one_match_and_shows_half_full_time_scores(self):
        with application.db() as conn:
            conn.execute(
                "UPDATE matches SET status='finished',ht_home=1,ht_away=0,home_goals=2,away_goals=1 WHERE id=?",
                (self.match_one,),
            )
            conn.executemany(
                "INSERT INTO results(match_id,market,prediction,actual,outcome) VALUES(?,?,?,?,?)",
                [
                    (self.match_one,"2,5 Gol","Üst 2,5","Üst 2,5","won"),
                    (self.match_one,"KG","Var","Var","won"),
                    (self.match_one,"Maç Sonucu","MS 1","MS 1","won"),
                ],
            )
        page = self.client.get("/results").get_data(as_text=True)
        self.assertEqual(page.count("Alpha FC – Beta FC"), 1)
        self.assertIn("<th>İY</th>", page)
        self.assertIn("<th>MS</th>", page)
        self.assertIn("1 – 0", page)
        self.assertIn("2 – 1", page)
        self.assertIn("3 bahis", page)
        self.assertIn("<th>Bahis türü</th>", page)
        self.assertIn("Gerçekleşen", page)

    def test_results_page_shows_date_before_match_name(self):
        with application.db() as conn:
            conn.execute(
                "UPDATE matches SET status='finished',home_goals=1,away_goals=0 WHERE id=?",
                (self.match_one,),
            )
            conn.execute(
                "INSERT INTO results(match_id,market,prediction,actual,outcome) VALUES(?,?,?,?,?)",
                (self.match_one,"Maç Sonucu","MS 1","MS 1","won"),
            )
        page = self.client.get("/results").get_data(as_text=True)
        self.assertIn("<th>Tarih</th>", page)
        self.assertIn("Alpha FC – Beta FC", page)
        self.assertIn(date.today().strftime("%d.%m.%Y"), page)

    def test_goal_total_results_hide_repeated_line_from_prediction_and_actual(self):
        with application.db() as conn:
            conn.execute(
                "UPDATE matches SET status='finished',home_goals=3,away_goals=2 WHERE id=?",
                (self.match_one,),
            )
            conn.executemany(
                "INSERT INTO results(match_id,market,prediction,actual,outcome) VALUES(?,?,?,?,?)",
                [
                    (self.match_one,"1,5 Gol","Üst 1,5","Üst 1,5","won"),
                    (self.match_one,"2,5 Gol","Üst 2,5","Üst 2,5","won"),
                    (self.match_one,"3,5 Gol","Alt 3,5","Üst 3,5","lost"),
                    (self.match_one,"4,5 Gol","Alt 4,5","Alt 4,5","won"),
                ],
            )
        page = self.client.get("/results").get_data(as_text=True)
        self.assertIn("<td>1,5 Gol</td><td>Üst</td><td>Üst</td>", page)
        self.assertIn("<td>2,5 Gol</td><td>Üst</td><td>Üst</td>", page)
        self.assertIn("<td>3,5 Gol</td><td>Alt</td><td>Üst</td>", page)
        self.assertIn("<td>4,5 Gol</td><td>Alt</td><td>Alt</td>", page)

    def test_regular_user_does_not_see_admin_match_controls(self):
        self._login_as(self.user_one, "one@example.com")
        response = self.client.get(f"/match/{self.match_one}")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('action="/match/%s/status"' % self.match_one, page)
        self.assertNotIn('action="/match/%s/result"' % self.match_one, page)



    def test_daily_top_ten_uses_at_least_five_fixtures_and_max_two_picks_per_fixture(self):
        import json

        rows = []
        for number in range(6):
            rows.append({
                "id": number + 100,
                "home": f"Home {number}",
                "away": f"Away {number}",
                "league": "Test League",
                "kickoff": f"2026-11-24T{15 + number:02d}:00:00+03:00",
                "prediction_confidence": json.dumps({}),
                "market_probabilities": json.dumps({
                    "match_result": {"1": 0.86 - number * 0.01, "X": 0.08, "2": 0.06},
                    "goals": {
                        "1.5": {"over": 0.79, "under": 0.21},
                        "2.5": {"over": 0.68, "under": 0.32},
                        "3.5": {"over": 0.42, "under": 0.58},
                        "4.5": {"over": 0.22, "under": 0.78},
                    },
                    "btts": {"Var": 0.71, "Yok": 0.29},
                }),
                "match_result": "MS 1",
                "over25": "Üst 2,5",
                "btts": "Var",
                "first_half_btts": "",
                "second_half_btts": "",
                "htft": "",
            })

        duplicate = dict(rows[0])
        duplicate["id"] = 999
        rows.append(duplicate)

        picks, eligible_matches = application._daily_top_ten(rows)
        self.assertEqual(eligible_matches, 6)
        self.assertEqual(len(picks), 10)
        counts = {}
        for pick in picks:
            counts[pick["id"]] = counts.get(pick["id"], 0) + 1
        self.assertGreaterEqual(len(counts), 5)
        self.assertLessEqual(max(counts.values()), 2)

    def test_daily_top_ten_waits_until_five_distinct_matches_are_eligible(self):
        rows = []
        for number in range(4):
            rows.append({
                "id": number + 200,
                "home": f"Home {number}",
                "away": f"Away {number}",
                "league": "Test League",
                "kickoff": f"2026-11-24T{15 + number:02d}:00:00+03:00",
                "prediction_confidence": "{}",
                "market_probabilities": '{"match_result":{"1":0.8,"X":0.1,"2":0.1}}',
                "match_result": "MS 1",
            })
        picks, eligible_matches = application._daily_top_ten(rows)
        self.assertEqual(picks, [])
        self.assertEqual(eligible_matches, 4)

if __name__ == "__main__":
    unittest.main()
