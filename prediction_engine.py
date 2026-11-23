import math
from collections import defaultdict

MIN_TEAM_MATCHES = 4
RECENT_LIMIT = 10
MAX_GOALS = 8

def poisson(lam, k):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)

def score_matrix(home_xg, away_xg):
    hp = [poisson(home_xg, i) for i in range(MAX_GOALS + 1)]
    ap = [poisson(away_xg, i) for i in range(MAX_GOALS + 1)]
    return [[hp[h] * ap[a] for a in range(MAX_GOALS + 1)] for h in range(MAX_GOALS + 1)]

def mean(values, fallback=0.0):
    return sum(values) / len(values) if values else fallback

def team_rates(rows, team):
    selected = [r for r in rows if r["home"] == team or r["away"] == team][-RECENT_LIMIT:]
    gf, ga, hgf, hga, s2gf, s2ga = [], [], [], [], [], []
    for r in selected:
        home = r["home"] == team
        own = r["home_goals"] if home else r["away_goals"]
        opp = r["away_goals"] if home else r["home_goals"]
        own_ht = r["ht_home"] if home else r["ht_away"]
        opp_ht = r["ht_away"] if home else r["ht_home"]
        if own is None or opp is None: continue
        gf.append(float(own)); ga.append(float(opp))
        if own_ht is not None and opp_ht is not None:
            hgf.append(float(own_ht)); hga.append(float(opp_ht))
            s2gf.append(max(0.0,float(own)-float(own_ht)))
            s2ga.append(max(0.0,float(opp)-float(opp_ht)))
    return {"n":len(gf),"gf":mean(gf),"ga":mean(ga),"hgf":mean(hgf),
            "hga":mean(hga),"s2gf":mean(s2gf),"s2ga":mean(s2ga),"half_n":len(hgf)}

def predict(match, history):
    rows=[r for r in history if r["status"]=="finished" and r["home_goals"] is not None and r["away_goals"] is not None and r["id"]!=match["id"]]
    home=team_rates(rows,match["home"]); away=team_rates(rows,match["away"])
    if home["n"]<MIN_TEAM_MATCHES or away["n"]<MIN_TEAM_MATCHES:
        return None, f"Yeterli geçmiş veri yok. Tahmin için her takımın en az {MIN_TEAM_MATCHES} tamamlanmış maçına ihtiyaç var (ev sahibi: {home['n']}, deplasman: {away['n']})."
    avg_home=mean([float(r["home_goals"]) for r in rows if r["home_goals"] is not None],1.35)
    avg_away=mean([float(r["away_goals"]) for r in rows if r["away_goals"] is not None],1.10)
    xh=max(.15,min(3.8,(.65*home["gf"]+.35*avg_home+.65*away["ga"]+.35*avg_home)/2))
    xa=max(.15,min(3.8,(.65*away["gf"]+.35*avg_away+.65*home["ga"]+.35*avg_away)/2))
    matrix=score_matrix(xh,xa)
    over=sum(matrix[h][a] for h in range(9) for a in range(9) if h+a>=3)
    btts=sum(matrix[h][a] for h in range(1,9) for a in range(1,9))
    predictions={"over25":"Üst 2,5" if over>=.5 else "Alt 2,5","btts":"Var" if btts>=.5 else "Yok"}
    confidence={"over25":round(max(over,1-over)*100),"btts":round(max(btts,1-btts)*100)}
    if home["half_n"]>=MIN_TEAM_MATCHES and away["half_n"]>=MIN_TEAM_MATCHES:
        h1=max(.08,(home["hgf"]+away["hga"])/2); a1=max(.08,(away["hgf"]+home["hga"])/2)
        h2=max(.08,(home["s2gf"]+away["s2ga"])/2); a2=max(.08,(away["s2gf"]+home["s2ga"])/2)
        def both(x,y): return (1-math.exp(-x))*(1-math.exp(-y))
        p1,p2=both(h1,a1),both(h2,a2)
        predictions["first_half_btts"]="Var" if p1>=.5 else "Yok"
        predictions["second_half_btts"]="Var" if p2>=.5 else "Yok"
        confidence["first_half_btts"]=round(max(p1,1-p1)*100)
        confidence["second_half_btts"]=round(max(p2,1-p2)*100)
        ht=score_matrix(h1,a1); sh=score_matrix(h2,a2); combos=defaultdict(float)
        for h in range(9):
            for a in range(9):
                hr="1" if h>a else "X" if h==a else "2"
                for x in range(9):
                    for y in range(9):
                        fr="1" if h+x>a+y else "X" if h+x==a+y else "2"
                        combos[hr+"/"+fr]+=ht[h][a]*sh[x][y]
        best=max(combos,key=combos.get)
        predictions["htft"]=best; confidence["htft"]=round(combos[best]*100)
    notes=(f"Geçmiş maç istatistik modeli · Son {RECENT_LIMIT} maça kadar; örneklem ev sahibi {home['n']}, "
           f"deplasman {away['n']}. Beklenen gol: {xh:.2f} – {xa:.2f}. "
           "Olasılıklar garanti değildir; sakatlık, kadro ve bahis oranları modele dahil değildir. "
           "Doğrulanmış korner geçmişi bulunmadığından korner tahmini üretilmedi.")
    return {"predictions":predictions,"confidence":confidence,"notes":notes},None
