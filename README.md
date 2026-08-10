# KEXP — Kodi Add-on (plugin.audio.kexp) — BETA

KEXP 90.3 Seattle in Kodi: the live stream plus the station's public
**two-week archive**, browsable by day, program, or DJ — with now-playing
metadata **replayed in sync** during archive playback, so the OSD shows
what was actually airing at that point in the broadcast.

Status: v0.1.0 beta. Archive playback is validated end-to-end on real
hardware (Raspberry Pi 4 / LibreELEC / Kodi 21 Omega). Built from
plugin.audio.internetradio's architecture. Companion document:
`kexp-archive-api.md` — the reverse-engineered API reference this is
built on.

## What it does

* **Listen live** — plays KEXP's public live stream; the background
  service polls the KEXP API for artist/title/album/cover art and the
  current show ("Variety Mix with Morgan").
* **Shows by day** — the last 14 days as a grid of date-card tiles,
  each opening to that day's shows (earliest first) with program art.
* **Programs / DJs** — the same two weeks sliced by program or DJ. Both
  grids are alphabetical; a program's or DJ's episode list is most-recent
  first, matching KEXP's own ordering.
* Selecting a show resolves the archive audio **at play time**, starts
  playback at the chosen point in the show, and the service replays the
  playlist metadata synchronized to the playback position.

## How archive playback works

Browsing uses KEXP's public v2 API (`/v2/shows/`, cached 15 min). Playing
an archive show calls the undocumented `get_streaming_url` resolver, which
returns the MP3 file containing the requested moment plus a second-accurate
offset into that file. The plugin hands Kodi the file URL via
`setResolvedUrl`; the service seeks to the offset once the player confirms
it has landed, then every few seconds computes the original broadcast
moment from file-start + playback-position, looks up the play at that
moment in `/v2/plays/`, and pushes it to the now-playing tag.

The audio files are large (often a multi-hour block) and fully seekable,
so scrubbing within a show works; the metadata follows the playback
position.

## Settings

* Live now-playing poll interval (default 20 s)
* Archive metadata sync interval (default 10 s)
* Archive bitrate 64/128/256 (default 128; the API treats this as a
  preference and its actual effect is unverified)

## Companion Kodi setting

On a dedicated audio box, raise Kodi's fullscreen-music blackout timeout
or the screen goes black ~10 s into playback:

```xml
<advancedsettings>
  <songinfoduration>86400</songinfoduration>
</advancedsettings>
```

## Known limitations (beta)

* **Deep-seek lag (investigated, closed — not fixable at the addon
  level).** Seeking or resuming far into an archive file is slow, and
  the delay scales with how far the target is from the current playback
  position (roughly 9 s for a ~10-minute jump, ~50 s for a ~47-minute
  jump, measured on-device). Root-caused via `kodi.log` capture with full
  curl tracing: KEXP's archive files are genuinely CBR (confirmed by
  KEXP), and Kodi/ffmpeg *does* issue correct HTTP `Range` requests — both
  of the original suspect theories (missing Range support, missing VBR
  seek index) were ruled out. The actual cause is that once a stream is
  already open, `CDVDDemuxFFmpeg::SeekTime` reaches the target by
  **decoding and discarding forward** over the network rather than
  jumping to the target byte offset with a fresh Range request — that's
  what makes the lag proportional to seek distance. A workaround was
  attempted (opening the stream pre-positioned via a `|Range=bytes=N-`
  URL modifier, using the known CBR bitrate to compute the target byte
  offset directly). It worked at the HTTP layer (server returned `206
  Partial Content` immediately) but broke Kodi's `CFileCache`, which
  read the partial response's `Content-Length` as if it were the full
  file size and fell into a non-converging backward crawl — worse than
  the original stall. This confirmed the bottleneck is inside Kodi's
  own player/demuxer internals, not something `plugin.audio.kexp` can
  address. **No further work is planned here**; given how rarely a deep
  resume is used on this appliance, the lag is accepted as-is.
* **Metadata timing.** KEXP timestamps each play when the DJ logs it,
  not at the instant it airs, so the displayed song can run a little
  ahead of the audio — the same behavior as KEXP's own players. The
  plugin's own seek/position error is corrected; this residual is in
  KEXP's data and is typically well under a minute.
* **No play-through past a show file.** Playback ends at the end of the
  resolved file; automatic continuation into the next show
  (`sg-url-next`) is designed but not yet implemented.
* **No archive listening history.** (Live/archive history is a possible
  later feature.)
* **No manual seek bar in some skins.** The file is fully seekable, but
  a given skin's music OSD may not draw a scrubber; playback still
  starts at the correct point automatically.
* **Placeholder icon** (deliberately not the KEXP logo).
* **Date/DJ tiles** render in a wall/grid view; if a folder opens as a
  plain list, pick a wall view once via Choose View (Kodi remembers it
  per path).

## Internationalization

Date-card tiles are composited on-device from pre-rendered PNG strips
under `resources/media/datestrips/<lang>/` (see the README there). `en-gb`
is the shipped default and fallback; add a language by dropping in a
sibling folder keyed by ISO-639-1 code.

## Etiquette

This uses KEXP's public but undocumented API for personal listening. The
add-on caches show lists, resolves streams only at play time, sends an
honest User-Agent, and lets the archive server's listening-session
mechanism (which feeds royalty reporting) work naturally. KEXP is
listener-powered — donate: https://www.kexp.org/donate/
