Date-tile strips (Shows-by-day grid)
====================================

The add-on builds each calendar date-card thumbnail on the device by
vertically stacking three pre-rendered PNG strips. Kodi's Python has no
image/font library, so the text must be rendered ahead of time into
these strips; the device only copies pixels.

Layout per language folder (folder name = ISO-639-1 code; en-gb is the
default that "en" resolves to, and the universal fallback):

  <lang>/month_01.png .. month_12.png   (12 month bands)
  <lang>/day_01.png   .. day_31.png     (31 day numerals)
  <lang>/week_0.png   .. week_6.png     (7 weekday bands; 0=Sunday..6=Saturday)
  <lang>/labels.json                    (localized words, for tile filenames)

Strip requirements (all must match or the tile silently blanks and logs
a warning):
  * PNG, 8-bit RGB, NO alpha channel, NON-interlaced.
  * All strips the SAME width (the shipped set is 400px).
  * Heights are free; the composite is their sum (shipped: 96+208+96=400).

labels.json:
  { "months":   [12 words, Jan..Dec],
    "weekdays": [7 words, Sun..Sat] }
  These are used only to name the cached composite
  (tile-<lang>-<MONTH>-<DD>-<WEEKDAY>.png), keeping each language's tiles
  on distinct, cache-friendly paths.

To add a language: copy en-gb/ to a new ISO-code folder (e.g. de/),
replace the 50 strip PNGs with localized art, and translate labels.json.
It is picked up automatically when Kodi's UI language matches.
