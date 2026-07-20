# -*- coding: utf-8 -*-
"""
Data layer for plugin.audio.kexp (PROTOTYPE).

Everything network- and cache-shaped lives here, shared by default.py
(browsing/resolving) and service.py (now-playing metadata).

API facts in this module come from the reverse-engineering reference
(kexp-archive-api.md, 2026-07-19/20 verification session):

  * Browse layer: the public-but-undocumented v2 API (api.kexp.org/v2).
    A v2 "show" is one broadcast instance with a start_time; there is no
    end_time -- show N ends where show N+1 starts.
  * Stream resolution: the unversioned get_streaming_url endpoint.
    GET ?bitrate=N&timestamp=ISO ->
        {"sg-url": <mp3 containing that moment>,
         "sg-url-next": <the following show's file>,
         "sg-offset": <seconds from file start to the requested moment>}
    URLs are stable/deterministic/unsigned; one file per show; files
    start ~10 min before the nominal hour, so NEVER do schedule math --
    trust sg-offset.
  * The StreamGuys host wraps delivery in an AIS listening session via a
    302 redirect; Kodi follows it natively. Never cache session-stamped
    URLs; always hand Kodi the canonical sg-url.
  * bitrate is a forgiving preference (bad values still return a
    stream). Whether it selects real variants is unverified.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import xbmc
import xbmcaddon
import xbmcvfs

ADDON = xbmcaddon.Addon("plugin.audio.kexp")
ADDON_ID: str = ADDON.getAddonInfo("id")

PROFILE: str = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
SHOWS_CACHE_PATH: str = os.path.join(PROFILE, "shows_cache.json")
SESSION_PATH: str = os.path.join(PROFILE, "archive_session.json")

API_V2: str = "https://api.kexp.org/v2"
RESOLVER: str = "https://api.kexp.org/get_streaming_url/"
# Same live stream the internetradio add-on uses for its KEXP station.
LIVE_STREAM_URL: str = "https://kexp.streamguys1.com/kexp160.aac"
USER_AGENT: str = "Kodi plugin.audio.kexp prototype"

ARCHIVE_DAYS: int = 14          # verified retention window
SHOWS_CACHE_TTL: float = 900.0  # be polite: reuse the show list for 15 min
MAX_SHOW_PAGES: int = 12        # hard cap on pagination, whatever happens


def log(msg: str, level: int = xbmc.LOGINFO) -> None:
    xbmc.log(f"[{ADDON_ID}/data] {msg}", level)


def ensure_profile() -> None:
    os.makedirs(PROFILE, exist_ok=True)


def get_json(url: str, timeout: float = 10.0) -> dict[str, Any] | None:
    """GET a JSON document. GET only -- the API 405s on HEAD."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data: Any = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as e:
        log(f"request failed ({url}): {e}", xbmc.LOGWARNING)
        return None


# --- shows (the browse layer) ----------------------------------------------

