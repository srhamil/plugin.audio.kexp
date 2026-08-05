# -*- coding: utf-8 -*-
"""
Background service for plugin.audio.kexp (BETA).

Verbose INFO instrumentation on purpose: this is the add-on's first
device shakedown. Drop the noisy lines to LOGDEBUG once stable.

Two handlers, gated by the kexp_mode item property (with a fallback of
matching the playing file against known URLs, since property survival
through setResolvedUrl is unvalidated -- the log records which
identification path won):

  LiveHandler    - port of internetradio's field-validated KexpHandler:
                   polls /v2/plays/?limit=1, pushes artist/title/album/
                   art + show label.
  ArchiveHandler - the new trick: replayed DJ metadata in sync with
                   replayed audio. Broadcast moment = file_start_epoch +
                   player.getTime(); look up the play at that moment in
                   /v2/plays/ and push it. Also performs the one-shot
                   seekTime(sg-offset) for jump-into-show starts.

Both handlers inherit TAG DEFENSE (internetradio's hard-won lesson):
Kodi's own ICY handling stomps any updateInfoTag push when the stream's
StreamTitle changes -- title becomes the raw string, album is cleared.
Every 2s tick compares the live InfoLabels against the last-pushed state
and re-pushes on drift. The live AAC stream definitely carries ICY;
whether the archive MP3 files do is unknown -- the defense costs nothing
and the diagnostic log will tell us ("tag stomped" lines).

No listening history yet: whether archive playback should
record history (and where) is an open design question.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

import xbmc
import xbmcaddon
import xbmcgui
from xbmcgui import ListItem
from xbmc import InfoTagMusic

ADDON = xbmcaddon.Addon()
ADDON_ID: str = ADDON.getAddonInfo("id")
sys.path.insert(0, os.path.join(
    ADDON.getAddonInfo("path"), "resources", "lib"))
import kexpdata  # noqa: E402

ITEM_PROP_MODE = "kexp_mode"
POLL_SECONDS = 2.0
API_V2 = kexpdata.API_V2


def log(msg: str, level: int = xbmc.LOGINFO) -> None:
    xbmc.log(f"[{ADDON_ID}/service] {msg}", level)


def setting_int(sid: str, default: int) -> int:
    try:
        return ADDON.getSettingInt(sid) or default
    except (RuntimeError, TypeError):
        return default


def push_tag(artist: str, title: str, album: str = "", art: str = "") -> None:
    """Update the now-playing tag. Item MUST come from getPlayingItem()
    (bare ListItem: text merges, artwork silently does not). Album is set
    unconditionally so empty CLEARS it and defensive re-pushes restore
    exactly what we pushed."""
    player = xbmc.Player()
    li:ListItem
    try:
        li:ListItem  = player.getPlayingItem()
    except RuntimeError:
        log("  push_tag: nothing playing, skipped", xbmc.LOGDEBUG)
        return
    tag:InfoTagMusic = li.getMusicInfoTag()
    tag.setTitle(title)
    if artist:
        tag.setArtist(artist)
    tag.setAlbum(album)
    if art:
        li.setArt({"thumb": art, "icon": art})
    try:
        player.updateInfoTag(li)
        log(f"  tag pushed: artist={artist!r} title={title!r} album={album!r}"
            f" art={'yes' if art else 'no'}", xbmc.LOGDEBUG)
    except RuntimeError as e:
        log(f"  updateInfoTag failed: {e}", xbmc.LOGWARNING)


class BaseHandler:
    """Per-playback-session handler with tag defense built in."""

    def __init__(self) -> None:
        self.pushed_artist: str = ""
        self.pushed_title: str = ""
        self.pushed_album: str = ""
        self.pushed_art: str = ""

    def poll(self) -> None:
        self._defend_tag()
        self.tick()

    def tick(self) -> None:
        raise NotImplementedError

    def _push(self, artist: str, title: str, album: str = "",
              art: str = "") -> None:
        self.pushed_artist = artist
        self.pushed_title = title
        self.pushed_album = album
        self.pushed_art = art
        push_tag(artist, title, album=album, art=art)

    def _defend_tag(self) -> None:
        if not self.pushed_title:
            return
        label_title: str = xbmc.getInfoLabel("MusicPlayer.Title")
        label_album: str = xbmc.getInfoLabel("MusicPlayer.Album")
        if not label_title:
            return
        if (label_title != self.pushed_title
                or label_album != self.pushed_album):
            log(f"tag stomped (title={label_title!r} album={label_album!r})"
                f" -> re-pushing {self.pushed_title!r}",xbmc.LOGDEBUG)
            push_tag(self.pushed_artist, self.pushed_title,
                     album=self.pushed_album, art=self.pushed_art)

    # shared API helpers ----------------------------------------------------

    @staticmethod
    def _get(url: str) -> dict[str, Any] | None:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": kexpdata.USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data: Any = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception as e:
            log(f"KEXP request failed ({url}): {e}", xbmc.LOGWARNING)
            return None

    def _first_play(self, url: str) -> dict[str, Any] | None:
        data = self._get(url)
        if not data:
            return None
        results: Any = data.get("results")
        if isinstance(results, list) and results \
                and isinstance(results[0], dict):
            return results[0]
        log("plays response had no results", xbmc.LOGDEBUG)
        return None


class LiveHandler(BaseHandler):
    """internetradio's field-validated KexpHandler, minus history."""

    def __init__(self) -> None:
        super().__init__()
        self.next_poll: float = 0.0
        self.last_play_key: str = ""
        self.show_cache: dict[int, str] = {}

    def tick(self) -> None:
        now: float = time.monotonic()
        if now < self.next_poll:
            return
        self.next_poll = now + max(float(setting_int("live_poll", 20)), 5.0)
        play = self._first_play(f"{API_V2}/plays/?limit=1")
        if play is None:
            return
        self._apply_play(play)

    def _apply_play(self, play: dict[str, Any]) -> None:
        play_type: str = str(play.get("play_type", ""))
        show_label: str = self._show_label(play.get("show"))
        if play_type == "trackplay":
            artist: str = str(play.get("artist") or "")
            title: str = str(play.get("song") or "")
            album: str = str(play.get("album") or "")
            art: str = str(play.get("image_uri")
                           or play.get("thumbnail_uri") or "")
            key = f"track|{artist}|{title}"
            if key == self.last_play_key or not title:
                return
            self.last_play_key = key
            log(f"live trackplay: {artist!r} - {title!r} ({album!r})"
                f" show={show_label!r}",xbmc.LOGDEBUG)
            self._push(artist, title, album=album, art=art)
        else:
            key = f"break|{show_label}"
            if key == self.last_play_key:
                return
            self.last_play_key = key
            log(f"live {play_type or 'airbreak'}: showing {show_label!r}",xbmc.LOGDEBUG)
            self._push(show_label or "KEXP", "Air break")

    def _show_label(self, show_id: Any) -> str:
        if not isinstance(show_id, int):
            return ""
        if show_id in self.show_cache:
            return self.show_cache[show_id]
        data = self._get(f"{API_V2}/shows/{show_id}/")
        label = ""
        if data:
            program: str = str(data.get("program_name") or "")
            hosts: Any = data.get("host_names")
            host_str: str = ", ".join(hosts) if isinstance(hosts, list) else ""
            label = f"{program} with {host_str}" if program and host_str \
                else (program or host_str)
        self.show_cache[show_id] = label
        return label


