# -*- coding: utf-8 -*-
"""
plugin.audio.kexp -- KEXP live + 2-week archive browser (BETA).

Browse tree:
    Listen live
    Shows by day  -> date folders -> shows
    Programs      -> program folders -> that program's shows
    DJs           -> DJ folders -> that DJ's shows

Architecture notes (vs plugin.audio.internetradio, the parent design):

  * Archive playback resolves at PLAY time via setResolvedUrl -- the
    first deliberate departure from the family's no-resolve-handler
    rule. Archive URLs are stable, but each one costs a
    get_streaming_url API call; resolving while listing would burn
    6-8 calls just to draw a folder. One call, exactly when needed.
  * The resolved item carries kexp_mode=archive plus the playback
    context; the resolver also writes archive_session.json as a
    belt-and-braces handoff to the service (see service.py).
  * Live playback keeps the family architecture exactly: the stream URL
    is the item path, tagged kexp_mode=live.
"""
from __future__ import annotations

import os
import sys
import time
import urllib.parse
from datetime import datetime
from typing import Any

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

sys.path.insert(0, os.path.join(
    xbmcaddon.Addon().getAddonInfo("path"), "resources", "lib"))
import kexpdata  # noqa: E402

ADDON = xbmcaddon.Addon()
ADDON_ID: str = ADDON.getAddonInfo("id")
HANDLE: int = int(sys.argv[1])
BASE_URL: str = sys.argv[0]

ITEM_PROP_MODE = "kexp_mode"          # "live" | "archive"

_T0 = time.time()


def trace(msg: str) -> None:
    # Routine browse/router tracing -- LOGDEBUG for beta so a normal INFO
    # log isn't flooded while browsing. Enable Kodi debug logging to see.
    xbmc.log("[%s/plugin] (+%6.2fs h=%s) %s"
             % (ADDON_ID, time.time() - _T0, HANDLE, msg), xbmc.LOGDEBUG)


def build_url(**kwargs: str) -> str:
    return BASE_URL + "?" + urllib.parse.urlencode(kwargs)


def notify(message: str, error: bool = False) -> None:
    icon = xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO
    xbmcgui.Dialog().notification("KEXP", message, icon, 4000)


def bitrate_setting() -> str:
    try:
        return ADDON.getSettingString("bitrate") or "128"
    except (RuntimeError, TypeError):
        return "128"


# --- listings ---------------------------------------------------------------

def list_root() -> None:
    xbmcplugin.setPluginCategory(HANDLE, "KEXP")
    trace("root")

    live = xbmcgui.ListItem(label="Listen live")
    live.getMusicInfoTag().setTitle("KEXP live")
    live.setProperty("IsPlayable", "true")
    live.setProperty(ITEM_PROP_MODE, "live")
    xbmcplugin.addDirectoryItem(
        HANDLE, kexpdata.LIVE_STREAM_URL, live, isFolder=False)

    for label, action in (("Shows by day", "days"),
                          ("Programs", "programs"),
                          ("DJs", "hosts")):
        li = xbmcgui.ListItem(label=label)
        xbmcplugin.addDirectoryItem(
            HANDLE, build_url(action=action), li, isFolder=True)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def _parse_day_key(day_key: str) -> datetime | None:
    """Parse our own 'YYYY-MM-DD' keys WITHOUT datetime.strptime.

    strptime lazily imports the _strptime module on first use, and Kodi's
    per-invocation interpreter lifecycle can leave that reference dead on
    later invocations (datetime.strptime becomes None). Since we generate
    these keys ourselves, a manual split is both bug-proof and cheaper."""
    try:
        y, m, d = (int(part) for part in day_key.split("-"))
        return datetime(y, m, d)
    except (ValueError, TypeError):
        return None


