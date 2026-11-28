# Rad Streams

A Dispatcharr plugin that turns internet radio stations into TV-style channels.
Each channel shows rotating photos of the station's city, the track that's
playing, and the city's current weather, with the station's
audio underneath. Theme channels show photos of a subject instead, such as
space or nature, without the city and weather.

- One channel per station, in a channel group called **Rad Streams**.
- Channels render **only while someone is watching** and stop about 10 seconds after the last viewer leaves.
- Photos come from Wikimedia Commons (freely licensed, credited on screen). Scenic shots (skylines, parks, water, mountains) are preferred, and a small town with too few photos is topped up from the surrounding area and its state or province. The Space, Earth, Night Sky and Aurora themes also use the NASA Image Library. Weather and geocoding come from Open-Meteo. None of these needs an API key.

## Install

1. Copy the `rad_streams` folder to `/app/data/plugins/rad_streams/` in your Dispatcharr container.
2. In Dispatcharr, open **Plugins**, reload if needed, and enable **Rad Streams**. Dispatcharr 0.34 or newer
   is required. When you enable it (or install an update that adds one), Dispatcharr lists the access the plugin
   asks for; approve all six:
   - **Run host processes** (`subprocess`): the stream server and FFmpeg.
   - **Listen for network connections** (`network_listener`): the server on 127.0.0.1 (Dispatcharr asks for a restart the first time).
   - **Access external network services** (`outbound_network`): radio streams, photos, weather and Validate.
   - **Write outside plugin storage** (`filesystem_write`): its files in `/data/rad_streams/` and the logo in `/data/logos/`.
   - **Run a persistent background service** (`persistent_service`): relaunches the server after Dispatcharr restarts.
   - **Run background tasks** (`background_tasks`, new in 0.6.0): Sync and Validate run in the background with a progress bar.
3. Add your stations to the **Stations** table, one row each (press **Add row**), then press **Save Settings**:

   | Name | Stream URL | City or Theme | Channel # |
   | --- | --- | --- | --- |
   | KEXP | https://kexp.streamguys1.com/kexp160.aac | Seattle, WA | 501 |
   | SomaFM Groove Salad | https://ice1.somafm.com/groovesalad-128-mp3 | San Francisco, CA | |

   Name, Stream URL and City or Theme are required; Channel # is optional (blank picks one from 1000 up).

   Use `City, Region` or `City, Country` when a name is ambiguous: US states, Canadian provinces and
   Australian states work by name or abbreviation (`Portland, ME`, `Vancouver, BC`, `Perth, WA, Australia`, `Paris, France`).
   For a channel that isn't about a city, put `Theme:` and a subject in the City column. A theme
   channel shows photos of that subject; its panel shows just the station and track, with no place,
   theme name or weather, for example `Theme: Nature` or `Theme: Space`.

   Built-in themes have search terms picked for good photos: Space, Earth, Nature, Ocean, Mountains,
   Forest, Waterfalls, Desert, Aurora, Sunsets, Beaches, Lakes, Wildlife and Night Sky. Any other word
   (`Theme: Trains`, `Theme: Lighthouses`) is searched on Wikimedia Commons, trying a plural in the singular too
   (`Lighthouses` also finds `Lighthouse`). Space, Earth, Night Sky and Aurora also pull public-domain photos
   from the NASA Image Library. No photographer fills more than a few spots in a set.
   Tick **Skip** to take a station off the air without deleting its row. Icecast/Shoutcast streams (including old Shoutcast v1 servers), `.pls`/`.m3u` playlist links and
   HLS (`.m3u8`) streams all work. HLS track titles come from the playlist or the segments' ID3 tags;
   encrypted and fMP4 HLS streams aren't supported.
4. Press **Validate** to check every stream, its track titles, the city and available photos.
5. Press **Sync Stations**. Channels appear in the Rad Streams group, and the stream server starts.

Sync and Validate run as Dispatcharr background tasks: the button returns straight away, a progress bar
shows how far along they are, and a pop-up gives the result. Each run saves a full report (see Actions).
Sync first checks every cell of the table. If any is wrong it changes nothing and names the row and column,
for example `row 3 (KEXP), Stream URL: stream URL must start with http:// or https://`. Dispatcharr itself
checks each cell's type when you press Save Settings; the rest is checked when Sync or Validate starts,
because Dispatcharr gives plugins no hook into Save.

