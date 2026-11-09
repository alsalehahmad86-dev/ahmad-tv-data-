#!/usr/bin/env python3
"""Refresh public football, finance and French public-holiday JSON feeds.

This repository contains data-pipeline code only; no URTV application source,
credentials, IPTV information, or user-specific location is stored here.
All files are written atomically only after their upstream response validates.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOOTBALL_API = "https://api.football-data.org/v4"
TSD_API = "https://www.thesportsdb.com/api/v1/json/123"
OLG_API = "https://api.openligadb.de"
COMPETITIONS = ("PD", "PL", "FL1", "CL")
TSD_LEAGUES = {"PD": "4335", "PL": "4328", "FL1": "4334", "CL": "4480"}
OLG_LEAGUES = {"PD": "la1", "PL": "pl", "CL": "ucl"}
TSD_NATIONAL_LEAGUES = {"UNL": "4490"}
COMP_NAMES_FR = {"PD": "La Liga", "PL": "Premier League", "FL1": "Ligue 1", "CL": "Ligue des champions UEFA",
                 "UNL": "Ligue des nations UEFA", "AFCONQ": "Qualifications AFCON PAMOJA 2027"}
CAF_FIXTURES_URL = "https://www.cafonline.com/afcon2025/news/crucial-matchdays-3-and-4-to-shape-totalenergies-caf-afcon-pamoja-2027-qualification-race/"
# CAF publishes the confirmed AFCON 2027 qualifier schedule as a fixture list, not a public JSON API.
# Keep only fixtures confirmed in its 3 November 2026 article, and verify the official page before emitting them.
CAF_2027_MD3 = (
    ("2026-11-12", "15:00", "Ghana", "Somalia", "Ghana", "Somalia"),
    ("2026-11-12", "19:00", "Côte d’Ivoire", "Gambie", "Côte d’Ivoire", "Gambia"),
    ("2026-11-12", "16:00", "Guinée", "Érythrée", "Guinea", "Eritrea"),
    ("2026-11-12", "16:00", "RD Congo", "Sierra Leone", "DR Congo", "Sierra Leone"),
    ("2026-11-12", "16:00", "Guinée équatoriale", "Zimbabwe", "Equatorial Guinea", "Zimbabwe"),
    ("2026-11-12", "19:00", "Bénin", "République centrafricaine", "Benin", "Central African Republic"),
    ("2026-11-12", "19:00", "Cameroun", "Namibie", "Cameroon", "Namibia"),
    ("2026-11-12", "19:00", "Sénégal", "Soudan", "Senegal", "Sudan"),
    ("2026-11-12", "16:00", "Madagascar", "Guinée-Bissau", "Madagascar", "Guinea-Bissau"),
    ("2026-11-13", "16:00", "Gabon", "Lesotho", "Gabon", "Lesotho"),
    ("2026-11-13", "20:00", "Maroc", "Niger", "Morocco", "Niger"),
    ("2026-11-13", "19:00", "Égypte", "Malawi", "Egypt", "Malawi"),
    ("2026-11-13", "16:00", "Afrique du Sud", "Kenya", "South Africa", "Kenya"),
    ("2026-11-13", "16:00", "Burkina Faso", "Mauritanie", "Burkina Faso", "Mauritania"),
    ("2026-11-13", "13:00", "Comores", "Congo", "Comoros", "Congo"),
    ("2026-11-13", "16:00", "Tunisie", "Libye", "Tunisia", "Libya"),
    ("2026-11-13", "13:00", "Zambie", "Burundi", "Zambia", "Burundi"),
    ("2026-11-13", "13:00", "Mozambique", "Éthiopie", "Mozambique", "Ethiopia"),
    ("2026-11-13", "16:00", "Cap-Vert", "Libéria", "Cape Verde", "Liberia"),
    ("2026-11-13", "19:00", "Mali", "Rwanda", "Mali", "Rwanda"),
    ("2026-11-13", "16:00", "Nigeria", "Tanzanie", "Nigeria", "Tanzania"),
)
# dateTo is exclusive in football-data.org. These disjoint windows include
# seven days of recent results, today, and every date through day +30.
MATCH_WINDOWS = ((-7, 3), (3, 13), (13, 23), (23, 31))


def get_json(url: str, headers: dict[str, str] | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": "ahmad-tv-data-feed/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=25) as response:
        if response.status != 200:
            raise RuntimeError(f"Upstream returned HTTP {response.status}: {urllib.parse.urlsplit(url).hostname}")
        return json.loads(response.read().decode("utf-8"))


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def get_page_text(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "ahmad-tv-data-feed/1.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"Fixture page returned HTTP {response.status}: {urllib.parse.urlsplit(url).hostname}")
        parser = _VisibleText()
        parser.feed(response.read().decode("utf-8", "replace"))
        return " ".join(parser.parts)


def caf_afcon_2027_events(now: dt.datetime):
    """Use only CAF's confirmed MD3 fixture announcement; emit nothing if it can't be verified."""
    text = re.sub(r"\s+", " ", html.unescape(get_page_text(CAF_FIXTURES_URL))).casefold()
    if "matchday 3 fixtures" not in text or "all times in gmt" not in text:
        raise RuntimeError("CAF official MD3 fixture list not present")
    # Confirm the article still contains every listed country and date/time before accepting the curated records.
    for day, kickoff, _home_fr, _away_fr, home_src, away_src in CAF_2027_MD3:
        if home_src.casefold() not in text or away_src.casefold() not in text:
            raise RuntimeError("CAF official fixture names could not be verified")
        time_label = f"{int(day[8:10])}/11, {int(kickoff[:2])}h{kickoff[3:]}"
        if time_label.casefold() not in text:
            raise RuntimeError("CAF official fixture date/time could not be verified")
    checked = now.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = []
    for day, kickoff, home, away, _home_src, _away_src in CAF_2027_MD3:
        stamp = f"{day}T{kickoff}:00Z"
        start = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if not now - dt.timedelta(minutes=1) <= start <= now + dt.timedelta(days=31):
            continue
        rows.append({
            "idEvent": f"caf-afconq-2027-{day.replace('-', '')}-{team_key(home)}-{team_key(away)}",
            "source": "CAF official fixtures", "sourceUrl": CAF_FIXTURES_URL,
            "feedKind": "public-aggregated", "competitionCode": "AFCONQ",
            "strLeague": COMP_NAMES_FR["AFCONQ"], "strCompetitionBadge": "",
            "strSeason": "2027", "strHomeTeam": home, "strAwayTeam": away,
            "strTimestamp": stamp, "dateEvent": day, "strTime": kickoff + ":00",
            "strStatus": "TIMED", "intHomeScore": None, "intAwayScore": None,
            "intRound": 3, "strSport": "Soccer", "strProgress": "",
            "sourceCheckedAt": checked,
        })
    return rows


def thesportsdb_nations_league_events(now: dt.datetime):
    """Get the next UEFA Nations League rounds from TheSportsDB's free V1 schedule feed."""
    events, status = [], {"leagueId": "4490"}
    for code, league_id in TSD_NATIONAL_LEAGUES.items():
        try:
            upcoming = get_json(f"{TSD_API}/eventsnextleague.php?id={league_id}").get("events") or []
            rounds = sorted({int(event.get("intRound")) for event in upcoming if str(event.get("intRound", "")).isdigit()})
            if not rounds:
                raise RuntimeError("TheSportsDB did not provide the next league round")
            found = []
            for round_number in (rounds[0], rounds[0] + 1):
                result = get_json(f"{TSD_API}/eventsround.php?id={league_id}&r={round_number}")
                found.extend(e for raw in result.get("events") or [] if (e := tsd_event(raw, code)))
                time.sleep(0.25)
            start = now - dt.timedelta(days=7)
            end = now + dt.timedelta(days=31)
            events.extend(e for e in found if start <= dt.datetime.fromisoformat(e["strTimestamp"].replace("Z", "+00:00")) <= end)
            status[code] = {"roundsChecked": [rounds[0], rounds[0] + 1], "matches": len(events)}
        except Exception as exc:
            status[code] = {"error": type(exc).__name__}
    return events, status


