import unittest
from prediction_engine import predict

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
            "id": "future-1", "home": "Home FC", "away": "Away FC",
            "league": "Test League", "status": "scheduled"
        }
        data, message = predict(match, history)

        self.assertIsNone(message)
        self.assertIsNotNone(data)
        self.assertIn("match_result", data["predictions"])
        self.assertIn("over25", data["predictions"])
        self.assertIn("btts", data["predictions"])
        self.assertIn("htft", data["predictions"])

if __name__ == "__main__":
    unittest.main()
