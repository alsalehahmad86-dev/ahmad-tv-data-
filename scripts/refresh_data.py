#!/usr/bin/env python3
"""Refresh public football, finance and French public-holiday JSON feeds.

This repository contains data-pipeline code only; no URTV application source,
credentials, IPTV information, or user-specific location is stored here.
All files are written atomically only after their upstream response validates.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOOTBALL_API = "https://api.football-data.org/v4"
COMPETITIONS = ("PD", "PL", "FL1", "BL1", "SA", "CL")


def get_json(url: str, headers: dict[str, str] | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": "ahmad-tv-data-feed/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=25) as response:
        if response.status != 200:
            raise RuntimeError(f"Upstream returned HTTP {response.status}: {urllib.parse.urlsplit(url).hostname}")
        return json.loads(response.read().decode("utf-8"))


def atomic_json(name: str, payload: dict):
    dest = ROOT / name
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    tmp.replace(dest)


def football(token: str, now: dt.datetime | None = None):
    now = now or dt.datetime.now(dt.timezone.utc)
    games: dict[int, dict] = {}
    windows = ((-5, 4), (4, 7))
    for index, (start, end) in enumerate(windows):
        if index:
            time.sleep(1.1)
        query = urllib.parse.urlencode({
            "competitions": ",".join(COMPETITIONS),
            "dateFrom": (now.date() + dt.timedelta(days=start)).isoformat(),
            "dateTo": (now.date() + dt.timedelta(days=end)).isoformat(),
        })
        page = get_json(f"{FOOTBALL_API}/matches?{query}", {"X-Auth-Token": token})
        for row in page.get("matches") or []:
            if row.get("id") is not None:
                games[row["id"]] = row

    tables = {}
    for index, code in enumerate(COMPETITIONS):
        time.sleep(1.1)
        data = get_json(f"{FOOTBALL_API}/competitions/{code}/standings", {"X-Auth-Token": token})
        comp = data.get("competition") or {}
        standings = (data.get("standings") or [{}])[0].get("table") or []
        tables[code] = {
            "code": code,
            "name": comp.get("name", code),
            "season": (data.get("season") or {}).get("startDate", "")[:4],
            "table": [{
                "position": item.get("position"),
                "team": (item.get("team") or {}).get("name", ""),
                "crest": (item.get("team") or {}).get("crest", ""),
                "played": item.get("playedGames"), "won": item.get("won"),
                "draw": item.get("draw"), "lost": item.get("lost"),
                "points": item.get("points"), "goalDifference": item.get("goalDifference"),
            } for item in standings],
        }

    matches = []
    for item in games.values():
        comp = item.get("competition") or {}
        home, away = item.get("homeTeam") or {}, item.get("awayTeam") or {}
        kickoff = item.get("utcDate")
        if not (kickoff and home.get("name") and away.get("name")):
            continue
        score = (item.get("score") or {}).get("fullTime") or {}
        status = item.get("status", "SCHEDULED")
        status = {"FINISHED": "FT", "HALF_TIME": "PAUSED"}.get(status, status)
        matches.append({
            "idEvent": f"fd-{item['id']}", "source": "football-data.org",
            "competitionCode": comp.get("code", ""), "strLeague": comp.get("name", ""),
            "strSeason": str((item.get("season") or {}).get("startDate", ""))[:4],
            "strHomeTeam": home["name"], "strAwayTeam": away["name"],
            "strHomeTeamBadge": home.get("crest") or "", "strAwayTeamBadge": away.get("crest") or "",
            "idHomeTeam": f"fd-team-{home.get('id', '')}", "idAwayTeam": f"fd-team-{away.get('id', '')}",
            "strTimestamp": kickoff, "dateEvent": kickoff[:10], "strTime": kickoff[11:19],
            "strStatus": status, "intHomeScore": score.get("home"), "intAwayScore": score.get("away"),
            "intRound": item.get("matchday"), "strSport": "Soccer", "strProgress": "",
        })
    matches.sort(key=lambda x: (x["strTimestamp"], x["idEvent"]))
    return {
        "schema": 1, "source": "football-data.org", "scoresDelayed": True,
        "updatedAt": now.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "timezone": "Europe/Paris", "competitions": list(COMPETITIONS),
        "matches": matches, "standings": tables,
    }


def finance():
    rates = get_json("https://api.frankfurter.dev/v1/latest?base=EUR&symbols=USD,GBP,CHF,TRY")
    gold = get_json("https://xaus.com/api/v1/spot?currency=EUR&unit=gram")
    price = (gold.get("xau") or {}).get("price")
    if not isinstance(price, (int, float)) or not rates.get("rates"):
        raise RuntimeError("Finance response failed validation")
    return {
        "schema": 1, "source": {"fx": "Frankfurter", "gold": "XAUS"},
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "fxDate": rates.get("date"), "base": "EUR", "rates": rates["rates"],
        "gold": {"symbol": "XAU", "price": price, "currency": "EUR", "unit": "gram",
                 "karat": None, "kind": "spot", "sourceUpdatedAt": gold.get("price_as_of"),
                 "stale": bool(gold.get("stale", False))},
    }


def easter_sunday(year: int) -> dt.date:
    # Anonymous Gregorian computus.
    a, b = year % 19, year // 100
    c, d, e = year % 100, (year // 100) // 4, (year // 100) % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(year, month, day)


def french_holidays(year: int):
    fixed = [(1, 1, "Jour de l’An"), (5, 1, "Fête du Travail"), (5, 8, "Victoire 1945"),
             (7, 14, "Fête nationale"), (8, 15, "Assomption"), (11, 1, "Toussaint"),
             (11, 11, "Armistice 1918"), (12, 25, "Noël")]
    easter = easter_sunday(year)
    movable = [(easter + dt.timedelta(days=1), "Lundi de Pâques"),
               (easter + dt.timedelta(days=39), "Ascension"),
               (easter + dt.timedelta(days=50), "Lundi de Pentecôte")]
    rows = [{"date": dt.date(year, month, day).isoformat(), "name": name, "scope": "France"}
            for month, day, name in fixed]
    rows.extend({"date": day.isoformat(), "name": name, "scope": "France"} for day, name in movable)
    rows.sort(key=lambda x: x["date"])
    return rows


def main():
    token = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()
    now = dt.datetime.now(dt.timezone.utc)
    # Build and validate all feeds in memory before replacing any existing file.
    finance_payload = finance()
    holiday_payload = {
        "schema": 1, "source": "French national public holiday calendar",
        "updatedAt": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "timezone": "Europe/Paris", "years": {
            str(year): french_holidays(year) for year in (now.year, now.year + 1)
        },
        "note": "National holidays only; Alsace-Moselle local holidays are not included.",
    }
    if token:
        match_payload = football(token, now)
        atomic_json("football.json", match_payload)
        match_report = f"{len(match_payload['matches'])} matches, {len(match_payload['standings'])} tables"
    else:
        # Keep the last known football file intact until the user adds the free API token.
        match_report = "football skipped (FOOTBALL_DATA_TOKEN is not configured)"
    atomic_json("finance.json", finance_payload)
    atomic_json("fr-holidays.json", holiday_payload)
    print(f"Validated feeds: {match_report}; "
          f"FX date {finance_payload['fxDate']}; gold EUR/g; {sum(map(len, holiday_payload['years'].values()))} holidays.")


if __name__ == "__main__":
    main()