def atomic_json(name: str, payload: dict):
    dest = ROOT / name
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    tmp.replace(dest)


def iso_utc(value):
    if not value:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        return ""


def team_key(value):
    import unicodedata
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = value.replace("ø", "o")
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = " ".join(value.replace("&", " and ").replace(".", " ").split())
    for suffix in (" football club", " fc", " cf", " sc", " ac", " club"):
        if value.endswith(suffix):
            value = value[:-len(suffix)].strip()
    value = value.removeprefix("fc ").removeprefix("cf ").removeprefix("sc ").removeprefix("fk ")
    aliases = {"deportivo alaves": "alaves", "alaves": "alaves", "manchester city": "man city",
               "man city": "man city", "manchester united": "man united", "man united": "man united",
               "paris saint germain": "psg", "paris sg": "psg", "psg": "psg",
               "fc barcelona": "barcelona", "barcelona": "barcelona", "real madrid": "real madrid",
               "es troyes": "troyes", "troyes": "troyes", "bodo/glimt": "bodo/glimt"}
    value = aliases.get(value, value)
    return value


def event_key(event):
    kickoff = str(event.get("strTimestamp") or "")[:10]
    home, away = team_key(event.get("strHomeTeam")), team_key(event.get("strAwayTeam"))
    if not kickoff or not home or not away:
        return ""
    return "|".join((str(event.get("competitionCode") or ""), kickoff, *sorted((home, away))))


