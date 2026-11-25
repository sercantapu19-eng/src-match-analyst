import os
import tempfile
import unittest
from datetime import date
import app as application
import auth

class SavedCouponTests(unittest.TestCase):
    def setUp(self):
        self.tempdir=tempfile.TemporaryDirectory()
        self.old_db_path=application.DB_PATH
        self.old_env_db_path=os.environ.get("DATABASE_PATH")
        path=os.path.join(self.tempdir.name,"coupon-test.db")
        os.environ["DATABASE_PATH"]=path
        application.DB_PATH=path
        application.init_db()
        auth.init_auth_db()
        with auth._db() as conn:
            self.user_one=auth._execute(conn,"INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?) RETURNING id",("coupon-one@example.com","hash","test")).fetchone()[0]
            self.user_two=auth._execute(conn,"INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?) RETURNING id",("coupon-two@example.com","hash","test")).fetchone()[0]
        with application.db() as conn:
            self.match_one=conn.execute("INSERT INTO matches(home,away,league,kickoff,status,created_at,market_probabilities) VALUES(?,?,?,?,?,?,?) RETURNING id",("Alpha FC","Beta FC","Test League",date.today().isoformat()+"T18:00:00+03:00","scheduled","test",'{"match_result":{"1":0.8,"X":0.1,"2":0.1}}')).fetchone()["id"]
            self.match_two=conn.execute("INSERT INTO matches(home,away,league,kickoff,status,created_at,market_probabilities) VALUES(?,?,?,?,?,?,?) RETURNING id",("Gamma FC","Delta FC","Test League",date.today().isoformat()+"T20:00:00+03:00","scheduled","test",'{"btts":{"Var":0.7,"Yok":0.3}}')).fetchone()["id"]
        self.client=application.app.test_client()
    def tearDown(self):
        application.DB_PATH=self.old_db_path
        if self.old_env_db_path is None: os.environ.pop("DATABASE_PATH",None)
        else: os.environ["DATABASE_PATH"]=self.old_env_db_path
        self.tempdir.cleanup()
    def login(self,user_id):
        with self.client.session_transaction() as session:
            session.clear();session["user_id"]=user_id;session["_csrf_token"]="coupon-test-token"
    def add(self,match_id,market,selection):
        return self.client.post("/coupon/draft/add",data={"csrf_token":"coupon-test-token","match_id":match_id,"market_key":market,"selection_key":selection},headers={"X-CSRF-Token":"coupon-test-token"})
    def test_multiple_matches_can_be_saved_under_one_named_coupon(self):
        self.login(self.user_one)
        self.assertEqual(self.add(self.match_one,"match_result","1").status_code,200)
        self.assertEqual(self.add(self.match_two,"btts","Var").status_code,200)
        draft=self.client.get("/coupon/draft").get_data(as_text=True)
        self.assertIn("Alpha FC",draft);self.assertIn("Gamma FC",draft)
        response=self.client.post("/coupon/save",data={"csrf_token":"coupon-test-token","name":"Akşam Kuponu"},headers={"X-CSRF-Token":"coupon-test-token"})
        self.assertEqual(response.status_code,302)
        page=self.client.get("/saved").get_data(as_text=True)
        self.assertIn("Akşam Kuponu",page);self.assertIn("MS 1",page);self.assertIn("Maç Sonucu",page)
    def test_coupon_draft_is_account_scoped(self):
        self.login(self.user_one);self.add(self.match_one,"match_result","1")
        self.login(self.user_two)
        page=self.client.get("/coupon/draft").get_data(as_text=True)
        self.assertIn("Kuponunda henüz seçim yok",page);self.assertNotIn("Alpha FC",page)
    def test_duplicate_selection_is_idempotent(self):
        self.login(self.user_one);self.add(self.match_one,"match_result","1");self.add(self.match_one,"match_result","1")
        page=self.client.get("/coupon/draft").get_data(as_text=True)
        self.assertEqual(page.count("Alpha FC"),1)


    def test_saved_list_shows_only_clickable_coupons_and_detail_route(self):
        self.login(self.user_one)
        self.add(self.match_one,"match_result","1")
        self.client.post("/coupon/save",data={"csrf_token":"coupon-test-token","name":"Detay Testi"},
            headers={"X-CSRF-Token":"coupon-test-token"})
        listing=self.client.get("/saved")
        self.assertEqual(listing.status_code,200)
        html=listing.get_data(as_text=True)
        self.assertIn('href="/coupon/',html)
        self.assertIn("Detay Testi",html)
        self.assertNotIn("Eski kayıtların",html)
        detail=self.client.get("/coupon/1")
        self.assertEqual(detail.status_code,200)
        detail_html=detail.get_data(as_text=True)
        self.assertIn("Alpha FC",detail_html)
        self.assertIn("MS 1",detail_html)

    def test_user_cannot_open_another_users_coupon(self):
        self.login(self.user_one)
        self.add(self.match_one,"match_result","1")
        self.client.post("/coupon/save",data={"csrf_token":"coupon-test-token","name":"Private"},
            headers={"X-CSRF-Token":"coupon-test-token"})
        self.login(self.user_two)
        self.assertEqual(self.client.get("/coupon/1").status_code,404)

if __name__=="__main__": unittest.main()