class ArchiveHandler(BaseHandler):
    """Synced metadata replay for archive playback.

    session: archive_session.json written by the plugin's resolver --
    sg_url, offset (seconds into the file for the requested moment),
    file_start_epoch (broadcast time of the file's first byte),
    show_label, art.
    """

    SEEK_GIVE_UP_TICKS = 20   # ~40s of trying before we play from 0:00
    SEEK_CONFIRM_TOLERANCE = 15.0   # getTime() within this of offset = landed

    def __init__(self, session: dict[str, Any]) -> None:
        super().__init__()
        self.session = session
        self.offset: float = float(session.get("offset") or 0)
        # seek_to defaults to the natural top-of-show offset (same as a
        # fresh show) but the plugin overrides it with a saved bookmark
        # position when the user chose to resume -- see default.py.
        self.seek_to: float = float(session.get("seek_to", self.offset))
        self.file_start: float = float(session.get("file_start_epoch") or 0)
        self.show_label: str = str(session.get("show_label") or "KEXP")
        self.show_art: str = str(session.get("art") or "")
        # Bookmark key -- the show's own 'start_time' string. Falls back to
        # 'requested' for forward compat with any stale session file
        # written before 'show_start' existed.
        self.show_start: str = str(
            session.get("show_start") or session.get("requested") or "")
        # Seek has three states: not yet requested, requested (waiting for
        # getTime() to actually reflect it), confirmed. We only trust the
        # clock for metadata sync once CONFIRMED -- declaring the seek done
        # the instant seekTime() is called (before getTime() catches up)
        # was anchoring every later sync ~30s early.
        self.seek_requested: bool = self.seek_to <= 0
        self.seek_confirmed: bool = self.seek_to <= 0
        self.seek_ticks: int = 0
        self.next_sync: float = 0.0
        self.next_bookmark: float = 0.0
        self.last_play_key: str = ""
        # Push the show identity immediately so the OSD is sensible even
        # before the first plays lookup lands.
        self._push(self.show_label, "KEXP archive", art=self.show_art)

    def tick(self) -> None:
        player = xbmc.Player()
        if not self.seek_confirmed:
            self._drive_seek(player)
            return                      # sync metadata only after the seek
        now = time.monotonic()
        if now >= self.next_bookmark:
            self.next_bookmark = now + max(
                float(setting_int("archive_poll", 10)), 5.0)
            self._save_bookmark(player)
        if self.file_start <= 0:
            return                      # can't map position -> airdate
        if now < self.next_sync:
            return
        self.next_sync = now + max(float(setting_int("archive_poll", 10)), 5.0)
        try:
            position: float = player.getTime()
        except RuntimeError:
            return
        moment: float = self.file_start + position
        self._sync_metadata(moment)

    def _save_bookmark(self, player: xbmc.Player) -> None:
        if not self.show_start:
            return
        try:
            position: float = player.getTime()
            total: float = player.getTotalTime()
        except RuntimeError:
            return
        kexpdata.write_bookmark(self.show_start, position, total,
                                 self.file_start, self.show_label,
                                 self.show_art)

    def flush_bookmark(self) -> None:
        """Force-save the resume point immediately (pause/stop/end),
        bypassing the tick throttle -- catches the last few seconds a
        periodic save might have missed. Safe to call even if the player
        has already torn down (RuntimeError -> last periodic save stands)."""
        self._save_bookmark(xbmc.Player())

    def _drive_seek(self, player: xbmc.Player) -> None:
        """Request the seek once the file is seekable, then wait for
        getTime() to actually reflect it before declaring it confirmed.

        seekTime() inside onAVStarted is flaky, so the seek is driven from
        the service tick. Critically, getTime() does NOT jump the instant
        seekTime() returns -- it lags a tick or two. Syncing during that
        lag reads a stale position and anchors all later metadata early,
        which showed up as a fixed ~30s offset. So we confirm the landing.
        """
        self.seek_ticks += 1
        try:
            total: float = player.getTotalTime()
        except RuntimeError:
            total = 0.0

        if not self.seek_requested:
            if total > 0:
                log(f"seeking to {self.seek_to:.0f}s"
                    f" (file duration {total:.0f}s)",xbmc.LOGDEBUG)
                player.seekTime(self.seek_to)
                self.seek_requested = True
            elif self.seek_ticks >= self.SEEK_GIVE_UP_TICKS:
                log("seek: player never became seekable; playing from 0:00",
                    xbmc.LOGDEBUG)
                self.seek_confirmed = True   # give up; sync from 0:00
            return

        # Seek requested -- wait for the reported position to reach it.
        try:
            pos: float = player.getTime()
        except RuntimeError:
            pos = 0.0
        if abs(pos - self.seek_to) <= self.SEEK_CONFIRM_TOLERANCE:
            log(f"seek confirmed: position {pos:.0f}s ~= target"
                f" {self.seek_to:.0f}s",xbmc.LOGDEBUG)
            self.seek_confirmed = True
        elif self.seek_ticks >= self.SEEK_GIVE_UP_TICKS:
            log(f"seek not confirmed after {self.seek_ticks} ticks"
                f" (position {pos:.0f}s vs target {self.seek_to:.0f}s);"
                f" proceeding anyway",xbmc.LOGDEBUG)
            self.seek_confirmed = True

    def _sync_metadata(self, moment: float) -> None:
        iso: str = datetime.fromtimestamp(moment, tz=timezone.utc)\
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        # DRF-conventional airdate filter; support is a shakedown watch
        # item. The sanity check below catches a server that ignores it
        # (which would return the CURRENT live play).
        url = (f"{API_V2}/plays/?"
               + urllib.parse.urlencode(
                   {"limit": 1, "ordering": "-airdate",
                    "airdate_before": iso}))
        play = self._first_play(url)
        if play is None:
            return
        airdate = kexpdata.parse_iso(str(play.get("airdate", "")))
        if airdate is None:
            return
        age: float = moment - airdate.timestamp()
        if age < -30 or age > 3 * 3600:
            log(f"plays airdate filter looks unsupported (asked <= {iso},"
                f" got {play.get('airdate')!r}) -- showing show identity"
                " only",xbmc.LOGDEBUG)
            key = "fallback"
            if key != self.last_play_key:
                self.last_play_key = key
                self._push(self.show_label, "KEXP archive", art=self.show_art)
            return

        play_type: str = str(play.get("play_type", ""))
        if play_type == "trackplay":
            artist: str = str(play.get("artist") or "")
            title: str = str(play.get("song") or "")
            album: str = str(play.get("album") or "")
            art: str = str(play.get("image_uri")
                           or play.get("thumbnail_uri") or "") \
                or self.show_art
            key = f"track|{artist}|{title}"
            if key == self.last_play_key or not title:
                return
            self.last_play_key = key
            log(f"archive sync @ {iso}: {artist!r} - {title!r} ({album!r})"
                f" [{age:.0f}s into it]",xbmc.LOGDEBUG)
            self._push(artist, title, album=album, art=art)
            log(f"KEXP playing {title} from {album} by {artist}")
 
        else:
            key = f"break|{self.show_label}"
            if key == self.last_play_key:
                return
            self.last_play_key = key
            log(f"archive sync @ {iso}: airbreak",xbmc.LOGDEBUG)
            self._push(self.show_label, "Air break", art=self.show_art)
            log(f"KEXP playing {self.show_label} air break")


