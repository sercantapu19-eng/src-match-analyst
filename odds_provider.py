"""API-Football bookmaker odds adapter.

Requires API_FOOTBALL_KEY in the hosting environment. It never fabricates
prices. The first pre-match snapshot captured by SRC is labelled as first-seen,
not as the bookmaker's official opening price.
"""
import os
import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher

import requests

BASE_URL = "https://v3.football.api-sports.io"


class OddsProviderError(Exception):
    pass


def _name_key(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    tokens = re.findall(r"[a-z0-9]+", text)
    suffixes = {"fc", "cf", "sk", "fk", "sc", "club", "football", "club"}
    while tokens and tokens[-1] in suffixes:
        tokens.pop()
    return " ".join(tokens)


def _team_match(a, b):
    left, right = _name_key(a), _name_key(b)
    if not left or not right:
        return False
    if left == right:
        return True
    return SequenceMatcher(None, left, right).ratio() >= 0.92


class ApiFootballOddsProvider:
    def __init__(self, api_key=None, session=None):
        self.api_key = (api_key or os.environ.get("API_FOOTBALL_KEY", "")).strip()
        self.session = session or requests.Session()
        if self.api_key:
            self.session.headers.update({"x-apisports-key": self.api_key, "Accept": "application/json"})

    @property
    def configured(self):
        return bool(self.api_key)

    def _get(self, path, params=None):
        if not self.configured:
            raise OddsProviderError("API_FOOTBALL_KEY yapılandırılmamış.")
        try:
            response = self.session.get(BASE_URL + path, params=params or {}, timeout=12)
        except requests.RequestException as exc:
            raise OddsProviderError(f"API-Football bağlantı hatası: {exc}") from exc
        if response.status_code == 429:
            raise OddsProviderError("API-Football istek kotası doldu.")
        if response.status_code in (401, 403):
            raise OddsProviderError("API-Football erişimi reddedildi; API anahtarını/planını kontrol edin.")
        if not response.ok:
            raise OddsProviderError(f"API-Football HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise OddsProviderError("API-Football geçersiz JSON döndürdü.") from exc
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            raise OddsProviderError("API-Football: " + str(errors)[:300])
        return payload if isinstance(payload, dict) else {}

    def resolve_fixture(self, match):
        kickoff = str(match.get("kickoff") or "")
        try:
            dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            day = dt.astimezone(timezone.utc).date().isoformat()
        except ValueError:
            raise OddsProviderError("Maç başlangıç tarihi eşleştirme için geçersiz.")
        payload = self._get("/fixtures", {"date": day, "timezone": "UTC"})
        candidates = []
        for row in payload.get("response") or []:
            fixture = row.get("fixture") or {}
            teams = row.get("teams") or {}
            home = (teams.get("home") or {}).get("name", "")
            away = (teams.get("away") or {}).get("name", "")
            if not (_team_match(match.get("home"), home) and _team_match(match.get("away"), away)):
                continue
            date_text = fixture.get("date")
            try:
                api_dt = datetime.fromisoformat(str(date_text).replace("Z", "+00:00"))
                if api_dt.tzinfo is None:
                    api_dt = api_dt.replace(tzinfo=timezone.utc)
                distance = abs((api_dt - dt.astimezone(timezone.utc)).total_seconds())
            except (ValueError, TypeError):
                distance = 999999
            if distance <= 4 * 3600:
                candidates.append((distance, fixture.get("id"), home, away))
        candidates.sort(key=lambda item: item[0])
        if not candidates:
            raise OddsProviderError("API-Football tarafında bu maç için güvenilir fikstür eşleşmesi bulunamadı.")
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            raise OddsProviderError("Birden fazla olası fikstür eşleşmesi var; oran çekimi güvenlik için durduruldu.")
        fixture_id = candidates[0][1]
        if not fixture_id:
            raise OddsProviderError("API-Football fikstür kimliği eksik.")
        return {"fixture_id": int(fixture_id), "home": candidates[0][2], "away": candidates[0][3]}

    @staticmethod
    def parse_odds(payload, source):
        captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        output = []
        for fixture in payload.get("response") or []:
            for bookmaker in fixture.get("bookmakers") or []:
                bookmaker_id = bookmaker.get("id")
                bookmaker_name = str(bookmaker.get("name") or "Bilinmeyen bahis şirketi")
                for bet in bookmaker.get("bets") or []:
                    market_id = bet.get("id")
                    market_name = str(bet.get("name") or "Diğer")
                    for value in bet.get("values") or []:
                        odd = value.get("odd")
                        try:
                            odd = float(odd)
                        except (TypeError, ValueError):
                            continue
                        if odd <= 1 or odd > 10000:
                            continue
                        output.append({
                            "fixture_id": str(((fixture.get("fixture") or {}).get("id") or "")),
                            "source": source,
                            "bookmaker_id": str(bookmaker_id or ""),
                            "bookmaker": bookmaker_name,
                            "market_id": str(market_id or ""),
                            "market": market_name,
                            "selection": str(value.get("value") or ""),
                            "handicap": str(value.get("handicap") or ""),
                            "odd": f"{odd:.4f}".rstrip("0").rstrip("."),
                            "suspended": bool(value.get("suspended", False)),
                            "captured_at": captured_at,
                            "raw": value,
                        })
        return output

    def pre_match_odds(self, fixture_id):
        return self.parse_odds(self._get("/odds", {"fixture": fixture_id}), "prematch")

    def live_odds(self, fixture_id=None):
        params = {"fixture": fixture_id} if fixture_id else {}
        return self.parse_odds(self._get("/odds/live", params), "live")
