"""The ffmpeg process that turns raw frames + PCM into an MPEG-TS stream.

Video arrives on stdin as rgb24 at the render rate (a few fps); audio arrives
on a second pipe as s16le PCM. Both are raw with fixed rates, so ffmpeg can
derive timestamps from byte counts and the two stay in sync as long as the
writers pace themselves against the same clock (see station.py).
"""
from __future__ import annotations

import glob
import logging
import os
import re
import subprocess
import threading
from typing import IO, Optional, Union

from .audio import CHANNELS, SAMPLE_RATE
from .config import VIDEO_ENCODERS

log = logging.getLogger("rad_streams.encoder")

ENCODER_LABELS = {"software": "software (CPU)", "vaapi": "VAAPI (Intel/AMD GPU)",
                  "qsv": "Intel Quick Sync", "nvenc": "NVIDIA NVENC"}
PROBE_TIMEOUT = 10.0
PROBE_SIZE = (640, 360)
PROBE_FRAMES = 5


def render_device() -> Optional[str]:
    """The first DRM render node (``/dev/dri/renderD128`` on most systems), if any."""
    nodes = sorted(glob.glob("/dev/dri/renderD*"))
    return nodes[0] if nodes else None


def video_args(encoder: str, video_kbps: int, gop: int) -> tuple[list[str], str, list[str]]:
    """``(global args, pixel-format filter, codec args)`` for one video encoder.

    The global args go before the inputs (a VAAPI device has to be opened
    first); the filter is appended to the frame-rate filter. An unknown or
    unusable name gives the software encoder.
    """
    rate = ["-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k",
            "-bufsize", f"{video_kbps * 2}k", "-g", str(gop)]
    if encoder == "vaapi":
        device = render_device()
        if device:
            # Frames are uploaded to the GPU as NV12 and encoded there.
            return (["-vaapi_device", device], "format=nv12,hwupload",
                    ["-c:v", "h264_vaapi", *rate, "-bf", "0"])
    if encoder == "qsv":
        return ([], "format=nv12",
                ["-c:v", "h264_qsv", "-preset", "veryfast", "-look_ahead", "0", *rate, "-bf", "0"])
    if encoder == "nvenc":
        return ([], "format=yuv420p",
                ["-c:v", "h264_nvenc", "-preset", "p4", *rate, "-bf", "0", "-forced-idr", "1"])
    return ([], "format=yuv420p",
            ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", *rate,
             "-keyint_min", str(gop), "-sc_threshold", "0"])


def probe(encoder: str, ffmpeg: str = "ffmpeg", timeout: float = PROBE_TIMEOUT) -> Optional[str]:
    """Encode a few black frames with ``encoder``. Returns None if that works,
    else a short reason. Uses raw frames on stdin, so it needs no lavfi."""
    if encoder == "vaapi" and not render_device():
        return "no /dev/dri render device (pass the GPU through to the container)"
    w, h = PROBE_SIZE
    pre, fmt, codec = video_args(encoder, 1000, 10)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", *pre,
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-framerate", "5",
           "-i", "pipe:0", "-frames:v", str(PROBE_FRAMES), "-vf", fmt, *codec, "-f", "null", "-"]
    try:
        # -nostdin only stops ffmpeg reading keys; the frames still come through pipe:0.
        proc = subprocess.run(cmd, input=bytes(w * h * 3 * PROBE_FRAMES),
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
    except FileNotFoundError:
        return "ffmpeg not found"
    except subprocess.TimeoutExpired:
        return "the test encode timed out"
    except OSError as exc:
        return str(exc)
    if proc.returncode == 0:
        return None
    # The first error names the cause ("Cannot load libcuda.so.1"); later ones are
    # generic. Drop the "[h264_nvenc @ 0x55d…]" prefix, which only adds noise.
    lines = [re.sub(r"^\[[^\]]*\]\s*", "", l).strip()
             for l in proc.stderr.decode("utf-8", "replace").splitlines()]
    lines = [l for l in lines if l]
    return lines[0][:200] if lines else f"ffmpeg exited with code {proc.returncode}"


def cached(encoder: str, ffmpeg: str = "ffmpeg") -> Optional[tuple[str, Optional[str]]]:
    """``resolve()``'s answer if it's already known, without testing anything."""
    if encoder not in VIDEO_ENCODERS or encoder == "software":
        return "software", None
    with _resolve_lock:
        if (encoder, ffmpeg) not in _resolved:
            return None
        reason = _resolved[(encoder, ffmpeg)]
    return ("software", reason) if reason else (encoder, None)


_resolved: dict = {}
_resolve_lock = threading.Lock()


def resolve(encoder: str, ffmpeg: str = "ffmpeg") -> tuple[str, Optional[str]]:
    """The encoder to use for ``encoder``, and why it fell back (None if it didn't).

    A GPU encoder is tested once per process; if the test fails the software
    encoder is used, so a missing driver never takes the channels down.
    """
    if encoder not in VIDEO_ENCODERS or encoder == "software":
        return "software", None
    with _resolve_lock:      # one test per encoder, even when stations start together
        key = (encoder, ffmpeg)
        if key not in _resolved:
            reason = probe(encoder, ffmpeg)
            if reason:
                log.warning("%s encoding unavailable (%s); using software encoding",
                            ENCODER_LABELS.get(encoder, encoder), reason)
            else:
                log.info("using %s for video encoding", ENCODER_LABELS.get(encoder, encoder))
            _resolved[key] = reason
        reason = _resolved[key]
    return ("software", reason) if reason else (encoder, None)


def build_command(width: int, height: int, render_fps: float, output_fps: int, video_kbps: int,
                  audio_kbps: int, audio_fd: int, output: str, *, ffmpeg: str = "ffmpeg",
                  smooth: bool = True, video_encoder: str = "software") -> list[str]:
    """``video_encoder`` should already be ``resolve()``-d: it isn't tested here."""
    gop = max(1, int(output_fps * 2))  # keyframe every 2 s so viewers join quickly
    # framerate= blends neighbouring frames, which turns the few-fps render
    # into smooth crossfades; fps= only repeats frames.
    vf = (f"framerate=fps={output_fps}:interp_start=0:interp_end=255:scene=100"
          if smooth else f"fps={output_fps}")
    pre, fmt, codec = video_args(video_encoder, video_kbps, gop)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", *pre,
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
        "-framerate", f"{render_fps:g}", "-thread_queue_size", "64", "-i", "pipe:0",
        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
        "-thread_queue_size", "256", "-i", f"pipe:{audio_fd}",
        "-map", "0:v", "-map", "1:a",
        "-vf", f"{vf},{fmt}",
        *codec,
        "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ar", str(SAMPLE_RATE),
        "-f", "mpegts", "-mpegts_flags", "+resend_headers", "-flush_packets", "1",
        "-muxdelay", "0", "-muxpreload", "0",
    ]
    if output == "-":
        cmd.append("pipe:1")
    else:
        cmd += ["-y", output]
    return cmd