def tsd_event(raw, code):
    home, away = raw.get("strHomeTeam"), raw.get("strAwayTeam")
    stamp = iso_utc(raw.get("strTimestamp"))
    if not stamp and raw.get("dateEvent"):
        stamp = iso_utc(f"{raw['dateEvent']}T{raw.get('strTime') or '00:00:00'}Z")
    if not stamp or not home or not away:
        return None
    return {
        "idEvent": f"tsd-{raw.get('idEvent')}", "source": "TheSportsDB",
        "feedKind": "public-aggregated",
        "competitionCode": code, "strLeague": COMP_NAMES_FR[code],
        "strCompetitionBadge": raw.get("strLeagueBadge") or raw.get("strLeagueLogo") or "",
        "strSeason": raw.get("strSeason") or "", "strHomeTeam": home, "strAwayTeam": away,
        "idHomeTeam": f"tsd-team-{raw.get('idHomeTeam') or ''}",
        "idAwayTeam": f"tsd-team-{raw.get('idAwayTeam') or ''}",
        "strHomeTeamBadge": raw.get("strHomeTeamBadge") or "",
        "strAwayTeamBadge": raw.get("strAwayTeamBadge") or "",
        "strTimestamp": stamp, "dateEvent": stamp[:10], "strTime": stamp[11:19],
        "strStatus": raw.get("strStatus") or "SCHEDULED",
        "intHomeScore": raw.get("intHomeScore"), "intAwayScore": raw.get("intAwayScore"),
        "intRound": raw.get("intRound"), "strSport": "Soccer", "strProgress": "",
        "sourceUpdatedAt": iso_utc(raw.get("dateModified")) or None,
    }


def tsd_standing(raw):
    return {
        "position": raw.get("intRank"), "team": raw.get("strTeam", ""),
        "played": raw.get("intPlayed"), "won": raw.get("intWin"), "draw": raw.get("intDraw"),
        "lost": raw.get("intLoss"), "points": raw.get("intPoints"),
        "goalDifference": raw.get("intGoalDifference"),
    }


def fd_event(item):
    comp = item.get("competition") or {}
    home, away = item.get("homeTeam") or {}, item.get("awayTeam") or {}
    kickoff = iso_utc(item.get("utcDate"))
    if not (kickoff and home.get("name") and away.get("name")):
        return None
    score = (item.get("score") or {}).get("fullTime") or {}
    status = {"FINISHED": "FT", "HALF_TIME": "PAUSED"}.get(item.get("status", "SCHEDULED"), item.get("status", "SCHEDULED"))
    return {
        "idEvent": f"fd-{item['id']}", "source": "football-data.org", "feedKind": "public-aggregated", "competitionCode": comp.get("code", ""),
        "strLeague": COMP_NAMES_FR.get(comp.get("code"), comp.get("name", "")),
        "strCompetitionBadge": comp.get("emblem") or "",
        "strSeason": str((item.get("season") or {}).get("startDate", ""))[:4],
        "strHomeTeam": home["name"], "strAwayTeam": away["name"],
        "idHomeTeam": f"fd-team-{home.get('id', '')}", "idAwayTeam": f"fd-team-{away.get('id', '')}",
        "strTimestamp": kickoff, "dateEvent": kickoff[:10], "strTime": kickoff[11:19],
        "strStatus": status, "intHomeScore": score.get("home"), "intAwayScore": score.get("away"),
        "intRound": item.get("matchday"), "strSport": "Soccer", "strProgress": "",
        "sourceUpdatedAt": iso_utc(item.get("lastUpdated")) or None,
    }


