"""Transparent football prediction model based on imported finished match results.

No odds or injury assumptions are invented. Markets requiring unavailable data
(corners) are deliberately omitted.
"""
import math
import re
import unicodedata
from collections import defaultdict

RECENT_LIMIT = 10
MIN_TEAM_MATCHES = 4
MIN_HALF_MATCHES = 4
MAX_GOALS = 10

def _mean(values, fallback):
    return sum(values) / len(values) if values else fallback

def _poisson(lam, goals):
    return math.exp(-lam) * (lam ** goals) / math.factorial(goals)

def _matrix(home_xg, away_xg):
    hp = [_poisson(home_xg, i) for i in range(MAX_GOALS + 1)]
    ap = [_poisson(away_xg, i) for i in range(MAX_GOALS + 1)]
    return [[hp[h] * ap[a] for a in range(MAX_GOALS + 1)] for h in range(MAX_GOALS + 1)]

def _normalize_team(value):
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    removable = {"fc", "afc", "cf", "sc", "fk", "sk", "ks", "club", "football", "soccer", "kulubu", "spor"}
    words = [word for word in text.split() if word not in removable]
    return " ".join(words)


class PreparedHistory:
    """Pre-index completed matches once so each fixture prediction is fast."""
    def __init__(self, rows):
        self.rows = [
            r for r in rows
            if r.get("status") == "finished"
            and r.get("home_goals") is not None and r.get("away_goals") is not None
        ]
        self.by_league = defaultdict(list)
        self.by_team = defaultdict(list)
        self.by_league_team = defaultdict(list)
        for row in self.rows:
            league = (row.get("league") or "").casefold()
            home = _normalize_team(row.get("home"))
            away = _normalize_team(row.get("away"))
            self.by_league[league].append(row)
            if home:
                self.by_team[home].append(row)
                self.by_league_team[(league, home)].append(row)
            if away and away != home:
                self.by_team[away].append(row)
                self.by_league_team[(league, away)].append(row)

    def team_rows(self, team, league_rows, match_id=None):
        target = _normalize_team(team)
        # The global index avoids normalizing every historical team name for
        # every upcoming fixture. League rows are filtered by membership.
        candidates = self.by_team.get(target, ())
        allowed_ids = {id(row) for row in league_rows}
        return [r for r in candidates if id(r) in allowed_ids and str(r.get("id")) != str(match_id)]


def prepare_history(rows):
    return rows if isinstance(rows, PreparedHistory) else PreparedHistory(rows)


def _rates(selected, team, venue=None):
    target = _normalize_team(team)
    if venue:
        at_venue = [
            r for r in selected
            if (_normalize_team(r.get("home")) == target) == (venue == "home")
        ]
        # Use a venue split only when it has enough samples.
        if len(at_venue) >= 3:
            selected = at_venue
    selected = selected[-RECENT_LIMIT:]
    gf, ga, ht_gf, ht_ga, sh_gf, sh_ga = [], [], [], [], [], []
    for r in selected:
        is_home = _normalize_team(r.get("home")) == target
        own = r.get("home_goals") if is_home else r.get("away_goals")
        opp = r.get("away_goals") if is_home else r.get("home_goals")
        own_ht = r.get("ht_home") if is_home else r.get("ht_away")
        opp_ht = r.get("ht_away") if is_home else r.get("ht_home")
        if own is None or opp is None:
            continue
        gf.append(float(own)); ga.append(float(opp))
        if own_ht is not None and opp_ht is not None:
            own_ht, opp_ht = float(own_ht), float(opp_ht)
            if own_ht <= float(own) and opp_ht <= float(opp):
                ht_gf.append(own_ht); ht_ga.append(opp_ht)
                sh_gf.append(float(own) - own_ht); sh_ga.append(float(opp) - opp_ht)
    return {
        "n": len(gf), "gf": _mean(gf, 1.15), "ga": _mean(ga, 1.15),
        "ht_n": len(ht_gf), "ht_gf": _mean(ht_gf, .5), "ht_ga": _mean(ht_ga, .5),
        "sh_gf": _mean(sh_gf, .65), "sh_ga": _mean(sh_ga, .65),
    }

def _outcome(matrix):
    home = draw = away = over = btts = 0.0
    for h in range(MAX_GOALS + 1):
        for a in range(MAX_GOALS + 1):
            p = matrix[h][a]
            if h > a: home += p
            elif h == a: draw += p
            else: away += p
            if h + a >= 3: over += p
            if h > 0 and a > 0: btts += p
    return {"1": home, "X": draw, "2": away}, over, btts

def _both_score_prob(x, y):
    return (1 - math.exp(-x)) * (1 - math.exp(-y))

