import os, sqlite3, json, time
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash
from football_provider import FootballProvider, ProviderError
from prediction_engine import predict

app=Flask(__name__); app.secret_key=os.environ.get("SECRET_KEY","change-this-before-deploy")
DB_PATH=os.environ.get("DATABASE_PATH",os.path.join(os.path.dirname(__file__),"src_match_analyst.db"))
MARKETS=[("htft","İY/MS"),("over25","2,5 Gol"),("btts","KG"),("first_half_btts","1. Yarı KG"),("second_half_btts","2. Yarı KG"),("corners","Korner Alt/Üst")]

def db():
 c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); return c

def init_db():
 with db() as c:
  c.execute("""CREATE TABLE IF NOT EXISTS matches(
   id INTEGER PRIMARY KEY AUTOINCREMENT,home TEXT NOT NULL,away TEXT NOT NULL,league TEXT DEFAULT '',
   kickoff TEXT DEFAULT '',status TEXT DEFAULT 'scheduled',odds_home TEXT DEFAULT '',odds_draw TEXT DEFAULT '',
   odds_away TEXT DEFAULT '',htft TEXT DEFAULT '',over25 TEXT DEFAULT '',btts TEXT DEFAULT '',
   first_half_btts TEXT DEFAULT '',second_half_btts TEXT DEFAULT '',corners TEXT DEFAULT '',
   notes TEXT DEFAULT '',created_at TEXT NOT NULL,provider TEXT DEFAULT '',external_id TEXT DEFAULT '',
   home_goals INTEGER,away_goals INTEGER,ht_home INTEGER,ht_away INTEGER,status_short TEXT DEFAULT '',elapsed_minute INTEGER,prediction_confidence TEXT DEFAULT '{}',prediction_message TEXT DEFAULT '',analysis_notes TEXT DEFAULT '')""")
  cols={r[1] for r in c.execute("PRAGMA table_info(matches)").fetchall()}
  for name,typ in [("provider","TEXT DEFAULT ''"),("external_id","TEXT DEFAULT ''"),("home_goals","INTEGER"),("away_goals","INTEGER"),("ht_home","INTEGER"),("ht_away","INTEGER"),("status_short","TEXT DEFAULT ''"),("elapsed_minute","INTEGER"),("prediction_confidence","TEXT DEFAULT '{}'"),("prediction_message","TEXT DEFAULT ''"),("analysis_notes","TEXT DEFAULT ''")]:
   if name not in cols: c.execute(f"ALTER TABLE matches ADD COLUMN {name} {typ}")
  c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_fixture ON matches(provider,external_id) WHERE external_id <> ''")
  c.execute("""CREATE TABLE IF NOT EXISTS saved_matches(id INTEGER PRIMARY KEY AUTOINCREMENT,match_id INTEGER NOT NULL UNIQUE,saved_at TEXT NOT NULL,FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE)""")
  c.execute("""CREATE TABLE IF NOT EXISTS results(id INTEGER PRIMARY KEY AUTOINCREMENT,match_id INTEGER NOT NULL,market TEXT NOT NULL,prediction TEXT NOT NULL,actual TEXT DEFAULT '',outcome TEXT DEFAULT 'pending',FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE,UNIQUE(match_id,market))""")
  c.execute("""CREATE TABLE IF NOT EXISTS sync_log(id INTEGER PRIMARY KEY AUTOINCREMENT,provider TEXT,day TEXT,ran_at TEXT,fixtures_count INTEGER DEFAULT 0,status TEXT,message TEXT)""")

def sync_prediction_rows(c,mid):
 m=c.execute("SELECT * FROM matches WHERE id=?",(mid,)).fetchone()
 for k,label in MARKETS:
  if m[k]:
   c.execute("INSERT INTO results(match_id,market,prediction) VALUES(?,?,?) ON CONFLICT(match_id,market) DO UPDATE SET prediction=excluded.prediction",(mid,label,m[k]))


