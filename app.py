import os, sqlite3, json, time
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from football_provider import FootballProvider, ProviderError
from prediction_engine import predict, prepare_history

app=Flask(__name__); app.secret_key=os.environ.get("SECRET_KEY","change-this-before-deploy")
DB_PATH=os.environ.get("DATABASE_PATH",os.path.join(os.path.dirname(__file__),"src_match_analyst.db"))
MARKETS=[("match_result","Maç Sonucu"),("htft","İY/MS"),("over25","2,5 Gol"),("btts","KG"),("first_half_btts","1. Yarı KG"),("second_half_btts","2. Yarı KG"),("corners","Korner Alt/Üst")]

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
   home_goals INTEGER,away_goals INTEGER,ht_home INTEGER,ht_away INTEGER,status_short TEXT DEFAULT '',elapsed_minute INTEGER,match_result TEXT DEFAULT '',prediction_confidence TEXT DEFAULT '{}',market_probabilities TEXT DEFAULT '{}',prediction_message TEXT DEFAULT '',analysis_notes TEXT DEFAULT '')""")
  cols={r[1] for r in c.execute("PRAGMA table_info(matches)").fetchall()}
  for name,typ in [("provider","TEXT DEFAULT ''"),("external_id","TEXT DEFAULT ''"),("home_goals","INTEGER"),("away_goals","INTEGER"),("ht_home","INTEGER"),("ht_away","INTEGER"),("status_short","TEXT DEFAULT ''"),("elapsed_minute","INTEGER"),("match_result","TEXT DEFAULT ''"),("prediction_confidence","TEXT DEFAULT '{}'"),("market_probabilities","TEXT DEFAULT '{}'"),("prediction_message","TEXT DEFAULT ''"),("analysis_notes","TEXT DEFAULT ''")]:
   if name not in cols: c.execute(f"ALTER TABLE matches ADD COLUMN {name} {typ}")
  c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_fixture ON matches(provider,external_id) WHERE external_id <> ''")
  c.execute("""CREATE TABLE IF NOT EXISTS saved_matches(id INTEGER PRIMARY KEY AUTOINCREMENT,match_id INTEGER NOT NULL UNIQUE,saved_at TEXT NOT NULL,FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE)""")
  c.execute("""CREATE TABLE IF NOT EXISTS results(id INTEGER PRIMARY KEY AUTOINCREMENT,match_id INTEGER NOT NULL,market TEXT NOT NULL,prediction TEXT NOT NULL,actual TEXT DEFAULT '',outcome TEXT DEFAULT 'pending',FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE,UNIQUE(match_id,market))""")
  c.execute("""CREATE TABLE IF NOT EXISTS sync_log(id INTEGER PRIMARY KEY AUTOINCREMENT,provider TEXT,day TEXT,ran_at TEXT,fixtures_count INTEGER DEFAULT 0,status TEXT,message TEXT)""")
  c.execute("""CREATE TABLE IF NOT EXISTS sync_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,phase TEXT NOT NULL,status TEXT NOT NULL,start_date TEXT NOT NULL,end_date TEXT NOT NULL,cursor_date TEXT NOT NULL,total_days INTEGER NOT NULL,processed_days INTEGER DEFAULT 0,fixtures_count INTEGER DEFAULT 0,predictions_total INTEGER DEFAULT 0,predictions_processed INTEGER DEFAULT 0,fetched_count INTEGER DEFAULT 0,skipped_count INTEGER DEFAULT 0,attempts INTEGER DEFAULT 0,last_error TEXT DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
  job_cols={r[1] for r in c.execute("PRAGMA table_info(sync_jobs)").fetchall()}
  for name,typ in [("fetched_count","INTEGER DEFAULT 0"),("skipped_count","INTEGER DEFAULT 0"),("team_names","TEXT DEFAULT '[]'"),("team_index","INTEGER DEFAULT 0")]:
   if name not in job_cols: c.execute(f"ALTER TABLE sync_jobs ADD COLUMN {name} {typ}")

def sync_prediction_rows(c,mid):
 m=c.execute("SELECT * FROM matches WHERE id=?",(mid,)).fetchone()
 for k,label in MARKETS:
  if m[k]:
   c.execute("INSERT INTO results(match_id,market,prediction) VALUES(?,?,?) ON CONFLICT(match_id,market) DO UPDATE SET prediction=excluded.prediction",(mid,label,m[k]))