def predict(match, history):
    indexed = prepare_history(history)
    league = (match.get("league") or "").casefold()
    same_league = indexed.by_league.get(league, ())
    # Prefer same-competition history when at least 12 finished matches exist.
    if len(same_league) >= 12:
        league_rows = same_league
        home_rows = indexed.by_league_team.get((league, _normalize_team(match["home"])), ())
        away_rows = indexed.by_league_team.get((league, _normalize_team(match["away"])), ())
    else:
        league_rows = indexed.rows
        home_rows = indexed.by_team.get(_normalize_team(match["home"]), ())
        away_rows = indexed.by_team.get(_normalize_team(match["away"]), ())
    match_id = match.get("id")
    home_rows = [r for r in home_rows if str(r.get("id")) != str(match_id)]
    away_rows = [r for r in away_rows if str(r.get("id")) != str(match_id)]
    home_all = _rates(home_rows, match["home"])
    away_all = _rates(away_rows, match["away"])
    if home_all["n"] < MIN_TEAM_MATCHES or away_all["n"] < MIN_TEAM_MATCHES:
        return None, (
            f"Tahmin için geçmiş veri yetersiz. Son {RECENT_LIMIT} maç içinde en az "
            f"{MIN_TEAM_MATCHES} tamamlanmış maç gerekli. "
            f"{match['home']}: {home_all['n']}, {match['away']}: {away_all['n']} maç."
        )

    home_home = _rates(home_rows, match["home"], "home")
    away_away = _rates(away_rows, match["away"], "away")
    avg_home = _mean([float(r["home_goals"]) for r in league_rows if r.get("home_goals") is not None], 1.4)
    avg_away = _mean([float(r["away_goals"]) for r in league_rows if r.get("away_goals") is not None], 1.1)
    # Blend venue-specific and overall recent form. Clamp extreme samples.
    home_attack = .65 * home_home["gf"] + .35 * home_all["gf"]
    away_defence = .65 * away_away["ga"] + .35 * away_all["ga"]
    away_attack = .65 * away_away["gf"] + .35 * away_all["gf"]
    home_defence = .65 * home_home["ga"] + .35 * home_all["ga"]
    home_xg = max(.15, min(4.0, .52 * home_attack + .28 * away_defence + .20 * avg_home))
    away_xg = max(.15, min(4.0, .52 * away_attack + .28 * home_defence + .20 * avg_away))
    matrix = _matrix(home_xg, away_xg)
    result_probs, over_prob, btts_prob = _outcome(matrix)

    best_result = max(result_probs, key=result_probs.get)
    predictions = {
        "match_result": {"1": "MS 1", "X": "MS X", "2": "MS 2"}[best_result],
        "over25": "Üst 2,5" if over_prob >= .5 else "Alt 2,5",
        "btts": "Var" if btts_prob >= .5 else "Yok",
    }
    confidence = {
        "match_result": round(result_probs[best_result] * 100),
        "over25": round((over_prob if over_prob >= .5 else 1 - over_prob) * 100),
        "btts": round((btts_prob if btts_prob >= .5 else 1 - btts_prob) * 100),
    }

    # Half-time and second-half markets are calculated only when both teams have
    # enough actual half-time scores in the imported history.
    home_half = home_home
    away_half = away_away
    if home_half["ht_n"] >= MIN_HALF_MATCHES and away_half["ht_n"] >= MIN_HALF_MATCHES:
        h1 = max(.08, .55 * home_half["ht_gf"] + .45 * away_half["ht_ga"])
        a1 = max(.08, .55 * away_half["ht_gf"] + .45 * home_half["ht_ga"])
        p1 = _both_score_prob(h1, a1)
        h2 = max(.08, .55 * home_half["sh_gf"] + .45 * away_half["sh_ga"])
        a2 = max(.08, .55 * away_half["sh_gf"] + .45 * home_half["sh_ga"])
        p2 = _both_score_prob(h2, a2)
        predictions["first_half_btts"] = "Var" if p1 >= .5 else "Yok"
        predictions["second_half_btts"] = "Var" if p2 >= .5 else "Yok"
        confidence["first_half_btts"] = round((p1 if p1 >= .5 else 1-p1) * 100)
        confidence["second_half_btts"] = round((p2 if p2 >= .5 else 1-p2) * 100)

        # Approximate HT/FT by independent first- and second-half score distributions.
        first = _matrix(h1, a1)
        second = _matrix(h2, a2)
        combos = defaultdict(float)
        for h in range(MAX_GOALS + 1):
            for a in range(MAX_GOALS + 1):
                half = "1" if h > a else "X" if h == a else "2"
                for x in range(MAX_GOALS + 1):
                    for y in range(MAX_GOALS + 1):
                        full = "1" if h+x > a+y else "X" if h+x == a+y else "2"
                        combos[half + "/" + full] += first[h][a] * second[x][y]
        best_htft = max(combos, key=combos.get)
        predictions["htft"] = best_htft
        confidence["htft"] = round(combos[best_htft] * 100)

    notes = (
        f"İstatistik modeli · Son {RECENT_LIMIT} maçlık form (mevcut veri kadar). "
        f"Örneklem: {match['home']} {home_all['n']} maç, {match['away']} {away_all['n']} maç. "
        f"Modelin beklenen gol tahmini: {home_xg:.2f} – {away_xg:.2f}. "
        "MS, 2,5 gol ve KG olasılıkları Poisson gol dağılımıyla hesaplandı. "
        "İY/2Y pazarları yalnızca yeterli devre skoru bulunan geçmiş maçlarla hesaplanır. "
        "Sakatlık, ilk 11, motivasyon ve bahis oranları henüz modele dahil değildir. "
        "Korner verisi doğrulanmadığı için korner tahmini üretilmez."
    )
    return {"predictions": predictions, "confidence": confidence, "notes": notes}, None