def parse_iso(ts: str) -> datetime | None:
    """Parse the API's ISO-8601 timestamps (offset-aware, incl. 'Z')."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def fetch_shows(force: bool = False) -> list[dict[str, Any]]:
    """All shows in the archive window, newest first, disk-cached.

    Fetches /v2/shows/ ordered -start_time and paginates via the DRF
    'next' links until results age out of the window. Each returned show
    dict is the raw API object plus 'start_epoch' (float, UTC).

    Defensive notes (diagnostic build): the ordering parameter is
    conventional DRF but its support on this endpoint is unverified. If
    the server ignores it and returns ascending-order results, every
    first-page show will predate the window and this returns [] with a
    loud log line -- which is the shakedown signal to adjust.
    """
    ensure_profile()
    if not force:
        cached = _read_cache()
        if cached is not None:
            return cached

    cutoff: float = time.time() - ARCHIVE_DAYS * 86400.0
    shows: list[dict[str, Any]] = []
    url: str | None = f"{API_V2}/shows/?" + urllib.parse.urlencode(
        {"ordering": "-start_time", "limit": 100})
    pages = 0
    while url and pages < MAX_SHOW_PAGES:
        pages += 1
        data = get_json(url)
        if data is None:
            break
        results: Any = data.get("results")
        if not isinstance(results, list):
            log("shows response had no results list", xbmc.LOGWARNING)
            break
        page_in_window = 0
        for s in results:
            if not isinstance(s, dict):
                continue
            dt = parse_iso(str(s.get("start_time", "")))
            if dt is None:
                continue
            epoch: float = dt.timestamp()
            if epoch < cutoff:
                continue
            s["start_epoch"] = epoch
            shows.append(s)
            page_in_window += 1
        log(f"shows page {pages}: {len(results)} results, "
            f"{page_in_window} in window")
        if page_in_window == 0:
            # Either we paged past the window (done) or ordering is not
            # what we expect (shakedown watch item).
            if pages == 1:
                log("first shows page entirely outside the archive window"
                    " -- ordering=-start_time may be unsupported",
                    xbmc.LOGWARNING)
            break
        url = data.get("next") if isinstance(data.get("next"), str) else None

    shows.sort(key=lambda s: s["start_epoch"], reverse=True)
    log(f"fetched {len(shows)} shows in the {ARCHIVE_DAYS}-day window"
        f" ({pages} page(s))")
    if shows:
        _write_cache(shows)
    return shows


def _read_cache() -> list[dict[str, Any]] | None:
    try:
        with open(SHOWS_CACHE_PATH, "r", encoding="utf-8") as f:
            blob: Any = json.load(f)
        if (isinstance(blob, dict)
                and time.time() - float(blob.get("fetched_at", 0))
                < SHOWS_CACHE_TTL
                and isinstance(blob.get("shows"), list)):
            log(f"shows cache hit ({len(blob['shows'])} shows)")
            return blob["shows"]
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return None


def _write_cache(shows: list[dict[str, Any]]) -> None:
    tmp = SHOWS_CACHE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "shows": shows}, f)
        os.replace(tmp, SHOWS_CACHE_PATH)
    except OSError as e:
        log(f"shows cache write failed: {e}", xbmc.LOGWARNING)


def show_label(show: dict[str, Any]) -> str:
    """'Program with Host, Host' -- same rendering as internetradio."""
    program: str = str(show.get("program_name") or "")
    hosts: Any = show.get("host_names")
    host_str: str = ", ".join(hosts) if isinstance(hosts, list) else ""
    if program and host_str:
        return f"{program} with {host_str}"
    return program or host_str or "KEXP"


def group_by_day(shows: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """[(YYYY-MM-DD local, [shows newest-first])], newest day first."""
    days: dict[str, list[dict[str, Any]]] = {}
    for s in shows:
        local = datetime.fromtimestamp(s["start_epoch"]).astimezone()
        key = local.strftime("%Y-%m-%d")
        days.setdefault(key, []).append(s)
    return sorted(days.items(), key=lambda kv: kv[0], reverse=True)


def unique_programs(shows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One representative (newest) show per program, sorted by name."""
    seen: dict[Any, dict[str, Any]] = {}
    for s in shows:                       # shows arrive newest-first
        pid = s.get("program")
        if pid is not None and pid not in seen:
            seen[pid] = s
    return sorted(seen.values(),
                  key=lambda s: str(s.get("program_name") or "").lower())


def unique_hosts(shows: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """[(host_id, host_name)] across the window, sorted by name."""
    seen: dict[int, str] = {}
    for s in shows:
        ids: Any = s.get("hosts")
        names: Any = s.get("host_names")
        if isinstance(ids, list) and isinstance(names, list):
            for hid, hname in zip(ids, names):
                if isinstance(hid, int) and hid not in seen:
                    seen[hid] = str(hname)
    return sorted(seen.items(), key=lambda kv: kv[1].lower())


# --- stream resolution ------------------------------------------------------

def resolve_stream(timestamp: str, bitrate: str) -> dict[str, Any] | None:
    """Call get_streaming_url for a moment in the archive.

    Returns the raw response dict ({'sg-url', 'sg-url-next',
    'sg-offset'}) or None. Error shapes for out-of-window timestamps are
    an unverified area -- anything without an sg-url is treated as
    failure and logged verbatim for the shakedown record.
    """
    url = RESOLVER + "?" + urllib.parse.urlencode(
        {"bitrate": bitrate, "timestamp": timestamp})
    log(f"resolving: {url}")
    data = get_json(url)
    if data is None:
        return None
    if not data.get("sg-url"):
        log(f"resolver response without sg-url: {data!r}", xbmc.LOGWARNING)
        return None
    log(f"resolved: offset={data.get('sg-offset')!r} "
        f"url={data.get('sg-url')!r}")
    return data


# --- archive session handoff (plugin -> service) ---------------------------
# setResolvedUrl item properties reaching getPlayingItem() is plausible but
# unvalidated on-device, so the resolver ALSO drops this file; the service
# uses the item property when present and falls back to matching the
# playing file against sg_url. The diagnostic log records which path won.

def write_session(info: dict[str, Any]) -> None:
    ensure_profile()
    info = dict(info)
    info["written_at"] = time.time()
    tmp = SESSION_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=1)
        os.replace(tmp, SESSION_PATH)
        log(f"archive session written: {info.get('sg_url')!r}"
            f" offset={info.get('offset')}")
    except OSError as e:
        log(f"archive session write failed: {e}", xbmc.LOGWARNING)


def read_session() -> dict[str, Any] | None:
    try:
        with open(SESSION_PATH, "r", encoding="utf-8") as f:
            data: Any = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None