class RadioPlayer(xbmc.Player):
    def __init__(self) -> None:
        super().__init__()
        self.handler: BaseHandler | None = None

    def onAVStarted(self) -> None:
        log("onAVStarted fired", xbmc.LOGDEBUG)
        mode, how = self._identify()
        if mode == "live":
            self.handler = LiveHandler()
            log(f"  attached LiveHandler (identified via {how})",xbmc.LOGDEBUG)
        elif mode == "archive":
            session = kexpdata.read_session() or {}
            self.handler = ArchiveHandler(session)
            log(f"  attached ArchiveHandler (identified via {how});"
                f" offset={session.get('offset')!r}"
                f" show={session.get('show_label')!r}",xbmc.LOGDEBUG)
        else:
            if self.handler:
                log("  non-KEXP playback started -> detaching handler",xbmc.LOGDEBUG)
            self.handler = None

    def _identify(self) -> tuple[str, str]:
        """(mode, how) -- property first, then playing-file matching."""
        try:
            item = self.getPlayingItem()
            prop: str = item.getProperty(ITEM_PROP_MODE) or ""
        except RuntimeError:
            prop = ""
        if prop in ("live", "archive"):
            return prop, "item property"
        try:
            playing: str = self.getPlayingFile()
        except RuntimeError:
            return "", "n/a"
        if playing.startswith(kexpdata.LIVE_STREAM_URL):
            return "live", "file match"
        session = kexpdata.read_session()
        if session:
            sg_url: str = str(session.get("sg_url") or "")
            # The AIS redirect appends ?listeningSessionID=... -- compare
            # on the session-less prefix / path component.
            if sg_url and playing.split("?")[0] == sg_url.split("?")[0]:
                return "archive", "session file match"
        return "", "no match"

    def onPlayBackPaused(self) -> None:
        log("onPlayBackPaused fired", xbmc.LOGDEBUG)
        if isinstance(self.handler, ArchiveHandler):
            self.handler.flush_bookmark()

    def onPlayBackStopped(self) -> None:
        log("onPlayBackStopped fired -> detaching handler",xbmc.LOGDEBUG)
        if isinstance(self.handler, ArchiveHandler):
            self.handler.flush_bookmark()
        self.handler = None

    def onPlayBackEnded(self) -> None:
        # TODO(design): sg-url-next continuation chaining hooks in here.
        log("onPlayBackEnded fired -> detaching handler",xbmc.LOGDEBUG)
        if isinstance(self.handler, ArchiveHandler):
            # Natural end-of-file: flush_bookmark's position will land
            # inside BOOKMARK_END_GUARD of total, so write_bookmark clears
            # the bookmark rather than saving a useless near-the-end one.
            self.handler.flush_bookmark()
        self.handler = None

    def onPlayBackError(self) -> None:
        log("onPlayBackError fired -> detaching handler", xbmc.LOGWARNING)
        self.handler = None


def run() -> None:
    monitor = xbmc.Monitor()
    player = RadioPlayer()
    kexpdata.ensure_profile()
    log("=== service started (beta) ===")
    log(f"profile: {kexpdata.PROFILE}")

    while not monitor.abortRequested():
        if player.handler is not None and player.isPlaying():
            try:
                player.handler.poll()
            except Exception as e:
                log(f"handler poll error: {e}", xbmc.LOGERROR)
        if monitor.waitForAbort(POLL_SECONDS):
            break

    log("=== service stopped ===")


if __name__ == "__main__":
    run()