def generate_prediction(c, mid):
 m=c.execute("SELECT * FROM matches WHERE id=?",(mid,)).fetchone()
 if not m or m["status"] != "scheduled": return
 history=c.execute("SELECT id,home,away,league,status,home_goals,away_goals,ht_home,ht_away FROM matches WHERE status='finished' AND home_goals IS NOT NULL AND away_goals IS NOT NULL ORDER BY kickoff").fetchall()
 data,message=predict(dict(m),[dict(x) for x in history])
 if not data:
  c.execute("UPDATE matches SET prediction_message=?,prediction_confidence='{}',market_probabilities='{}',analysis_notes='' WHERE id=?",(message,mid))
  return
 for key,value in data["predictions"].items():
  c.execute("UPDATE matches SET "+key+"=? WHERE id=?",(value,mid))
  label=dict(MARKETS).get(key)
  if label:
   c.execute("INSERT INTO results(match_id,market,prediction) VALUES(?,?,?) ON CONFLICT(match_id,market) DO UPDATE SET prediction=excluded.prediction",(mid,label,value))
 c.execute("UPDATE matches SET prediction_confidence=?,market_probabilities=?,prediction_message='',analysis_notes=? WHERE id=?",(json.dumps(data["confidence"],ensure_ascii=False),json.dumps(data.get("market_probabilities",{}),ensure_ascii=False),data["notes"],mid))

def provider_ready():
 return True  # SportScore supports public access; API key is optional

def store_fixtures(c, fixtures):
 stored=0
 for f in fixtures:
  # Historical sync must only persist completed matches with a real full-time score.
  if f["status"] != "finished" or f["home_goals"] is None or f["away_goals"] is None:
   continue
  c.execute("""INSERT INTO matches(home,away,league,kickoff,status,created_at,provider,external_id,
    home_goals,away_goals,ht_home,ht_away,status_short,elapsed_minute)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(provider,external_id) WHERE external_id <> '' DO UPDATE SET
    home=excluded.home,away=excluded.away,league=excluded.league,kickoff=excluded.kickoff,
    status=excluded.status,home_goals=excluded.home_goals,away_goals=excluded.away_goals,
    ht_home=excluded.ht_home,ht_away=excluded.ht_away,status_short=excluded.status_short,elapsed_minute=excluded.elapsed_minute""",
    (f["home"],f["away"],f["league"],f["kickoff"],f["status"],datetime.utcnow().isoformat(),
     "sportscore",str(f["id"]),f["home_goals"],f["away_goals"],f["ht_home"],f["ht_away"],f["status_short"],f.get("elapsed_minute")))
  stored+=1
 return {"fetched":len(fixtures),"stored":stored,"skipped":len(fixtures)-stored}

def rebuild_one_prediction(c, target, history):
 try:
  data,message=predict(dict(target),history)
  if not data:
   c.execute("""UPDATE matches SET match_result='',htft='',over25='',btts='',first_half_btts='',second_half_btts='',
    prediction_message=?,prediction_confidence='{}',market_probabilities='{}',analysis_notes='' WHERE id=?""",(message,target["id"]))
   c.execute("DELETE FROM results WHERE match_id=? AND outcome='pending'",(target["id"],))
   return
  c.execute("""UPDATE matches SET match_result='',htft='',over25='',btts='',first_half_btts='',second_half_btts='',
    prediction_message='',prediction_confidence=?,market_probabilities=?,analysis_notes=? WHERE id=?""",
    (json.dumps(data["confidence"],ensure_ascii=False),json.dumps(data.get("market_probabilities",{}),ensure_ascii=False),data["notes"],target["id"]))
  c.execute("DELETE FROM results WHERE match_id=? AND outcome='pending'",(target["id"],))
  for key,value in data["predictions"].items():
   if key not in dict(MARKETS): continue
   c.execute("UPDATE matches SET "+key+"=? WHERE id=?",(value,target["id"]))
   c.execute("INSERT INTO results(match_id,market,prediction) VALUES(?,?,?) ON CONFLICT(match_id,market) DO UPDATE SET prediction=excluded.prediction,actual='',outcome='pending'",
    (target["id"],dict(MARKETS)[key],value))
 except Exception as exc:
  c.execute("UPDATE matches SET prediction_message=? WHERE id=?",(f"Tahmin üretilemedi: {exc}",target["id"]))

