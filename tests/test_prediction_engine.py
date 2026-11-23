import unittest
from prediction_engine import predict, prepare_history

class PredictionEngineTests(unittest.TestCase):
    def test_turkish_club_aliases_and_league_label_fallback(self):
        history = []
        # The provider uses different spellings from the upcoming fixture.
        for i in range(6):
            history.append({
                "id": f"b{i}", "home": "Başakşehir", "away": f"Rakip {i}",
                "league": "Türkiye Süper Lig", "status": "finished",
                "home_goals": 2, "away_goals": 0, "ht_home": 1, "ht_away": 0
            })
            history.append({
                "id": f"c{i}", "home": f"Rakip {i}", "away": "Arca Çorum FK",
                "league": "Türkiye Süper Lig", "status": "finished",
                "home_goals": 1, "away_goals": 2, "ht_home": 0, "ht_away": 1
            })
        # Ensure the target league has >=12 fixtures, which previously forced
        # an exact league-label lookup and hid the teams' valid match history.
        for i in range(12):
            history.append({
                "id": f"other{i}", "home": f"Other {i}", "away": f"Club {i}",
                "league": "Turkish Super League", "status": "finished",
                "home_goals": 1, "away_goals": 1, "ht_home": 0, "ht_away": 0
            })

        match = {
            "id": "future-alias", "home": "Başakşehir Futbol Kulübü",
            "away": "Çorum Belediyespor", "league": "Turkish Super League",
            "status": "scheduled"
        }
        data, message = predict(match, prepare_history(history))
        self.assertIsNone(message)
        self.assertIsNotNone(data)
        self.assertIn("match_result", data["predictions"])
        self.assertIn("Son 10 maçlık form", data["notes"])
        self.assertIn("Başakşehir Futbol Kulübü 6 maç", data["notes"])
        self.assertIn("Çorum Belediyespor 6 maç", data["notes"])

    def test_expanded_istanbul_and_corum_name_variants_match_same_club(self):
        from prediction_engine import _normalize_team
        variants = {
            "basaksehir": [
                "Başakşehir", "İstanbul Başakşehir FK",
                "İstanbul Başakşehir Futbol Kulübü", "Başakşehir Futbol Kulübü"
            ],
            "corum": [
                "Çorum", "Çorum FK", "Arca Çorum FK",
                "Çorum Belediyespor", "Arca Çorum Futbol Kulübü"
            ],
        }
        for canonical, names in variants.items():
            for name in names:
                with self.subTest(name=name):
                    self.assertEqual(_normalize_team(name), canonical)

    def test_prediction_uses_finished_history_for_both_teams(self):
        history = []
        for i in range(4):
            history.append({
                "id": f"h{i}", "home": "Home FC", "away": f"Opponent {i}",
                "league": "Test League", "status": "finished",
                "home_goals": 2, "away_goals": 1, "ht_home": 1, "ht_away": 0
            })
            history.append({
                "id": f"a{i}", "home": f"Opponent {i}", "away": "Away FC",
                "league": "Test League", "status": "finished",
                "home_goals": 0, "away_goals": 1, "ht_home": 0, "ht_away": 0
            })

        match = {
            "id": "future-1", "home": "Home", "away": "Away",
            "league": "Test League", "status": "scheduled"
        }
        indexed_history = prepare_history(history)
        data, message = predict(match, indexed_history)
        second_data, second_message = predict(match, indexed_history)

        self.assertIsNone(message)
        self.assertIsNone(second_message)
        self.assertEqual(data["predictions"], second_data["predictions"])
        self.assertIsNotNone(data)
        self.assertIn("match_result", data["predictions"])
        self.assertIn("over25", data["predictions"])
        self.assertIn("btts", data["predictions"])
        self.assertIn("htft", data["predictions"])
        markets = data["market_probabilities"]
        self.assertEqual(set(markets["match_result"]), {"1", "X", "2"})
        self.assertEqual(set(markets["goals"]), {"0.5", "1.5", "2.5", "3.5", "4.5"})
        self.assertEqual(set(markets["btts"]), {"Var", "Yok"})
        self.assertIsNotNone(markets["first_half_result"])
        self.assertEqual(set(markets["htft"]), {"1/1", "1/X", "1/2", "X/1", "X/X", "X/2", "2/1", "2/X", "2/2"})
        self.assertFalse(markets["corners"]["available"])
        for value in markets["match_result"].values():
            self.assertGreaterEqual(value, 0)
            self.assertLessEqual(value, 1)

if __name__ == "__main__":
    unittest.main()
