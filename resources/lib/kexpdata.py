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


# --- day tile (Shows-by-day grid) ------------------------------------------
# Kodi's runtime image decoder (setArt thumbs) has NO SVG handler on
# Omega/LibreELEC ("Could not find suitable input format: image/svg+xml").
# It DOES decode PNG. But there is no image/font library in Kodi's Python,
# so we can't render text on device. Instead we ship pre-rendered PNG strips
# and composite the three strips (month band / day numeral / weekday band)
# for a given date by splicing raw scanlines with stdlib only. Authoring the
# strips once (a human/PIL job) keeps the DEVICE code to pure pixel-copying.
#
# i18n: month and weekday names are language-specific, so strips live under
# resources/media/datestrips/<lang>/ (en-gb is the shipped default). A
# translator drops in a sibling folder (e.g. de/) with 50 strips + a
# labels.json and it's picked up when Kodi's UI language matches. Lookup is
# by ISO-639-1 code (en/de/fr...), with "en" mapping to the en-gb folder and
# en-gb as the universal fallback.
#
# Cache naming: the composite is named by its localized CONTENT, e.g.
# tile-en-JUL-18-SAT.png -- stable across runs (Kodi's texture cache keys on
# path, so a stable name is cache-clean) and language-scoped (so switching
# language can't collide two different images on one name). PNG, not JPEG:
# these are hard-edged flat-colour text tiles, JPEG's worst case.

TILES_DIR: str = os.path.join(PROFILE, "date_tiles")
STRIPS_ROOT: str = os.path.join(
    xbmcvfs.translatePath(ADDON.getAddonInfo("path")),
    "resources", "media", "datestrips")
DEFAULT_LANG_DIR: str = "en-gb"


def _ui_lang_code() -> str:
    """Kodi UI language as an ISO-639-1 code (e.g. 'en', 'de'); 'en' on any
    uncertainty."""
    try:
        code = xbmc.getLanguage(xbmc.ISO_639_1, False) or ""
    except (RuntimeError, TypeError, AttributeError):
        code = ""
    code = code.strip().lower()
    return code or "en"


def _lang_dir() -> tuple[str, str]:
    """(absolute strip folder, lang tag for filenames).

    'en' resolves to the en-gb default folder; any other ISO code is tried
    as its own folder; missing -> en-gb fallback."""
    code = _ui_lang_code()
    candidates = [DEFAULT_LANG_DIR] if code == "en" else [code, DEFAULT_LANG_DIR]
    for cand in candidates:
        path = os.path.join(STRIPS_ROOT, cand)
        if os.path.isdir(path):
            return path, cand
    return os.path.join(STRIPS_ROOT, DEFAULT_LANG_DIR), DEFAULT_LANG_DIR


def _lang_labels(strip_dir: str) -> dict[str, list[str]]:
    """months[12] + weekdays[7] localized words for tile filenames; falls
    back to numeric tokens if labels.json is missing/broken (still unique)."""
    try:
        with open(os.path.join(strip_dir, "labels.json"),
                  "r", encoding="utf-8") as f:
            data = json.load(f)
        months = data["months"]
        weekdays = data["weekdays"]
        if len(months) == 12 and len(weekdays) == 7:
            return {"months": [str(x) for x in months],
                    "weekdays": [str(x) for x in weekdays]}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {"months": [f"m{i:02d}" for i in range(1, 13)],
            "weekdays": [f"w{i}" for i in range(7)]}


def _read_png_rgb(path: str) -> tuple[int, int, bytes] | None:
    """Decode a strict 8-bit RGB, non-interlaced PNG to (w, h, raw_scanlines).

    Only handles the exact format our strips are authored in -- not a
    general decoder. raw_scanlines is the INFLATED stream: h rows each of
    a 1-byte filter tag + w*3 RGB bytes."""
    import struct
    import zlib
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w = h = 0
    idat = bytearray()
    i = 8
    while i + 12 <= len(data):
        ln = struct.unpack(">I", data[i:i + 4])[0]
        tag = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + ln]
        if tag == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            if (depth, ctype, body[12]) != (8, 2, 0):
                return None
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        i += 12 + ln
    if not w or not h:
        return None
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    if len(raw) != h * (1 + w * 3):
        return None
    return w, h, raw


def _write_png_rgb(path: str, w: int, h: int, raw: bytes) -> bool:
    import struct
    import zlib

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    try:
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(png)
        os.replace(tmp, path)
        return True
    except OSError as e:
        log(f"tile PNG write failed ({path}): {e}", xbmc.LOGWARNING)
        return False


def _vstack(paths: list[str], out_path: str) -> bool:
    decoded = []
    for p in paths:
        d = _read_png_rgb(p)
        if d is None:
            log(f"date strip unreadable/!RGB8: {p}", xbmc.LOGWARNING)
            return False
        decoded.append(d)
    width = decoded[0][0]
    if any(w != width for w, _, _ in decoded):
        log("date strips differ in width; cannot stack", xbmc.LOGWARNING)
        return False
    total_h = sum(h for _, h, _ in decoded)
    combined = bytearray()
    for _, _, raw in decoded:
        combined += raw
    return _write_png_rgb(out_path, width, total_h, bytes(combined))


def _safe_token(text: str) -> str:
    """Filesystem-safe token from a (possibly non-ASCII) label word."""
    return "".join(c if c.isalnum() else "_" for c in text) or "x"


def day_tile(day_key: str) -> str:
    """Path to a cached calendar date-card PNG for 'YYYY-MM-DD' ('' on error).

    Composited once per date from the current language's month/day/weekday
    strips; named by localized content for a cache-clean, collision-free
    path (tile-<lang>-<MON>-<DD>-<WKD>.png)."""
    parts = day_key.split("-")
    if len(parts) != 3:
        return ""
    try:
        y, m, d = (int(p) for p in parts)
        from datetime import date as _date
        weekday = int(_date(y, m, d).strftime("%w"))   # 0=Sun..6=Sat
    except (ValueError, TypeError):
        return ""
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return ""

    strip_dir, lang_tag = _lang_dir()
    labels = _lang_labels(strip_dir)
    mon_word = _safe_token(labels["months"][m - 1])
    wkd_word = _safe_token(labels["weekdays"][weekday])
    name = f"tile-{_safe_token(lang_tag)}-{mon_word}-{d:02d}-{wkd_word}.png"
    out_path = os.path.join(TILES_DIR, name)
    if os.path.exists(out_path):
        return out_path

    strips = [os.path.join(strip_dir, f"month_{m:02d}.png"),
              os.path.join(strip_dir, f"day_{d:02d}.png"),
              os.path.join(strip_dir, f"week_{weekday}.png")]
    try:
        os.makedirs(TILES_DIR, exist_ok=True)
    except OSError as e:
        log(f"date_tiles dir create failed: {e}", xbmc.LOGWARNING)
        return ""
    return out_path if _vstack(strips, out_path) else ""