def generate_prediction(c, mid):
 m=c.execute("SELECT * FROM matches WHERE id=?",(mid,)).fetchone()
 if not m or m["status"] != "scheduled": return
 history=c.execute("SELECT id,home,away,status,home_goals,away_goals,ht_home,ht_away FROM matches WHERE status='finished' AND home_goals IS NOT NULL AND away_goals IS NOT NULL ORDER BY kickoff").fetchall()
 data,message=predict(dict(m),[dict(x) for x in history])
 if not data:
  c.execute("UPDATE matches SET prediction_message=?,prediction_confidence='{}',analysis_notes='' WHERE id=?",(message,mid))
  return
 for key,value in data["predictions"].items():
  c.execute("UPDATE matches SET "+key+"=? WHERE id=?",(value,mid))
  label=dict(MARKETS).get(key)
  if label:
   c.execute("INSERT INTO results(match_id,market,prediction) VALUES(?,?,?) ON CONFLICT(match_id,market) DO UPDATE SET prediction=excluded.prediction",(mid,label,value))
 c.execute("UPDATE matches SET prediction_confidence=?,prediction_message='',analysis_notes=? WHERE id=?",(json.dumps(data["confidence"],ensure_ascii=False),data["notes"],mid))

def provider_ready():
 return True  # SportScore supports public access; API key is optional

init_db()

@app.route("/")
def index():
 day=request.args.get("date") or date.today().isoformat()
 view=request.args.get("view","upcoming")
 if view not in ("upcoming","live","completed"): view="upcoming"
 status_filter={"completed":"finished","live":"live","upcoming":"scheduled"}[view]
 with db() as c:
  ms=c.execute("""SELECT m.*,s.id saved FROM matches m LEFT JOIN saved_matches s ON s.match_id=m.id
   WHERE substr(m.kickoff,1,10)=? AND m.status=? ORDER BY m.kickoff""",(day,status_filter)).fetchall()
  saved=c.execute("SELECT COUNT(*) FROM saved_matches").fetchone()[0]
  done=c.execute("SELECT COUNT(*) FROM matches WHERE status='finished'").fetchone()[0]
  upcoming=c.execute("SELECT COUNT(*) FROM matches WHERE substr(kickoff,1,10)=? AND status='scheduled'",(day,)).fetchone()[0]
  live_count=c.execute("SELECT COUNT(*) FROM matches WHERE substr(kickoff,1,10)=? AND status='live'",(day,)).fetchone()[0]
  last=c.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
 return render_template("index.html",matches=ms,saved_count=saved,done_count=done,day=day,
  provider_ready=provider_ready(),last_sync=last,view=view,upcoming_count=upcoming,live_count=live_count)

@app.post("/sync/fixtures")
def sync_fixtures():
 day=request.form.get("date") or date.today().isoformat()
 try:
  provider=FootballProvider(os.environ.get("SPORTSCORE_API_KEY"))
  fixtures=[]
  for offset in range(30,-1,-1):
   target=(date.fromisoformat(day)-timedelta(days=offset)).isoformat()
   fixtures.extend(provider.fixtures(target))
   if offset: time.sleep(0.12)
  with db() as c:
   for f in fixtures:
    c.execute("""INSERT INTO matches(home,away,league,kickoff,status,created_at,provider,external_id,
      home_goals,away_goals,ht_home,ht_away,status_short,elapsed_minute)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(provider,external_id) WHERE external_id <> '' DO UPDATE SET
      home=excluded.home,away=excluded.away,league=excluded.league,kickoff=excluded.kickoff,
      status=excluded.status,home_goals=excluded.home_goals,away_goals=excluded.away_goals,
      ht_home=excluded.ht_home,ht_away=excluded.ht_away,status_short=excluded.status_short,elapsed_minute=excluded.elapsed_minute""",
      (f["home"],f["away"],f["league"],f["kickoff"],f["status"],datetime.utcnow().isoformat(),
       "sportscore",str(f["id"]),f["home_goals"],f["away_goals"],f["ht_home"],f["ht_away"],f["status_short"],f.get("elapsed_minute")))
   c.execute("INSERT INTO sync_log(provider,day,ran_at,fixtures_count,status,message) VALUES(?,?,?,?,?,?)",
    ("sportscore",day,datetime.utcnow().isoformat(),len(fixtures),"success",f"SportScore: {len(fixtures)} maç kaydı (31 günlük geçmiş dahil)"))
   targets=c.execute("SELECT id FROM matches WHERE substr(kickoff,1,10)=? AND status='scheduled'",(day,)).fetchall()
   for target in targets: generate_prediction(c,target["id"])
  flash(f"{len(fixtures)} maç otomatik olarak içe aktarıldı/güncellendi.","success")
 except ProviderError as e:
  with db() as c:c.execute("INSERT INTO sync_log(provider,day,ran_at,status,message) VALUES(?,?,?,?,?)",("sportscore",day,datetime.utcnow().isoformat(),"error",str(e)))
  flash(f"Veri sağlayıcı hatası: {e}","error")
 except Exception as e:
  flash(f"Beklenmeyen senkronizasyon hatası: {e}","error")
 return redirect(url_for("index",date=day))

