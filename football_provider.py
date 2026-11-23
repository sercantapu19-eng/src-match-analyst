import requests
BASE_URL="https://v3.football.api-sports.io"
class ProviderError(Exception): pass
class FootballProvider:
    def __init__(self,api_key):
        if not api_key: raise ProviderError("API_FOOTBALL_KEY eksik.")
        self.session=requests.Session()
        self.session.headers.update({"x-apisports-key":api_key})
    def fixtures(self,day):
        try:
            r=self.session.get(BASE_URL+"/fixtures",params={"date":day,"timezone":"Europe/Istanbul"},timeout=25)
        except requests.RequestException as e: raise ProviderError(f"API bağlantısı kurulamadı: {e}") from e
        if r.status_code in (401,403): raise ProviderError("API anahtarı geçersiz veya erişim yetkisi yok.")
        if r.status_code==429: raise ProviderError("API istek kotası doldu.")
        if not r.ok: raise ProviderError(f"API HTTP {r.status_code}: {r.text[:250]}")
        try: data=r.json()
        except ValueError as e: raise ProviderError("Sağlayıcı geçerli JSON döndürmedi.") from e
        if data.get("errors"): raise ProviderError(str(data["errors"])[:400])
        out=[]
        for x in data.get("response",[]):
            f=x.get("fixture",{}); t=x.get("teams",{}); g=x.get("goals",{}); h=x.get("score",{}).get("halftime",{})
            short=(f.get("status") or {}).get("short","NS")
            state="finished" if short in ("FT","AET","PEN") else "live" if short in ("1H","HT","2H","ET","P","LIVE","INT") else "scheduled"
            out.append({"id":f.get("id"),"home":(t.get("home") or {}).get("name") or "Ev sahibi","away":(t.get("away") or {}).get("name") or "Deplasman","league":(x.get("league") or {}).get("name",""),"kickoff":f.get("date",""),"status":state,"status_short":short,"home_goals":g.get("home"),"away_goals":g.get("away"),"ht_home":h.get("home"),"ht_away":h.get("away")})
        return out