After updating the plugin files, press **Sync Stations** once: it restarts the stream server so the new code takes effect.

Sync again after any edit. It creates, updates and removes channels to match the table. An empty table
never deletes channels (that is usually an accident); use **Remove All Channels**, or tick Skip on every row.

### Optional columns: Keep ID and Logo URL

- **Keep ID** keeps a station's channel when you rename it. A station's id comes from its name, so a plain rename makes a new channel (and the old one, with its number and favourites, is removed). To rename, put the **old** name in Keep ID, then change the name. Putting the current name there changes nothing, so it's safe to fill in before you need it.
- **Logo URL** sets the channel's logo from an `http://` or `https://` image URL. Without it, channels get the Rad Streams logo, which Sync copies to `/data/logos/rad-streams.png`.

### Upgrading from the old Stations text box (before 0.6.0)

Up to 0.5.3 stations were typed into one text box as `Name | Stream URL | City | Channel # | id=… | logo=…`
lines. 0.6.0 copies that list into the table the first time it loads, so the table opens already filled in and
Sync and Validate use it straight away. Every station keeps its channel (its id is worked out exactly as
before). A line that was commented out with `#` arrives with Skip ticked, plain `#` comments are dropped, and a
line that couldn't be split into columns lands whole in the Name cell with a note above the table. Check the
table and press **Save Settings** to keep it. The old text stays in the saved settings, untouched, as a backup.

### Guide and logos

Sync gives every Rad Streams channel a logo and a guide entry, so TV apps show them like other channels.
The guide comes from a "Custom Dummy EPG" source called **Rad Streams** that Sync creates: each channel
shows the station's name with "Live from Seattle, WA" (or "Nature photos" for a theme) in 4-hour blocks.
Dispatcharr generates these on the fly, so they never run out. If you pick a different logo or EPG for a
channel by hand, Sync leaves it alone. Sync owns the Rad Streams EPG source itself, so changes made to it in
Dispatcharr's Dummy EPG form are put back at the next Sync. Remove All Channels also deletes the Rad Streams EPG source.

## Settings

| Setting | What it does |
| --- | --- |
| Stations | The station table (see above). |
| Temperature Units | °F/mph or °C/km/h. |
| Text Movement | Burn-in protection: the text moves to a new spot on every photo change (once a minute), cycling through 8 positions, random, or corners only. It holds still in between. |
| Photo Credits | Brief (default) shows the photo and weather credit for 5 seconds when each photo appears, then fades it out. Always keeps it on screen. The licences require a credit, so it can't be turned off. |
| CPU Use | Normal (default) sends 25 frames a second. Low sends 15 and uses a faster encoder preset, which takes about a third less CPU per station with software encoding. Crossfades look a little less smooth. |

The **Advanced** section, collapsed until you open it, holds the rest:

| Setting | What it does |
| --- | --- |
| Resolution | 480p up to 4K. Default 1080p. |
| Video Bitrate | Upper limit per station. With software encoding the picture is encoded to a quality level, so still photos use less than the limit. |
| Video Encoding | Software (default) or a GPU: VAAPI (Intel/AMD), Intel Quick Sync, or NVIDIA NVENC. The GPU has to be passed through to the Dispatcharr container (`/dev/dri` for Intel/AMD, the NVIDIA container runtime for NVIDIA). Sync tests the GPU and falls back to software encoding, with a warning, if it doesn't work. Status shows which one is in use. For VAAPI the container also needs `/dev/dri` and its user in the `video` and `render` groups. Older Intel iGPUs (such as the UHD 630) only support constant quality with VAAPI, so the Video Bitrate limit doesn't apply there: the bitrate follows the picture, about 4-5 Mbps at peak on a dense starfield at 1080p25. `logs/rad_streams.log` shows the mode, e.g. `encoder: VAAPI (CQP 28)`. |
| Server Port | Local port for the channel streams (default 5980, bound to 127.0.0.1). Sync checks the port first and changes nothing if something else is using it. |

## Actions

- **Sync Stations** (background, with progress) checks the table, matches channels to it and starts the server.
- **Validate** (background, with progress) checks each stream, its track titles, the city and the photos. Stations are
  checked eight at a time, and the time allowed grows with the list (45 seconds per eight stations, up to 15 minutes).