@app.route("/match/new",methods=["GET","POST"])
def new_match():
 if request.method=="POST":
  keys=["home","away","league","kickoff","odds_home","odds_draw","odds_away","htft","over25","btts","first_half_btts","second_half_btts","corners","notes"]
  d={k:request.form.get(k,"").strip() for k in keys}
  if not d["home"] or not d["away"]: flash("İki takım adı zorunludur.","error"); return render_template("form.html",data=d)
  with db() as c:
   cur=c.execute("INSERT INTO matches(home,away,league,kickoff,status,odds_home,odds_draw,odds_away,htft,over25,btts,first_half_btts,second_half_btts,corners,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(d["home"],d["away"],d["league"],d["kickoff"],"scheduled",d["odds_home"],d["odds_draw"],d["odds_away"],d["htft"],d["over25"],d["btts"],d["first_half_btts"],d["second_half_btts"],d["corners"],d["notes"],datetime.utcnow().isoformat()))
   sync_prediction_rows(c,cur.lastrowid)
  flash("Maç kaydedildi.","success"); return redirect("/")
 return render_template("form.html",data={})

@app.post("/match/<int:mid>/save")
def save(mid):
 with db() as c:
  if c.execute("SELECT 1 FROM matches WHERE id=?",(mid,)).fetchone():c.execute("INSERT OR IGNORE INTO saved_matches(match_id,saved_at) VALUES(?,?)",(mid,datetime.utcnow().isoformat()))
 return redirect(request.referrer or "/")
@app.post("/match/<int:mid>/unsave")
def unsave(mid):
 with db() as c:c.execute("DELETE FROM saved_matches WHERE match_id=?",(mid,))
 return redirect(request.referrer or "/")
@app.route("/match/<int:mid>")
def detail(mid):
 with db() as c:
  m=c.execute("SELECT m.*,s.id saved FROM matches m LEFT JOIN saved_matches s ON s.match_id=m.id WHERE m.id=?",(mid,)).fetchone()
  rs=c.execute("SELECT * FROM results WHERE match_id=? ORDER BY id",(mid,)).fetchall()
  confidence=json.loads(m["prediction_confidence"] or "{}")
 if not m:return "Maç bulunamadı",404
 return render_template("detail.html",m=m,results=rs,markets=MARKETS,confidence=confidence)
@app.post("/match/<int:mid>/status")
def status(mid):
 st=request.form.get("status","scheduled")
 if st not in ("scheduled","live","finished"):st="scheduled"
 with db() as c:c.execute("UPDATE matches SET status=? WHERE id=?",(st,mid))
 return redirect(url_for("detail",mid=mid))
@app.post("/match/<int:mid>/result")
def result(mid):
 with db() as c:
  for key,label in MARKETS:
   actual=request.form.get("actual_"+key,"").strip(); row=c.execute("SELECT prediction FROM results WHERE match_id=? AND market=?",(mid,label)).fetchone()
   if row and actual:
    outcome="won" if row["prediction"].casefold()==actual.casefold() else "lost"
    c.execute("UPDATE results SET actual=?,outcome=? WHERE match_id=? AND market=?",(actual,outcome,mid,label))
  c.execute("UPDATE matches SET status='finished' WHERE id=?",(mid,))
 flash("Tahminler sonuçlarla karşılaştırıldı.","success"); return redirect(url_for("detail",mid=mid))
@app.route("/saved")
def saved():
 with db() as c:ms=c.execute("SELECT m.* FROM matches m JOIN saved_matches s ON s.match_id=m.id ORDER BY s.saved_at DESC").fetchall()
 return render_template("saved.html",matches=ms)
@app.route("/results")
def results():
 with db() as c:
  rows=c.execute("SELECT m.home,m.away,m.league,m.kickoff,r.market,r.prediction,r.actual,r.outcome FROM results r JOIN matches m ON m.id=r.match_id WHERE m.status='finished' ORDER BY m.kickoff DESC").fetchall()
  stats=c.execute("SELECT outcome,COUNT(*) n FROM results WHERE outcome IN ('won','lost') GROUP BY outcome").fetchall()
 counts={r["outcome"]:r["n"] for r in stats}; won=counts.get("won",0); lost=counts.get("lost",0)
 return render_template("results.html",rows=rows,won=won,lost=lost,pct=round(won*100/(won+lost)) if won+lost else 0)
if __name__=="__main__":app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)),debug=True)
