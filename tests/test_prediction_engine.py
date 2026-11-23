import unittest
from prediction_engine import predict, prepare_history

class PredictionEngineTests(unittest.TestCase):
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