- Both save a full report to `/data/rad_streams/reports/` (`sync-<date>-<time>.txt`, `validate-<date>-<time>.txt`;
  the newest 20 of each are kept, readable only by Dispatcharr's user). The latest is also in `logs/sync.log` and
  `logs/validate.log`. The pop-up shows a short summary, and Dispatcharr's task history keeps the result with every
  station's details as a downloadable JSON file.
- Pressing Sync, Validate or Remove while the same kind of run is still going says so instead of starting a second one.
- After Sync and Remove, Dispatcharr's plugin page refreshes itself, and open pages reload their channel and guide lists.
- **Status** shows whether the server is running and who's watching.
- **Stop Server** disconnects viewers and stops the server. Sync starts it again.
- **Remove All Channels** stops the server and deletes every channel the plugin created.

## How it works

The plugin starts a small background process (`rs/service.py`) that runs one
HTTP server with a route per station: `http://127.0.0.1:<port>/radio/<station-id>.ts`.
Each channel's stream points at its route. The first connection starts that
station's renderer:

1. The renderer opens the radio stream itself, strips the ICY metadata for the track title, and decodes the audio with ffmpeg.
2. Pillow draws frames at about 4 fps: a crossfading photo, a dim text panel (station, city, track, and the weather, labelled as the station's city), and the photo credit.
3. A second ffmpeg encodes the frames and audio to H.264/AAC MPEG-TS, blending frames up to 25 fps.

Each new viewer connection first gets about 1 MB of MPEG-TS null packets, which
players discard. Dispatcharr's proxy waits for about 1 MB of a stream before it
sends anything to a viewer, and a still-picture channel (especially with VAAPI,
which ignores the bitrate setting on older Intel GPUs) can take 8 seconds or more
to produce that much. The null packets fill that buffer at once. The proxy also
only forwards whole ~256 KB chunks, so for the first 6 seconds each viewer's
stream is topped up with more null packets to about 4 Mbit/s; after that
nothing is added. Together these get the first real frames to the player in
about a second instead of 8+ (since v0.6.2), for about 4 MB extra per tune-in.

Audio and video are paced against one clock. If the station drops, the
renderer reconnects with backoff and plays silence meanwhile, and the panel
says "Reconnecting to stream…". Blank or generic titles ("Unknown", ads, the
station's own name) show the station name instead. Each station opens on the
Rad Streams splash for 5 seconds; it stays up until the city's photos have
downloaded, or for good if there are none. The splash (a vintage radio and the logo) shifts slightly at each change. If weather can't be fetched, the last reading
stays up for three hours and is then hidden.

The server process survives Dispatcharr's web worker restarts. After a
container restart, the plugin relaunches it about 20 seconds after
Dispatcharr starts, if it was running before. Disabling or deleting the plugin stops it.

Files, all under `/data/rad_streams/`:

- `service.json` is the config written by Sync. `status.json` is the server status. `channels.json` maps stations to channels.
- `cache/images/<city>/` holds 20 photos per city. Every 12 hours up to half of them are swapped for new ones, and dropped photos are deleted. Once a day the server also deletes cities no station has played for 30 days, old splash images and leftover temporary files, so the cache doesn't grow. `cache/geocode/` holds city lookups. `cache/splash-*.png` is the splash, drawn once per resolution.
- `logs/rad_streams.log` (rotating), `logs/ffmpeg-<station>.log`, `logs/validate.log`, `logs/sync.log`.
- `reports/` holds the last 20 Sync and 20 Validate reports.

## Troubleshooting

- **Channel won't play:** press Status. If the server isn't running, press Sync and check `logs/service-console.log` and `logs/rad_streams.log`.
- **Port in use:** Sync says so and changes nothing. Change Server Port and Sync again; the channels are re-pointed automatically.
- **Use the "Proxy" stream profile** (Sync picks it automatically). An ffmpeg-based profile can read a real-time source as "buffering" and try to switch streams.
- **No track titles:** some stations don't send ICY metadata. Validate says so.
- **CPU:** a 1080p station uses roughly half of one CPU core while someone is watching, and nothing otherwise. GPU encoding (Video Encoding setting) takes the H.264 encode off the CPU; drawing the frames and blending them up to 25 fps still use the CPU. The CPU Use setting's Low choice cuts the software encoder's share by about a third.
- **Guide empty, or the Rad Streams EPG missing from the EPG manager:** reload the Dispatcharr page. Since v0.5.1 Sync tells open pages to reload their EPG lists (and since v0.6.0 their channel lists), but an older Dispatcharr may not listen for that.
- **Channel takes 8+ seconds to start** (most noticeable with VAAPI): fixed in v0.6.2 by the null-packet prelude described under How it works.
- **Bitrate setting has no effect with VAAPI:** older Intel GPUs (e.g. UHD 630) only support constant-quality VAAPI encoding (`VAAPI (CQP 28)` in the log), so the stream size follows the picture, not the setting.
- **Detail "pulses" every 2 seconds** (fine backgrounds such as starfields go soft at each keyframe, then sharpen): fixed in v0.6.1 for software encoding, which now uses x264's lookahead and a larger buffer instead of `-tune zerolatency`. VAAPI on an older Intel iGPU encodes every frame at the same quality and doesn't pulse.
- **GPU encoding not used:** Sync's message and `logs/rad_streams.log` give the reason, such as "Cannot load libcuda.so.1" (the NVIDIA runtime isn't set up for the container) or "no /dev/dri render device" (pass `/dev/dri` through). If a GPU passes Sync's test but an encode then fails as a station starts (some NVIDIA cards limit how many run at once), that station restarts with software encoding and Status says so until the next Sync.
- **After a Dispatcharr database reset or restore:** press Sync. Rad Streams only updates or deletes channels it can recognise as its own, so it creates fresh ones and leaves any others alone.

## Standalone testing

The renderer runs without Dispatcharr (Python 3.10+, Pillow, numpy, requests, ffmpeg):

```
cd rad_streams
python3 -m rs --name "KEXP" --url "https://kexp.streamguys1.com/kexp160.aac" \
    --city "Seattle, WA" --seconds 120 --out kexp.ts        # record to a file
python3 -m rs.server --stations stations.txt                 # serve /radio/<id>.ts routes
```

`python3 -m pip install -r requirements-dev.txt` installs what the tests need.
GitHub runs the same tests on every push and pull request (`.github/workflows/tests.yml`).
`python3 -m pytest tests` runs the test suite offline, using a fake ICY
server, canned API responses, synthetic photos, and a stand-in for
Dispatcharr's channel models.

## Code layout

- `plugin.py`, `plugin.json`: Dispatcharr plugin (settings, actions, service lifecycle)
- `rs/channels.py`: channel/stream sync, guide entries and logos
- `rs/config.py`: the stations table and old text format, cell checks, render settings
- `rs/validate.py`: Validate checks
- `rs/reports.py`: saved Sync and Validate reports
- `rs/service.py`: the background process
- `rs/server.py`: on-demand HTTP server
- `rs/station.py`: one station's renderer, paced in real time
- `rs/audio.py`: stream connection, ICY parsing, decoding, jitter buffer
- `rs/hls.py`: live HLS client (playlists, segments, EXTINF/ID3 titles)
- `rs/images.py`: Wikimedia Commons search and cache
- `rs/nasa.py`: NASA Image Library search, for the space themes
- `rs/geo.py`, `rs/weather.py`: Open-Meteo
- `rs/render.py`, `rs/motion.py`: frame drawing, burn-in prevention
- `rs/splash.py`: the splash screen, drawn in code
- `rs/cleanup.py`: the daily cache cleanup
- `rs/themes.py`: theme channels (`Theme: Space`) and their searches
- `rs/encoder.py`: ffmpeg encoder process, and the GPU encoder test

## License

Rad Streams is licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later), the same licence as Dispatcharr. See `LICENSE`. The Inter font keeps its own licence, listed under Credits.

## Credits

Inter font: SIL Open Font License 1.1 (`assets/fonts/Inter-LICENSE.txt`).
Photos: Wikimedia Commons contributors. Only CC0, public domain, CC BY and CC BY-SA images are used, each credited on screen.
Space photos: [NASA Image and Video Library](https://images.nasa.gov/) (public domain; NASA's own images only, credited on screen).
Weather and geocoding: [Open-Meteo](https://open-meteo.com/) (CC BY 4.0).
Channel creation and process management follow the approach of
[Pirate Weather Station](https://github.com/dexdeadly/pirate-weatharr-station).
