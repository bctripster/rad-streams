"""Standalone renderer: ``python -m rs --name ... --url ... --city ... --out out.ts``.

Runs one station outside Dispatcharr and writes MPEG-TS to a file (or to
stdout with ``--out -``), for testing the renderer on its own.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import RESOLUTIONS, TEXT_PATTERNS, RenderSettings, Station
from .station import StationRunner


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rad_streams", description=__doc__)
    ap.add_argument("--name", required=True, help="station name shown on screen")
    ap.add_argument("--url", required=True, help="radio stream URL (Icecast/Shoutcast, .pls/.m3u ok)")
    ap.add_argument("--city", required=True, help="e.g. 'Burlington, VT' or 'Lyon, France'")
    ap.add_argument("--out", default="out.ts", help="output .ts path, or - for stdout")
    ap.add_argument("--seconds", type=float, default=60, help="how long to record")
    ap.add_argument("--resolution", default="1920x1080", choices=sorted(RESOLUTIONS))
    ap.add_argument("--units", default="F", choices=["F", "C"])
    ap.add_argument("--render-fps", type=float, default=4.0)
    ap.add_argument("--output-fps", type=int, default=25)
    ap.add_argument("--video-kbps", type=int, default=3000)
    ap.add_argument("--image-seconds", type=int, default=60)
    ap.add_argument("--pattern", default="cycle", choices=TEXT_PATTERNS)
    ap.add_argument("--data-dir", default=os.path.abspath("rs-data"))
    ap.add_argument("--no-smooth", action="store_true", help="repeat frames instead of blending")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = RenderSettings.from_resolution(
        a.resolution, units=a.units, render_fps=a.render_fps, output_fps=a.output_fps,
        video_kbps=a.video_kbps, image_seconds=a.image_seconds, text_pattern=a.pattern,
        data_dir=a.data_dir)
    os.makedirs(settings.log_dir, exist_ok=True)
    out = sys.stdout.buffer if a.out == "-" else a.out
    runner = StationRunner(Station(a.name, a.url, a.city), settings, out, smooth=not a.no_smooth,
                           log_path=os.path.join(settings.log_dir, "ffmpeg.log"))
    try:
        runner.run_for(a.seconds)
    except KeyboardInterrupt:
        runner.stop()
    logging.info("rendered %d frames, repeated %d, audio underruns %d%s",
                 runner.frames_rendered, runner.frames_repeated, runner.radio.pcm.underruns,
                 f", error: {runner.error}" if runner.error else "")
    return 1 if runner.error else 0


if __name__ == "__main__":
    sys.exit(main())