class Encoder:
    def __init__(self, width: int, height: int, *, render_fps: float, output_fps: int,
                 video_kbps: int, audio_kbps: int, output: Union[str, IO[bytes]],
                 ffmpeg: str = "ffmpeg", smooth: bool = True, log_path: Optional[str] = None,
                 video_encoder: str = "software"):
        self.width, self.height = width, height
        self.args = dict(render_fps=render_fps, output_fps=output_fps, video_kbps=video_kbps,
                         audio_kbps=audio_kbps, ffmpeg=ffmpeg, smooth=smooth)
        # A GPU encoder is tested (once per process) before it's used.
        self.args["video_encoder"], self.fallback_reason = resolve(video_encoder, ffmpeg)
        self.output = output
        self.log_path = log_path
        self.proc: Optional[subprocess.Popen] = None
        self.audio_w: Optional[int] = None

    @property
    def stdout(self) -> Optional[IO[bytes]]:
        return self.proc.stdout if self.proc else None

    def start(self) -> None:
        audio_r, audio_w = os.pipe()
        if isinstance(self.output, str) and self.output != "-":
            out_arg, stdout = self.output, subprocess.DEVNULL
        elif self.output == "-":
            out_arg, stdout = "-", subprocess.PIPE      # caller reads self.stdout
        else:
            out_arg, stdout = "-", self.output           # an open file object
        # pass_fds keeps the read end at the same fd number in the child, so
        # ffmpeg can open it as pipe:<n> without a preexec hook (unsafe in a
        # threaded process like Dispatcharr's).
        cmd = build_command(self.width, self.height, audio_fd=audio_r, output=out_arg, **self.args)
        stderr = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        log.debug("ffmpeg: %s", " ".join(cmd))
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                         pass_fds=(audio_r,), bufsize=0)
        except BaseException:
            os.close(audio_w)
            raise
        finally:
            os.close(audio_r)
            if stderr is not subprocess.DEVNULL:
                stderr.close()
        self.audio_w = audio_w

    def write_video(self, frame_bytes: bytes) -> None:
        self.proc.stdin.write(frame_bytes)

    def write_audio(self, pcm: bytes) -> None:
        view = memoryview(pcm)
        while view:
            fd = self.audio_w
            if fd is None:
                raise BrokenPipeError("encoder stopped")
            n = os.write(fd, view)
            view = view[n:]

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 5.0) -> None:
        proc = self.proc
        if proc is None:
            return
        fd, self.audio_w = self.audio_w, None     # close once: a reused fd number isn't ours
        try:
            proc.stdin.close()
        except Exception:
            pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def abort(self) -> None:
        """Kill ffmpeg at once, so whoever reads its output sees the end."""
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