def history_job_payload(job):
 teams=json.loads(job["team_names"] or "[]") if "team_names" in job.keys() else []
 total=max(1,job["total_days"]+len(teams)+job["predictions_total"])
 done=job["processed_days"]+job["team_index"]+job["predictions_processed"]
 percent=100 if job["status"]=="completed" else min(99,round(done*100/total))
 return {"job_id":job["id"],"status":job["status"],"phase":job["phase"],
  "processed_days":job["processed_days"],"total_days":job["total_days"],
  "team_index":job["team_index"] if "team_index" in job.keys() else 0,"teams_total":len(teams),
  "predictions_processed":job["predictions_processed"],"predictions_total":job["predictions_total"],
  "fixtures_count":job["fixtures_count"],"fetched_count":job["fetched_count"],"skipped_count":job["skipped_count"],"percent":percent,"last_error":job["last_error"]}


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
  # Fetch only the selected day in this request. The previous 61-day loop
  # made sequential external calls (25s timeout each) inside one sync worker.
  fixtures=provider.fixtures(day)
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
   # Read finished-match history once and reuse it for all predictions.
   history=[dict(x) for x in c.execute(
    "SELECT id,home,away,league,status,home_goals,away_goals,ht_home,ht_away FROM matches WHERE status='finished' AND home_goals IS NOT NULL AND away_goals IS NOT NULL ORDER BY kickoff"
   ).fetchall()]
   history_index=prepare_history(history)
   targets=c.execute("SELECT * FROM matches WHERE substr(kickoff,1,10)=? AND status='scheduled'",(day,)).fetchall()
   for target in targets:
    try:
     data,message=predict(dict(target),history_index)
     if not data:
      c.execute("UPDATE matches SET prediction_message=?,prediction_confidence='{}',analysis_notes='' WHERE id=?",(message,target["id"]))
      continue
     for key,value in data["predictions"].items():
      if key not in dict(MARKETS): continue
      c.execute("UPDATE matches SET "+key+"=? WHERE id=?",(value,target["id"]))
      label=dict(MARKETS)[key]
      c.execute("INSERT INTO results(match_id,market,prediction) VALUES(?,?,?) ON CONFLICT(match_id,market) DO UPDATE SET prediction=excluded.prediction",(target["id"],label,value))
     c.execute("UPDATE matches SET prediction_confidence=?,market_probabilities=?,prediction_message='',analysis_notes=? WHERE id=?",(json.dumps(data["confidence"],ensure_ascii=False),json.dumps(data.get("market_probabilities",{}),ensure_ascii=False),data["notes"],target["id"]))
    except Exception as prediction_error:
     # A single bad fixture must not abort the whole daily import.
     c.execute("UPDATE matches SET prediction_message=? WHERE id=?",(f"Tahmin üretilemedi: {prediction_error}",target["id"]))
   c.execute("INSERT INTO sync_log(provider,day,ran_at,fixtures_count,status,message) VALUES(?,?,?,?,?,?)",
    ("sportscore",day,datetime.utcnow().isoformat(),len(fixtures),"success",f"SportScore: {len(fixtures)} maç kaydı"))
  flash(f"{len(fixtures)} maç otomatik olarak içe aktarıldı/güncellendi.", "success")
 except ProviderError as e:
  with db() as c:
   c.execute("INSERT INTO sync_log(provider,day,ran_at,status,message) VALUES(?,?,?,?,?)",
    ("sportscore",day,datetime.utcnow().isoformat(),"error",str(e)))
  flash(f"Veri sağlayıcı hatası: {e}", "error")
 except Exception as e:
  with db() as c:
   c.execute("INSERT INTO sync_log(provider,day,ran_at,status,message) VALUES(?,?,?,?,?)",
    ("sportscore",day,datetime.utcnow().isoformat(),"error",str(e)))
  flash(f"Beklenmeyen senkronizasyon hatası: {e}", "error")
 return redirect(url_for("index",date=day))

