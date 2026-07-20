# KEXP — Kodi Add-on (plugin.audio.kexp) — PROTOTYPE

KEXP 90.3 Seattle in Kodi: the live stream plus the station's public
**two-week archive**, browsable by day, program, or host — with
now-playing metadata **replayed in sync** during archive playback, so
the OSD shows what was actually airing at that moment of the broadcast.

Status: v0.0.1, quick-and-dirty prototype, diagnostic (verbose INFO)
logging throughout. Built from plugin.audio.internetradio's validated
architecture. Companion document: `kexp-archive-api.md` — the
reverse-engineered API reference this is built on.

## What it does

* **Listen live** — plays KEXP's public live stream; the background
  service polls the KEXP API for artist/title/album/cover art and the
  current show ("Variety Mix with Morgan"), exactly as the
  internetradio add-on's KEXP station does.
* **Shows by day** — the last 14 days as folders, each listing that
  day's broadcast shows with program artwork.
* **Programs / Hosts** — the same two weeks sliced by program or DJ.
* Selecting a show resolves the archive audio **at play time** (one
  API call), starts playback at the show's beginning, and the service
  replays the playlist metadata synchronized to the playback position.
  Kodi's normal seeking works within a show; metadata follows.

## Settings

* Live now-playing poll interval (default 20 s)
* Archive metadata sync interval (default 10 s)
* Archive bitrate 64/128/256 (default 128; the API treats this as a
  preference and its actual effect is unverified)

## How it works (one paragraph)

Browsing uses KEXP's public v2 API (`/v2/shows/`, cached for 15 min).
Playing an archive show calls the undocumented `get_streaming_url`
resolver, which returns the MP3 file containing the requested moment
plus an exact byte-accurate second offset; the plugin hands Kodi the
file URL via `setResolvedUrl` and the service seeks to the offset once
the player is ready. During playback the service computes the original
broadcast moment from file start + playback position, looks up the play
at that moment in `/v2/plays/`, and pushes it to the player tag —
defended against Kodi's own ICY tag-stomping (see the internetradio
project for that saga).

## Prototype caveats / open items

* `sg-url-next` continuation chaining (play through show boundaries)
  is designed but not implemented — playback ends at the file end.
* No listening history yet (open design question: whether archive
  playback should record history at all, and where).
* The `/v2/plays/` airdate filter name and the `/v2/shows/` ordering
  parameter follow DRF convention but are unverified; the code detects
  and logs both failure modes rather than showing wrong data.
* Whether item properties survive `setResolvedUrl` is unvalidated; a
  session handoff file covers the gap and the log records which
  identification path won.
* Placeholder icon (deliberately not the KEXP logo).

## Etiquette

This uses KEXP's public but undocumented API for personal listening.
Be kind to it: the add-on caches show lists, resolves streams only at
play time, sends an honest User-Agent, and lets the archive server's
listening-session mechanism (which feeds royalty reporting) work
naturally. KEXP is listener-powered — donate: https://www.kexp.org/donate/
