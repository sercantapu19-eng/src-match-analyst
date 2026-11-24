import unittest

from result_sync import actual_market_values, evaluate_prediction


class ResultSyncTests(unittest.TestCase):
    def test_full_time_markets(self):
        actual = actual_market_values({"home_goals": 2, "away_goals": 1})
        self.assertEqual(actual["match_result"], "MS 1")
        self.assertEqual(actual["over25"], "Üst 2,5")
        self.assertEqual(actual["btts"], "Var")

    def test_draw_and_under_without_btts(self):
        actual = actual_market_values({"home_goals": 0, "away_goals": 0})
        self.assertEqual(actual["match_result"], "MS X")
        self.assertEqual(actual["over25"], "Alt 2,5")
        self.assertEqual(actual["btts"], "Yok")

    def test_half_markets_only_when_half_score_exists(self):
        actual = actual_market_values({
            "home_goals": 2, "away_goals": 1, "ht_home": 0, "ht_away": 1
        })
        self.assertEqual(actual["htft"], "2/1")
        self.assertEqual(actual["first_half_btts"], "Yok")
        self.assertEqual(actual["second_half_btts"], "Yok")

    def test_missing_full_time_score_is_not_resultable(self):
        self.assertEqual(actual_market_values({"home_goals": 1, "away_goals": None}), {})

    def test_missing_half_time_score_does_not_invent_half_markets(self):
        actual = actual_market_values({"home_goals": 2, "away_goals": 1})
        self.assertNotIn("htft", actual)
        self.assertNotIn("first_half_btts", actual)
        self.assertNotIn("second_half_btts", actual)

    def test_prediction_comparison(self):
        self.assertEqual(evaluate_prediction("Üst 2,5", "Üst 2,5"), "won")
        self.assertEqual(evaluate_prediction("MS 2", "MS 1"), "lost")


if __name__ == "__main__":
    unittest.main()