def olg_event(raw, code):
    home, away = raw.get("team1") or {}, raw.get("team2") or {}
    kickoff = iso_utc(raw.get("matchDateTimeUTC") or raw.get("matchDateTime"))
    if not (kickoff and home.get("teamName") and away.get("teamName")):
        return None
    results = raw.get("matchResults") or []
    final = next((r for r in reversed(results) if r.get("pointsTeam1") is not None and r.get("pointsTeam2") is not None), {})
    finished = bool(raw.get("matchIsFinished"))
    return {
        "idEvent": f"olg-{raw.get('matchID')}", "source": "OpenLigaDB", "feedKind": "public-aggregated", "competitionCode": code,
        "strLeague": COMP_NAMES_FR[code], "strSeason": str(raw.get("leagueSeason") or ""),
        "strHomeTeam": home["teamName"], "strAwayTeam": away["teamName"],
        "idHomeTeam": f"olg-team-{home.get('teamId', '')}", "idAwayTeam": f"olg-team-{away.get('teamId', '')}",
        "strTimestamp": kickoff, "dateEvent": kickoff[:10], "strTime": kickoff[11:19],
        "strStatus": "FT" if finished else "SCHEDULED",
        "intHomeScore": final.get("pointsTeam1") if finished else None,
        "intAwayScore": final.get("pointsTeam2") if finished else None,
        "intRound": raw.get("group", {}).get("groupOrderID"), "strSport": "Soccer", "strProgress": "",
        "sourceUpdatedAt": iso_utc(raw.get("lastUpdateDateTime")) or None,
    }


def olg_table(raw):
    # OpenLigaDB's getbltable response is already ordered by rank, but does not
    # provide leaguePosition and calls the goal-difference field goalDiff.
    # Derive the ordinal from that provider order and preserve its real value.
    return [{
        "position": r.get("leaguePosition") or position, "team": r.get("teamName", ""),
        "played": r.get("matches"), "won": r.get("won"), "draw": r.get("draw"), "lost": r.get("lost"),
        "points": r.get("points"), "goalDifference": r.get("goalDifference", r.get("goalDiff")),
    } for position, r in enumerate((row for row in raw if row.get("teamName")), start=1)]


