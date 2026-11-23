import unittest
import app as application


class PostgresAdapterTests(unittest.TestCase):
    def test_sqlite_placeholders_become_postgres_placeholders(self):
        sql = application.translate_postgres_sql(
            "SELECT * FROM matches WHERE id=? AND status=?"
        )
        self.assertEqual(sql, "SELECT * FROM matches WHERE id=%s AND status=%s")

    def test_insert_or_ignore_becomes_postgres_conflict_clause(self):
        sql = application.translate_postgres_sql(
            "INSERT OR IGNORE INTO saved_matches(match_id,saved_at) VALUES(?,?)"
        )
        self.assertIn("INSERT INTO saved_matches", sql)
        self.assertTrue(sql.endswith("ON CONFLICT DO NOTHING"))

    def test_sqlite_datetime_now_comparison_becomes_postgres_timestamp_query(self):
        sql = application.translate_postgres_sql(
            "SELECT * FROM matches WHERE datetime(kickoff)>=datetime('now')"
        )
        self.assertIn("CAST(kickoff AS timestamptz) >= NOW()", sql)

    def test_hybrid_row_supports_named_and_positional_access(self):
        row = application.HybridRow(["id", "status"], [17, "saved"])
        self.assertEqual(row["id"], 17)
        self.assertEqual(row[0], 17)
        self.assertEqual(row[1], "saved")
        self.assertEqual(dict(row), {"id": 17, "status": "saved"})


if __name__ == "__main__":
    unittest.main()
