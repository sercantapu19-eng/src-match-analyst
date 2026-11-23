"""Optional OpenLigaDB adapter.

This module is intentionally isolated from the active SportScore pipeline.
It normalizes OpenLigaDB match data but does not write to the database.
Callers must cache centrally and obey OpenLigaDB API fair-use terms.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import requests

BASE_URL = "https://api.openligadb.de"


class OpenLigaDBError(RuntimeError):
    pass


class OpenLigaDBProvider:
    def __init__(self, session: requests.Session | None = None, timeout: int = 12):
        self.session = session or requests.Session()
        self.timeout = timeout
        app_name = os.getenv("OPENLIGADB_USER_AGENT", "SRC-Match-Analyst/1.0")
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": app_name,
        })

    def _get(self, path: str) -> Any:
        try:
            response = self.session.get(f"{BASE_URL}{path}", timeout=self.timeout)
        except requests.RequestException as exc:
            raise OpenLigaDBError(f"OpenLigaDB connection error: {exc}") from exc
        if response.status_code == 429:
            raise OpenLigaDBError("OpenLigaDB rate limit reached; retry later with backoff.")
        if not response.ok:
            raise OpenLigaDBError(f"OpenLigaDB HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise OpenLigaDBError("OpenLigaDB returned invalid JSON.") from exc

    def available_leagues(self, season: int | None = None) -> list[dict]:
        path = "/getavailableleagues" + (f"/{int(season)}" if season else "")
        payload = self._get(path)
        return payload if isinstance(payload, list) else []

    def matches(self, league_shortcut: str, season: int) -> list[dict]:
        shortcut = str(league_shortcut).strip()
        if not shortcut or "/" in shortcut:
            raise ValueError("A valid OpenLigaDB league shortcut is required.")
        payload = self._get(f"/getmatchdata/{shortcut}/{int(season)}")
        rows = payload if isinstance(payload, list) else []
        return [self.normalize_match(row, shortcut) for row in rows if isinstance(row, dict)]

    @staticmethod
    def _team_name(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("TeamName") or value.get("name") or "").strip()
        return str(value or "").strip()

    @staticmethod
    def _result(match: dict, half_time: bool) -> tuple[int | None, int | None]:
        results = match.get("MatchResults") or match.get("matchResults") or []
        if not isinstance(results, list):
            return None, None
        for result in results:
            if not isinstance(result, dict):
                continue
            name = str(result.get("ResultName") or result.get("resultName") or "").casefold()
            type_id = result.get("ResultTypeID", result.get("resultTypeId"))
            order_id = result.get("ResultOrderID", result.get("resultOrderId"))
            is_half = ("halbzeit" in name or "half" in name or str(type_id) == "1")
            is_final = ("end" in name or "final" in name or str(type_id) == "2")
            if half_time and not is_half:
                continue
            if not half_time and not is_final:
                continue
            try:
                home = int(result.get("PointsTeam1", result.get("pointsTeam1")))
                away = int(result.get("PointsTeam2", result.get("pointsTeam2")))
                return home, away
            except (TypeError, ValueError):
                continue
        # Older/variant payloads may expose only ordered result values.
        if not half_time:
            candidates = []
            for result in results:
                if isinstance(result, dict):
                    try:
                        candidates.append((int(result.get("ResultOrderID", result.get("resultOrderId", 0))),
                                           int(result.get("PointsTeam1", result.get("pointsTeam1"))),
                                           int(result.get("PointsTeam2", result.get("pointsTeam2")))))
                    except (TypeError, ValueError):
                        pass
            if candidates:
                _, home, away = max(candidates)
                return home, away
        return None, None

    @classmethod
    def normalize_match(cls, match: dict, league_shortcut: str = "") -> dict:
        home = cls._team_name(match.get("Team1") or match.get("team1"))
        away = cls._team_name(match.get("Team2") or match.get("team2"))
        kickoff = match.get("MatchDateTimeUTC") or match.get("MatchDateTime") or ""
        if kickoff:
            try:
                parsed = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                kickoff = parsed.isoformat()
            except ValueError:
                kickoff = str(kickoff)
        ht_home, ht_away = cls._result(match, True)
        ft_home, ft_away = cls._result(match, False)
        is_finished = ft_home is not None and ft_away is not None
        return {
            "id": str(match.get("MatchID") or match.get("matchId") or ""),
            "home": home,
            "away": away,
            "league": str(league_shortcut),
            "kickoff": kickoff,
            "status": "finished" if is_finished else "scheduled",
            "status_short": "FT" if is_finished else "",
            "home_goals": ft_home,
            "away_goals": ft_away,
            "ht_home": ht_home,
            "ht_away": ht_away,
            "provider": "openligadb",
            "source_attribution": "Data: OpenLigaDB (ODbL 1.0) — https://www.openligadb.de",
        }