@app.post("/sync/history/start")
def start_history_sync():
 today=date.today()
 start=today-timedelta(days=180)
 end=today-timedelta(days=1)
 now=datetime.utcnow().isoformat()
 with db() as c:
  active=c.execute("SELECT * FROM sync_jobs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
  if active:
   return jsonify(history_job_payload(active))
  cur=c.execute("""INSERT INTO sync_jobs(phase,status,start_date,end_date,cursor_date,total_days,team_names,team_index,created_at,updated_at)
   VALUES('history','running',?,?,?,?,?,?,?,?)""",(start.isoformat(),end.isoformat(),start.isoformat(),(end-start).days+1,"[]",0,now,now))
  job=c.execute("SELECT * FROM sync_jobs WHERE id=?",(cur.lastrowid,)).fetchone()
 return jsonify(history_job_payload(job))

@app.get("/sync/history/status")
def history_sync_status():
 job_id=request.args.get("job_id",type=int)
 with db() as c:
  job=c.execute("SELECT * FROM sync_jobs WHERE id=?",(job_id,)).fetchone() if job_id else c.execute("SELECT * FROM sync_jobs ORDER BY id DESC LIMIT 1").fetchone()
 if not job:return jsonify({"status":"idle","percent":0})
 return jsonify(history_job_payload(job))

@app.post("/sync/history/step")
def history_sync_step():
 payload=request.get_json(silent=True) or {}
 job_id=payload.get("job_id")
 with db() as c:
  job=c.execute("SELECT * FROM sync_jobs WHERE id=?",(job_id,)).fetchone()
  if not job:return jsonify({"status":"error","last_error":"Senkronizasyon işi bulunamadı."}),404
  if job["status"]!="running":return jsonify(history_job_payload(job))
  phase=job["phase"]
  if phase=="history":
   day=job["cursor_date"]
   try:
    provider=FootballProvider(os.environ.get("SPORTSCORE_API_KEY"))
    fixtures=provider.fixtures(day,status="finished")
    stats=store_fixtures(c,fixtures)
    next_day=(date.fromisoformat(day)+timedelta(days=1)).isoformat()
    processed=job["processed_days"]+1
    team_names=json.loads(job["team_names"] or "[]")
    if next_day>job["end_date"]:
     rows=c.execute("SELECT home,away FROM matches WHERE status='scheduled' AND datetime(kickoff)>=datetime('now') ORDER BY kickoff").fetchall()
     seen=set()
     for row in rows:
      for name in (row["home"],row["away"]):
       key=" ".join(str(name or "").casefold().split())
       if key and key not in seen:
        seen.add(key); team_names.append(str(name))
     next_phase="team_history" if team_names else "predictions"
     pred_total=0
     final_status="running"
    else:
     pred_total=job["predictions_total"]; next_phase="history"; final_status="running"
    c.execute("""UPDATE sync_jobs SET phase=?,status=?,cursor_date=?,processed_days=?,team_names=?,team_index=0,fixtures_count=fixtures_count+?,
      fetched_count=fetched_count+?,skipped_count=skipped_count+?,predictions_total=?,attempts=0,last_error='',updated_at=? WHERE id=?""",
      (next_phase,final_status,next_day,processed,json.dumps(team_names,ensure_ascii=False),stats["stored"],stats["fetched"],stats["skipped"],pred_total,datetime.utcnow().isoformat(),job_id))
    if final_status=="completed":
     c.execute("INSERT INTO sync_log(provider,day,ran_at,fixtures_count,status,message) VALUES(?,?,?,?,?,?)",
      ("sportscore",date.today().isoformat(),datetime.utcnow().isoformat(),job["fixtures_count"]+len(fixtures),"success","180 günlük geçmiş maç verisi tamamlandı"))
   except Exception as exc:
    attempts=job["attempts"]+1
    state="error" if attempts>=3 else "running"
    c.execute("UPDATE sync_jobs SET attempts=?,last_error=?,status=?,updated_at=? WHERE id=?",
     (attempts,str(exc),state,datetime.utcnow().isoformat(),job_id))
  elif phase=="team_history":
   team_names=json.loads(job["team_names"] or "[]")
   index=job["team_index"]
   if index < len(team_names):
    team_name=team_names[index]
    try:
     provider=FootballProvider(os.environ.get("SPORTSCORE_API_KEY"))
     team_fixtures=provider.team_fixtures_for_team(team_name,limit=30)
     stats=store_fixtures(c,team_fixtures)
     team_skipped=stats["skipped"] + (1 if not team_fixtures else 0)
     error_note=""
    except Exception as exc:
     stats={"fetched":0,"stored":0}
     team_skipped=1
     error_note=f"{team_name}: {exc}"
    next_index=index+1
    is_last=next_index>=len(team_names)
    if is_last:
     pred_total=c.execute("SELECT COUNT(*) FROM matches WHERE status='scheduled' AND datetime(kickoff)>=datetime('now')").fetchone()[0]
     next_phase="predictions" if pred_total else "completed"
    else:
     pred_total=job["predictions_total"]
     next_phase="team_history"
    c.execute("""UPDATE sync_jobs SET phase=?,status=?,team_index=?,fixtures_count=fixtures_count+?,
      fetched_count=fetched_count+?,skipped_count=skipped_count+?,predictions_total=?,
      last_error=?,updated_at=? WHERE id=?""",
      (next_phase,"running" if next_phase!="completed" else "completed",next_index,
       stats["stored"],stats["fetched"],team_skipped,error_note,datetime.utcnow().isoformat(),job_id))
    if next_phase=="completed":
     c.execute("INSERT INTO sync_log(provider,day,ran_at,fixtures_count,status,message) VALUES(?,?,?,?,?,?)",
      ("sportscore",date.today().isoformat(),datetime.utcnow().isoformat(),job["fixtures_count"]+stats["stored"],"success",
       f"Geçmiş veri tamamlandı; {job['fetched_count']+stats['fetched']} kayıt alındı, takım geçmişi dahil {job['fixtures_count']+stats['stored']} tamamlanmış maç kaydedildi."))
  elif phase=="predictions":
   history=[dict(x) for x in c.execute("""SELECT id,home,away,league,status,home_goals,away_goals,ht_home,ht_away
    FROM matches WHERE status='finished' AND home_goals IS NOT NULL AND away_goals IS NOT NULL ORDER BY kickoff""").fetchall()]
   history_index=prepare_history(history)
   batch=c.execute("""SELECT * FROM matches WHERE status='scheduled' AND datetime(kickoff)>=datetime('now')
    ORDER BY kickoff,id LIMIT 8 OFFSET ?""",(job["predictions_processed"],)).fetchall()
   for target in batch:rebuild_one_prediction(c,target,history_index)
   processed=job["predictions_processed"]+len(batch)
   done=processed>=job["predictions_total"] or not batch
   c.execute("UPDATE sync_jobs SET predictions_processed=?,status=?,updated_at=? WHERE id=?",
    (processed,"completed" if done else "running",datetime.utcnow().isoformat(),job_id))
   if done:
    c.execute("INSERT INTO sync_log(provider,day,ran_at,fixtures_count,status,message) VALUES(?,?,?,?,?,?)",
     ("sportscore",date.today().isoformat(),datetime.utcnow().isoformat(),job["fixtures_count"],"success",
      f"Geçmiş veri tamamlandı; {job['fetched_count']} kayıt alındı, {job['fixtures_count']} tamamlanmış maç kaydedildi, {job['skipped_count']} kayıt atlandı; {processed} yaklaşan maç yeniden tahmin edildi."))
  job=c.execute("SELECT * FROM sync_jobs WHERE id=?",(job_id,)).fetchone()
 return jsonify(history_job_payload(job))

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
  # Lazily rebuild probabilities for fixtures imported before the detailed-market field existed.
  if m and m["status"] == "scheduled" and not json.loads(m["market_probabilities"] or "{}"):
   generate_prediction(c, mid)
   m=c.execute("SELECT m.*,s.id saved FROM matches m LEFT JOIN saved_matches s ON s.match_id=m.id WHERE m.id=?",(mid,)).fetchone()
  rs=c.execute("SELECT * FROM results WHERE match_id=? ORDER BY id",(mid,)).fetchall()
  confidence=json.loads(m["prediction_confidence"] or "{}")
  market_probabilities=json.loads(m["market_probabilities"] or "{}")
 if not m:return "Maç bulunamadı",404
 return render_template("detail.html",m=m,results=rs,markets=MARKETS,confidence=confidence,market_probabilities=market_probabilities)
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