def _day_label(day_key: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    if day_key == today:
        return "Today"
    dt = _parse_day_key(day_key)
    if dt is None:
        return day_key
    yesterday = datetime.fromtimestamp(time.time() - 86400).strftime("%Y-%m-%d")
    if day_key == yesterday:
        return "Yesterday"
    return dt.strftime("%a %b %d")


def list_days() -> None:
    xbmcplugin.setPluginCategory(HANDLE, "Shows by day")
    xbmcplugin.setContent(HANDLE, "albums")   # unlock the date-tile grid
    shows = kexpdata.fetch_shows()
    days = kexpdata.group_by_day(shows)
    trace("days: %d day(s), %d show(s) total" % (len(days), len(shows)))
    if not days:
        notify("No shows returned from the KEXP API (see log)", error=True)
    for day_key, day_shows in days:
        label = "%s  [COLOR gray](%d)[/COLOR]" % (
            _day_label(day_key), len(day_shows))
        li = xbmcgui.ListItem(label=label)
        # In setContent("albums") views SiLVO draws the caption from the
        # music info tag, not the list-item label, so set both.
        tag = li.getMusicInfoTag()
        tag.setTitle(_day_label(day_key))
        tag.setAlbum("%d shows" % len(day_shows))
        tile = kexpdata.day_tile(day_key)
        if tile:
            li.setArt({"thumb": tile, "icon": tile})
        xbmcplugin.addDirectoryItem(
            HANDLE, build_url(action="day", date=day_key), li, isFolder=True)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def show_listitem(show: dict[str, Any],
                  context: str = "day") -> xbmcgui.ListItem:
    """Build a show row. `context` trims the label to what the folder
    doesn't already tell the user:
        "day"     -> "HH:MM  Program with DJ"  (date is the folder)
        "program" -> "Sat Jul 18  HH:MM  with DJ"  (program is the folder)
        "dj"      -> "Sat Jul 18  HH:MM  Program"   (DJ is the folder)
    The info-tag title always keeps the full "Program with DJ" form: the
    player screen has no folder context to lean on."""
    full_label = kexpdata.show_label(show)
    local = datetime.fromtimestamp(show["start_epoch"]).astimezone()
    program: str = str(show.get("program_name") or "")
    hosts: Any = show.get("host_names")
    host_str: str = ", ".join(hosts) if isinstance(hosts, list) else ""

    if context == "day":
        stamp = local.strftime("%H:%M")
        text = full_label
    elif context == "program":
        stamp = local.strftime("%a %b %d  %H:%M")
        text = f"with {host_str}" if host_str else full_label
    elif context == "dj":
        stamp = local.strftime("%a %b %d  %H:%M")
        text = program or full_label
    else:
        stamp = local.strftime("%a %b %d  %H:%M")
        text = full_label

    li = xbmcgui.ListItem(label=f"{stamp}  {text}")
    tag = li.getMusicInfoTag()
    tag.setTitle(full_label)
    tag.setArtist("KEXP")
    art: str = str(show.get("program_image_uri")
                   or show.get("image_uri") or "")
    if art:
        li.setArt({"thumb": art, "icon": art})
    tagline: str = str(show.get("tagline") or "")
    if tagline:
        tag.setComment(tagline)
    li.setProperty("IsPlayable", "true")
    return li


def _add_show_items(shows: list[dict[str, Any]], context: str) -> None:
    for s in shows:
        # start_time as-is (offset-aware ISO) is what the resolver wants.
        url = build_url(
            action="play",
            start=str(s.get("start_time", "")),
            label=kexpdata.show_label(s),
            art=str(s.get("program_image_uri") or s.get("image_uri") or ""),
            start_epoch=str(s["start_epoch"]))
        xbmcplugin.addDirectoryItem(
            HANDLE, url, show_listitem(s, context), isFolder=False)


def list_day(day_key: str) -> None:
    xbmcplugin.setPluginCategory(HANDLE, _day_label(day_key))
    xbmcplugin.setContent(HANDLE, "albums")   # unlock icon/wall views
    shows = [s for k, ss in kexpdata.group_by_day(kexpdata.fetch_shows())
             if k == day_key for s in ss]
    # A single day reads naturally morning -> night, so within the day we
    # sort ASCENDING by start time. (Program/DJ "episode" lists stay
    # newest-first, matching KEXP's own per-show ordering.)
    shows.sort(key=lambda s: s["start_epoch"])
    trace("day %s: %d show(s)" % (day_key, len(shows)))
    _add_show_items(shows, context="day")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def list_programs() -> None:
    xbmcplugin.setPluginCategory(HANDLE, "Programs")
    xbmcplugin.setContent(HANDLE, "albums")
    reps = kexpdata.unique_programs(kexpdata.fetch_shows())
    trace("programs: %d" % len(reps))
    for s in reps:
        li = xbmcgui.ListItem(label=str(s.get("program_name") or "?"))
        art = str(s.get("program_image_uri") or "")
        if art:
            li.setArt({"thumb": art, "icon": art})
        xbmcplugin.addDirectoryItem(
            HANDLE, build_url(action="program", id=str(s.get("program"))),
            li, isFolder=True)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def list_program(program_id: str) -> None:
    shows = [s for s in kexpdata.fetch_shows()
             if str(s.get("program")) == program_id]
    name = str(shows[0].get("program_name")) if shows else "Program"
    xbmcplugin.setPluginCategory(HANDLE, name)
    xbmcplugin.setContent(HANDLE, "albums")
    trace("program %s: %d show(s)" % (program_id, len(shows)))
    _add_show_items(shows, context="program")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def list_hosts() -> None:
    xbmcplugin.setPluginCategory(HANDLE, "DJs")
    xbmcplugin.setContent(HANDLE, "albums")   # unlock the photo-tile grid
    shows = kexpdata.fetch_shows()
    hosts = kexpdata.unique_hosts(shows)
    trace("djs: %d" % len(hosts))
    for hid, hname in hosts:
        dj_shows = [s for s in shows
                    if isinstance(s.get("hosts"), list) and hid in s["hosts"]]
        # A DJ may host more than one program across two weeks; label the
        # tile with their MOST FREQUENT program (newest breaks ties, since
        # dj_shows is newest-first).
        freq: dict[str, int] = {}
        for s in dj_shows:
            pname = str(s.get("program_name") or "")
            if pname:
                freq[pname] = freq.get(pname, 0) + 1
        program = max(freq, key=lambda p: freq[p]) if freq else ""
        # Photo tile: newest show image for this DJ.
        art = next((str(s.get("image_uri") or "") for s in dj_shows
                    if s.get("image_uri")), "")

        li = xbmcgui.ListItem(label=hname)
        tag = li.getMusicInfoTag()
        tag.setTitle(hname)
        if program:
            # Secondary line in grid/wall views (the web page's third
            # element). setAlbum reliably surfaces as label2 in music
            # 'albums' content, more so than genre/comment across skins.
            tag.setAlbum(program)
            tag.setArtist(program)
        # Bare photo as thumb (Kodi decodes JPEG/PNG; it has no SVG
        # decoder for runtime art, so composited name-cards are out). The
        # name is the item label and the info-tag title; the program is
        # label2 via setAlbum -- a text-showing wall view renders both.
        if art:
            li.setArt({"thumb": art, "icon": art})
        xbmcplugin.addDirectoryItem(
            HANDLE, build_url(action="host", id=str(hid)), li, isFolder=True)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def list_host(host_id: str) -> None:
    try:
        hid = int(host_id)
    except ValueError:
        hid = -1
    shows = [s for s in kexpdata.fetch_shows()
             if isinstance(s.get("hosts"), list) and hid in s["hosts"]]
    name = "DJ"
    if shows:
        ids: Any = shows[0].get("hosts")
        names: Any = shows[0].get("host_names")
        if isinstance(ids, list) and isinstance(names, list) and hid in ids:
            name = str(names[ids.index(hid)])
    xbmcplugin.setPluginCategory(HANDLE, name)
    xbmcplugin.setContent(HANDLE, "albums")
    trace("dj %s: %d show(s)" % (host_id, len(shows)))
    _add_show_items(shows, context="dj")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


# --- archive playback (resolve at play time) --------------------------------

def play_archive(params: dict[str, str]) -> None:
    start: str = params.get("start", "")
    label: str = params.get("label", "KEXP archive")
    art: str = params.get("art", "")
    if not start:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return

    # Bookmark check happens BEFORE resolving: the dialog only needs the
    # saved position/label, not the stream itself. Deferred clearing (on
    # "play from beginning") until after a successful resolve below, so a
    # resolver failure can't silently discard a bookmark for nothing.
    bookmark = kexpdata.read_bookmark(start)
    resume_requested = False
    if bookmark is not None:
        resume_requested = xbmcgui.Dialog().yesno(
            "KEXP", "Resume at %s?" % kexpdata.resume_label(bookmark),
            yeslabel="Resume", nolabel="Play from beginning")

    info = kexpdata.resolve_stream(start, bitrate_setting())
    if info is None:
        notify("Could not resolve the archive stream (see log)", error=True)
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return

    offset: int = 0
    try:
        offset = int(info.get("sg-offset") or 0)
    except (TypeError, ValueError):
        pass

    # Seek target: the show's natural top-of-show offset by default -- same
    # as a fresh, never-played show -- overridden by the saved position
    # only when the user chose to resume.
    seek_to: float = float(offset)
    if bookmark is not None:
        if resume_requested:
            seek_to = float(bookmark.get("position", offset))
        else:
            kexpdata.clear_bookmark(start)

    # Broadcast moment of the FILE's first byte: what the service needs
    # to map playback position -> airdate for synced metadata.
    file_start_epoch: float = 0.0
    try:
        file_start_epoch = float(params.get("start_epoch", "")) - offset
    except ValueError:
        dt = kexpdata.parse_iso(start)
        if dt is not None:
            file_start_epoch = dt.timestamp() - offset

    kexpdata.write_session({
        "sg_url": str(info.get("sg-url") or ""),
        "sg_url_next": str(info.get("sg-url-next") or ""),
        "offset": offset,
        "seek_to": seek_to,
        "requested": start,
        "show_start": start,
        "file_start_epoch": file_start_epoch,
        "show_label": label,
        "art": art,
    })

    li = xbmcgui.ListItem(path=str(info["sg-url"]))
    tag = li.getMusicInfoTag()
    tag.setTitle(label)
    tag.setArtist("KEXP archive")
    if art:
        li.setArt({"thumb": art, "icon": art})
    li.setProperty(ITEM_PROP_MODE, "archive")
    # Skip Kodi's content probe: we know it's MP3, and a probe would
    # spend an extra AIS listening session for nothing.
    li.setMimeType("audio/mpeg")
    #li.setContentLookup(False)
    trace("resolved archive play: %s (offset %ds)" % (label, offset))
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


# --- router -----------------------------------------------------------------

def router(paramstring: str) -> None:
    trace("router: argv=%r" % (sys.argv,))
    params: dict[str, str] = dict(urllib.parse.parse_qsl(paramstring))
    action: str | None = params.get("action")

    if action is None:
        list_root()
    elif action == "days":
        list_days()
    elif action == "day":
        list_day(params.get("date", ""))
    elif action == "programs":
        list_programs()
    elif action == "program":
        list_program(params.get("id", ""))
    elif action == "hosts":
        list_hosts()
    elif action == "host":
        list_host(params.get("id", ""))
    elif action == "play":
        play_archive(params)
    else:
        xbmc.log("%s: unknown action %s" % (ADDON_ID, action), xbmc.LOGWARNING)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


if __name__ == "__main__":
    router(sys.argv[2][1:])