def football(token: str = "", now: dt.datetime | None = None):
    """Use the free public schedules first, with the full-cover club feeds as gap fillers.

    TheSportsDB's free league next/previous endpoints return one event and its table endpoint five rows.
    football-data.org is the full-coverage source for the four supported club competitions. OpenLigaDB
    is a no-key fallback for PD/PL/CL only. National fixtures are limited to the next UEFA Nations
    League rounds and CAF's verified public schedule. Scores are delayed, never represented as live.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    checked = now.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    tsd_events, tables, source_status, failures = [], {}, {}, []
    # Three free schedule/table lookups per club league.
    for code, league_id in TSD_LEAGUES.items():
        next_events, previous, table = [], [], []
        for label, path in (("next", f"eventsnextleague.php?id={league_id}"),
                            ("previous", f"eventspastleague.php?id={league_id}"),
                            ("table", f"lookuptable.php?l={league_id}")):
            try:
                response = get_json(f"{TSD_API}/{path}")
                if label == "next": next_events = response.get("events") or []
                elif label == "previous": previous = response.get("results") or []
                else: table = response.get("table") or []
            except Exception:
                pass
        tsd_events.extend(e for r in next_events + previous if (e := tsd_event(r, code)))
        if table:
            tables[code] = {
                "code": code, "name": COMP_NAMES_FR[code], "season": table[0].get("strSeason", ""),
                "source": "TheSportsDB", "complete": False,
                "table": [tsd_standing(row) for row in table],
            }
        source_status[code] = {"thesportsdb": {"next": len(next_events), "previous": len(previous), "tableRows": len(table)}}

    # Club fixtures above come from the four supported club leagues. The free
    # football-data.org tier has no national-team competitions, so fill the
    # current national-team schedule from TheSportsDB and CAF's official list.
    national_events, national_status = thesportsdb_nations_league_events(now)
    tsd_events.extend(national_events)
    source_status["UNL"] = {"thesportsdb": national_status.get("UNL", {}),
                            "matchCoverage": "round schedules" if national_status.get("UNL", {}).get("matches") else "unavailable"}
    try:
        caf_events = caf_afcon_2027_events(now)
        source_status["AFCONQ"] = {"cafOfficialFixtures": {"matches": len(caf_events), "url": CAF_FIXTURES_URL},
                                  "matchCoverage": "confirmed fixtures"}
    except Exception as exc:
        caf_events = []
        failures.append(f"CAF official AFCON fixtures: {type(exc).__name__}")
        source_status["AFCONQ"] = {"cafOfficialFixtures": {"error": type(exc).__name__},
                                  "matchCoverage": "unavailable"}
    tsd_events.extend(caf_events)

    # Four fixture windows plus four tables use eight calls, under the free
    # tier's ten calls/minute limit. dateTo is exclusive.
    fd_events, fd_comp_available, fd_table_available = [], set(), set()
    if token:
        try:
            for index, (start, end) in enumerate(MATCH_WINDOWS):
                if index: time.sleep(1.1)
                query = urllib.parse.urlencode({
                    "competitions": ",".join(COMPETITIONS),
                    "dateFrom": (now.date() + dt.timedelta(days=start)).isoformat(),
                    "dateTo": (now.date() + dt.timedelta(days=end)).isoformat(),
                })
                page = get_json(f"{FOOTBALL_API}/matches?{query}", {"X-Auth-Token": token})
                for raw in page.get("matches") or []:
                    event = fd_event(raw)
                    if event: fd_events.append(event)
            fd_comp_available.update(COMPETITIONS)
        except Exception as exc:
            failures.append(f"football-data.org matches: {type(exc).__name__}")
        for index, code in enumerate(COMPETITIONS):
            try:
                time.sleep(1.1)
                data = get_json(f"{FOOTBALL_API}/competitions/{code}/standings", {"X-Auth-Token": token})
                comp = data.get("competition") or {}
                rows = (data.get("standings") or [{}])[0].get("table") or []
                if rows:
                    tables[code] = {
                        "code": code, "name": COMP_NAMES_FR[code],
                        "season": str((data.get("season") or {}).get("startDate", ""))[:4],
                        "source": "football-data.org", "complete": True,
                        "table": [{
                            "position": row.get("position"), "team": (row.get("team") or {}).get("name", ""),
                            "played": row.get("playedGames"), "won": row.get("won"), "draw": row.get("draw"),
                            "lost": row.get("lost"), "points": row.get("points"),
                            "goalDifference": row.get("goalDifference"),
                        } for row in rows],
                    }
                    fd_table_available.add(code)
            except Exception as exc:
                failures.append(f"football-data.org {code}: {type(exc).__name__}")
    else:
        failures.append("football-data.org skipped: token not configured")

    # OpenLigaDB is a fallback only when football-data.org lacks a competition response.
    # Its actual 2026 season catalogue has PD, PL and CL, but no Ligue 1.
    olg_events, olg_match_available = [], set()
    for code, shortcut in OLG_LEAGUES.items():
        if code in fd_comp_available and code in fd_table_available:
            continue
        try:
            season = now.year if now.month >= 7 else now.year - 1
            if code not in fd_comp_available:
                raw_matches = get_json(f"{OLG_API}/getmatchdata/{shortcut}/{season}")
                olg_match_available.add(code)
                for raw in raw_matches:
                    event = olg_event(raw, code)
                    if event:
                        kickoff = dt.datetime.fromisoformat(event["strTimestamp"].replace("Z", "+00:00"))
                        if now - dt.timedelta(days=7) <= kickoff <= now + dt.timedelta(days=30, hours=23, minutes=59, seconds=59):
                            olg_events.append(event)
            if code not in fd_table_available and (code not in tables or not tables[code].get("complete")):
                rows = olg_table(get_json(f"{OLG_API}/getbltable/{shortcut}/{season}"))
                if rows and (code not in tables or len(rows) > len(tables[code].get("table", []))):
                    tables[code] = {"code": code, "name": COMP_NAMES_FR[code], "season": str(season),
                                    "source": "OpenLigaDB", "complete": True, "table": rows}
            source_status[code]["openligadb"] = "utilisé si football-data.org indisponible"
        except Exception as exc:
            source_status[code]["openligadbError"] = type(exc).__name__

    # Keep TheSportsDB's records first for matching events; use full-coverage feeds to fill its free-tier gaps.
    matches_by_key = {}
    for event in tsd_events + fd_events + olg_events:
        key = event_key(event)
        if not key:
            continue
        old = matches_by_key.get(key)
        if old is None:
            matches_by_key[key] = event
        else:
            # Keep complete source timing/status/scores and merge non-empty metadata, never duplicate the fixture.
            primary = old if old.get("source") == "TheSportsDB" else event if event.get("source") == "TheSportsDB" else old
            other = event if primary is old else old
            merged = dict(other); merged.update({k: v for k, v in primary.items() if v not in (None, "")})
            # football-data's delayed result/status is the authoritative value when available.
            fd = next((x for x in (old, event) if x.get("source") == "football-data.org"), None)
            if fd:
                for k in ("strTimestamp", "dateEvent", "strTime", "strStatus", "intHomeScore", "intAwayScore"):
                    if k in fd: merged[k] = fd[k]
            merged["source"] = "+".join(dict.fromkeys((old.get("source", ""), event.get("source", ""))))
            merged["feedKind"] = "public-aggregated"
            matches_by_key[key] = merged
    for code in COMPETITIONS:
        source_status.setdefault(code, {})
        match_coverage = "complete" if code in fd_comp_available or code in olg_match_available else "partial"
        table_coverage = "complete" if tables.get(code, {}).get("complete") else "partial" if code in tables else "unavailable"
        source_status[code]["matchCoverage"] = match_coverage
        source_status[code]["tableCoverage"] = table_coverage
        source_status[code]["coverage"] = "complete" if match_coverage == table_coverage == "complete" else "partial"
    matches = sorted(matches_by_key.values(), key=lambda x: (x["strTimestamp"], x["competitionCode"], x["idEvent"]))
    source_data_times = [e.get("sourceUpdatedAt") for e in matches if e.get("sourceUpdatedAt")]
    competition_badges = {e["competitionCode"]: e["strCompetitionBadge"] for e in matches
                          if e.get("competitionCode") and e.get("strCompetitionBadge")}
    return {
        "schema": 1, "source": "multi-source", "scoresDelayed": True,
        "updatedAt": checked, "checkedAt": checked,
        "sourceDataUpdatedAt": max(source_data_times) if source_data_times else None,
        "timezone": "Europe/Paris", "competitions": [*COMPETITIONS, "UNL", "AFCONQ"],
        "fixtureRange": {"from": (now.date() - dt.timedelta(days=7)).isoformat(),
                         "through": (now.date() + dt.timedelta(days=31)).isoformat()},
        "competitionBadges": competition_badges,
        "sources": ["TheSportsDB", "football-data.org", "OpenLigaDB", "CAF official fixtures"],
        "attribution": {"TheSportsDB": "Sports data: TheSportsDB",
                        "football-data.org": "Football data provided by the Football-Data.org API",
                        "OpenLigaDB": "Daten von OpenLigaDB (ODbL 1.0)",
                        "CAF official fixtures": CAF_FIXTURES_URL},
        "sourceStatus": source_status, "providerFailures": failures,
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
    rows.extend([
        {"date": (easter - dt.timedelta(days=2)).isoformat(), "name": "Vendredi saint", "scope": "Alsace-Moselle"},
        {"date": dt.date(year, 12, 26).isoformat(), "name": "Saint-Étienne", "scope": "Alsace-Moselle"},
    ])
    rows.sort(key=lambda x: x["date"])
    return rows


def main():
    token = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()
    now = dt.datetime.now(dt.timezone.utc)
    # Build and validate all feeds in memory before replacing any existing file.
    try:
        finance_payload = finance()
    except Exception as exc:
        finance_payload = None
        print(f"Finance feed retained its last valid file ({type(exc).__name__}).")
    holiday_payload = {
        "schema": 1, "source": "French public holiday calendar",
        "timezone": "Europe/Paris", "years": {
            str(year): french_holidays(year) for year in (now.year, now.year + 1)
        },
        "note": "National holidays only; Alsace-Moselle local holidays are not included.",
    }
    match_payload = football(token, now)
    if not match_payload["matches"]:
        # Do not replace a valid matches feed with an empty upstream response, even if tables answered.
        raise RuntimeError("All football sources returned no usable matches; keeping the previous football.json")
    atomic_json("football.json", match_payload)
    partial = [code for code, status in match_payload["sourceStatus"].items() if status.get("coverage") != "complete"]
    match_report = (f"{len(match_payload['matches'])} deduplicated matches, "
                    f"{len(match_payload['standings'])} tables, checked {match_payload['checkedAt']}, "
                    f"partial coverage: {','.join(partial) if partial else 'none'}")
    if finance_payload:
        atomic_json("finance.json", finance_payload)
    atomic_json("fr-holidays.json", holiday_payload)
    print(f"Validated feeds: {match_report}; "
          f"FX date {finance_payload['fxDate'] if finance_payload else 'unchanged'}; "
          f"gold EUR/g; {sum(map(len, holiday_payload['years'].values()))} holidays.")


if __name__ == "__main__":
    main()
