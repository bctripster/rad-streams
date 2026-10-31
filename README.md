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
2. In Dispatcharr, open **Plugins**, reload if needed, and enable **Rad Streams**.
3. Enter your stations in the **Stations** box, one per line:

   ```
   Name | Stream URL | City | Channel # (optional)
   KEXP | https://kexp.streamguys1.com/kexp160.aac | Seattle, WA | 501
   SomaFM Groove Salad | https://ice1.somafm.com/groovesalad-128-mp3 | San Francisco, CA
   ```

   Use `City, Region` or `City, Country` when a name is ambiguous: US states, Canadian provinces and
   Australian states work by name or abbreviation (`Portland, ME`, `Vancouver, BC`, `Perth, WA, Australia`, `Paris, France`).
   For a channel that isn't about a city, put `Theme:` and a subject in the City column. A theme
   channel shows photos of that subject; its panel shows just the station and track, with no place,
   theme name or weather:

   ```
   Groove Salad | https://ice1.somafm.com/groovesalad-128-mp3 | Theme: Nature
   Space Station Soma | https://ice1.somafm.com/spacestation-128-mp3 | Theme: Space
   ```

   Built-in themes have search terms picked for good photos: Space, Earth, Nature, Ocean, Mountains,
   Forest, Waterfalls, Desert, Aurora, Sunsets, Beaches, Lakes, Wildlife and Night Sky. Any other word
   (`Theme: Trains`, `Theme: Lighthouses`) is searched on Wikimedia Commons, trying a plural in the singular too
   (`Lighthouses` also finds `Lighthouse`). Space, Earth, Night Sky and Aurora also pull public-domain photos
   from the NASA Image Library. No photographer fills more than a few spots in a set.
   Lines starting with `#` are ignored. Icecast/Shoutcast streams (including old Shoutcast v1 servers), `.pls`/`.m3u` playlist links and
   HLS (`.m3u8`) streams all work. HLS track titles come from the playlist or the segments' ID3 tags;
   encrypted and fMP4 HLS streams aren't supported.
4. Press **Validate** to check every stream, its track titles, the city and available photos.
5. Press **Sync Stations**. Channels appear in the Rad Streams group, and the stream server starts.

After updating the plugin files, press **Sync Stations** once: it restarts the stream server so the new code takes effect.

Sync again after any edit. It creates, updates and removes channels to match the list.

### Optional columns: `id=` and `logo=`

After the Channel # column you can add `id=` and `logo=`, in either order. The Channel # column can be left empty (`| |`) or left out:

```
KEXP Seattle | https://kexp.streamguys1.com/kexp160.aac | Seattle, WA | 501 | id=KEXP
SomaFM Groove Salad | https://ice1.somafm.com/groovesalad-128-mp3 | San Francisco, CA | | logo=https://example.com/groove-salad.png
```

- **`id=`** keeps a station's channel when you rename it. A station's id comes from its name, so a plain rename makes a new channel (and the old one, with its number and favourites, is removed). To rename, add `id=` with the **old** name, then change the name. Adding `id=<current name>` to an existing station changes nothing, so it's safe to add it before you need it.
- **`logo=`** sets the channel's logo from an `http://` or `https://` image URL. Without it, channels get the Rad Streams logo, which Sync copies to `/data/logos/rad-streams.png`.

### Guide and logos

Sync gives every Rad Streams channel a logo and a guide entry, so TV apps show them like other channels.
The guide comes from a "Custom Dummy EPG" source called **Rad Streams** that Sync creates: each channel
shows the station's name with "Live from Seattle, WA" (or "Nature photos" for a theme) in 4-hour blocks.
Dispatcharr generates these on the fly, so they never run out. If you pick a different logo or EPG for a
channel by hand, Sync leaves it alone. Remove All Channels also deletes the Rad Streams EPG source.

## Settings

| Setting | What it does |
| --- | --- |
| Stations | The station list (see above). |
| Temperature Units | °F/mph or °C/km/h. |
| Resolution | 480p up to 4K. Default 1080p. |
| Video Bitrate | Upper limit per station. Still pictures usually use far less. |
| Text Movement | Burn-in protection: the text moves to a new spot on every photo change (once a minute), cycling through 8 positions, random, or corners only. It holds still in between. |
| Photo Credits | Brief (default) shows the photo and weather credit for 5 seconds when each photo appears, then fades it out. Always keeps it on screen. The licences require a credit, so it can't be turned off. |
| Video Encoding | Software (default) or a GPU: VAAPI (Intel/AMD), Intel Quick Sync, or NVIDIA NVENC. The GPU has to be passed through to the Dispatcharr container (`/dev/dri` for Intel/AMD, the NVIDIA container runtime for NVIDIA). Sync tests the GPU and falls back to software encoding, with a warning, if it doesn't work. Status shows which one is in use. |
| Server Port | Local port for the channel streams (default 5980, bound to 127.0.0.1). |

## Actions

- **Sync Stations** matches channels to the list and starts the server.
- **Validate** writes a full report to `/data/rad_streams/logs/validate.log`. The pop-up shows a short summary.
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

## Troubleshooting

- **Channel won't play:** press Status. If the server isn't running, press Sync and check `logs/service-console.log` and `logs/rad_streams.log`.
- **Port in use:** change Server Port and Sync. The channels are re-pointed automatically.
- **Use the "Proxy" stream profile** (Sync picks it automatically). An ffmpeg-based profile can read a real-time source as "buffering" and try to switch streams.
- **No track titles:** some stations don't send ICY metadata. Validate says so.
- **CPU:** a 1080p station uses roughly half of one CPU core while someone is watching, and nothing otherwise. GPU encoding (Video Encoding setting) takes the H.264 encode off the CPU; drawing the frames and blending them up to 25 fps still use the CPU.
- **Guide empty, or the Rad Streams EPG missing from the EPG manager:** reload the Dispatcharr page. Since v0.5.1 Sync tells open pages to reload their EPG lists, but an older Dispatcharr may not listen for that.
- **GPU encoding not used:** Sync's message and `logs/rad_streams.log` give the reason, such as "Cannot load libcuda.so.1" (the NVIDIA runtime isn't set up for the container) or "no /dev/dri render device" (pass `/dev/dri` through).

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
- `rs/validate.py`: Validate checks
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
