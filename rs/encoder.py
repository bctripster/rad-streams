"""The ffmpeg process that turns raw frames + PCM into an MPEG-TS stream.

Video arrives on stdin as rgb24 at the render rate (a few fps); audio arrives
on a second pipe as s16le PCM. Both are raw with fixed rates, so ffmpeg can
derive timestamps from byte counts and the two stay in sync as long as the
writers pace themselves against the same clock (see station.py).
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import IO, Optional, Union

from .audio import CHANNELS, SAMPLE_RATE

log = logging.getLogger("rad_streams.encoder")


def build_command(width: int, height: int, render_fps: float, output_fps: int, video_kbps: int,
                  audio_kbps: int, audio_fd: int, output: str, *, ffmpeg: str = "ffmpeg",
                  smooth: bool = True) -> list[str]:
    gop = max(1, int(output_fps * 2))  # keyframe every 2 s so viewers join quickly
    # framerate= blends neighbouring frames, which turns the few-fps render
    # into smooth crossfades; fps= only repeats frames.
    vf = (f"framerate=fps={output_fps}:interp_start=0:interp_end=255:scene=100"
          if smooth else f"fps={output_fps}")
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
        "-framerate", f"{render_fps:g}", "-thread_queue_size", "64", "-i", "pipe:0",
        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
        "-thread_queue_size", "256", "-i", f"pipe:{audio_fd}",
        "-map", "0:v", "-map", "1:a",
        "-vf", f"{vf},format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
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
                 ffmpeg: str = "ffmpeg", smooth: bool = True, log_path: Optional[str] = None):
        self.width, self.height = width, height
        self.args = dict(render_fps=render_fps, output_fps=output_fps, video_kbps=video_kbps,
                         audio_kbps=audio_kbps, ffmpeg=ffmpeg, smooth=smooth)
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
