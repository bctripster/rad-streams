"""v0.6.1: VAAPI on constant-QP-only Intel drivers, and no keyframe "pulse" in software.

1. The VAAPI test tries CBR, then constant QP, and the stream uses whichever passed.
2. VAAPI runs the filter graph on one thread ("-filter_threads 1", a global option).
3. The test encode uses exactly the options the stream will use.
4. libx264 drops "-tune zerolatency" (keyframes were starved) and uses a 4x buffer.
"""
import shutil

import pytest

from rs import encoder

ARGS = (1280, 720, 4, 25, 3000, 128, 5, "-")
CBR_ERROR = b"[h264_vaapi @ 0x5] Driver does not support CBR RC mode (supported modes: CQP).\n"


@pytest.fixture
def gpu(monkeypatch):
    """A render node exists, and no test results are remembered."""
    monkeypatch.setattr(encoder, "render_device", lambda: "/dev/dri/renderD128")
    monkeypatch.setattr(encoder, "_resolved", {})
    monkeypatch.setattr(encoder, "_vaapi_modes", {})


class Run:
    """Stands in for subprocess.run: fails any command that matches ``reject``."""

    def __init__(self, reject=None, error=CBR_ERROR):
        self.reject, self.error, self.cmds = reject, error, []

    def __call__(self, cmd, **kwargs):
        self.cmds.append(cmd)
        failed = self.reject is not None and self.reject(cmd)

        class Done:
            returncode = 1 if failed else 0
            stderr = self.error if failed else b""
        return Done()


def cqp_only(cmd):
    return "-rc_mode" not in cmd


def test_cqp_only_driver_passes_and_the_stream_uses_cqp(gpu, monkeypatch):
    run = Run(reject=cqp_only)
    monkeypatch.setattr(encoder.subprocess, "run", run)
    assert encoder.resolve("vaapi") == ("vaapi", None)
    assert len(run.cmds) == 2 and "-b:v" in run.cmds[0]            # CBR first, then CQP
    cmd = encoder.build_command(*ARGS, video_encoder="vaapi")
    assert cmd[cmd.index("-rc_mode") + 1] == "CQP"
    assert cmd[cmd.index("-qp") + 1] == str(encoder.VAAPI_CQP)
    assert "-b:v" not in cmd and "-maxrate" not in cmd and "-bufsize" not in cmd
    assert cmd[cmd.index("-bf") + 1] == "0" and cmd[cmd.index("-g") + 1] == "50"
    assert encoder.describe("vaapi") == "VAAPI (CQP 28)"
    # remembered: a second resolve doesn't test again
    assert encoder.resolve("vaapi") == ("vaapi", None) and len(run.cmds) == 2


def test_cbr_driver_keeps_the_bitrate(gpu, monkeypatch):
    run = Run()
    monkeypatch.setattr(encoder.subprocess, "run", run)
    assert encoder.probe("vaapi") is None and len(run.cmds) == 1
    cmd = encoder.build_command(*ARGS, video_encoder="vaapi")
    assert cmd[cmd.index("-b:v") + 1] == "3000k" and "-rc_mode" not in cmd
    assert encoder.describe("vaapi", 3000) == "VAAPI (CBR 3000k)"


def test_vaapi_that_fails_every_mode_gives_the_first_reason(gpu, monkeypatch):
    monkeypatch.setattr(encoder.subprocess, "run",
                        Run(reject=lambda cmd: True, error=b"[AVHWDeviceContext @ 0x1] No VA display found\n"))
    assert encoder.resolve("vaapi") == ("software", "No VA display found")


def test_forget_drops_the_rate_mode(gpu, monkeypatch):
    monkeypatch.setattr(encoder.subprocess, "run", Run(reject=cqp_only))
    encoder.probe("vaapi")
    assert encoder.vaapi_rate_mode() == "cqp"
    encoder.forget()
    assert encoder.vaapi_rate_mode() == "cbr"


def test_filter_threads_is_global_and_before_the_inputs(gpu):
    cmd = encoder.build_command(*ARGS, video_encoder="vaapi")
    i = cmd.index("-filter_threads")
    assert cmd[i + 1] == "1" and i < cmd.index("-i")
    assert cmd.index("-vaapi_device") < cmd.index("-i")


def test_probe_uses_the_stream_options(gpu, monkeypatch):
    """The test command carries the stream's global and codec options for the chosen mode."""
    run = Run(reject=cqp_only)
    monkeypatch.setattr(encoder.subprocess, "run", run)
    encoder.probe("vaapi")
    test = run.cmds[-1]
    stream = encoder.build_command(*ARGS, video_encoder="vaapi")
    for opt in ("-vaapi_device", "-filter_threads", "-rc_mode", "-qp", "-bf", "-c:v"):
        assert test[test.index(opt) + 1] == stream[stream.index(opt) + 1], opt
    assert test.index("-filter_threads") < test.index("-i")
    assert test[test.index("-vf") + 1] == "format=nv12,hwupload"
    assert stream[stream.index("-vf") + 1].endswith(",format=nv12,hwupload")


def test_software_without_dev_dri(monkeypatch):
    monkeypatch.setattr(encoder, "render_device", lambda: None)
    monkeypatch.setattr(encoder, "_resolved", {})
    called = []
    monkeypatch.setattr(encoder.subprocess, "run", lambda *a, **k: called.append(a))
    assert encoder.resolve("vaapi")[0] == "software"
    assert "no /dev/dri" in encoder.resolve("vaapi")[1] and not called   # nothing was run
    cmd = encoder.build_command(*ARGS, video_encoder="vaapi")
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert "-vaapi_device" not in cmd and "-filter_threads" not in cmd and "-rc_mode" not in cmd


def test_software_has_lookahead_and_a_4x_buffer():
    cmd = encoder.build_command(*ARGS)
    assert "-tune" not in cmd and "zerolatency" not in cmd
    assert cmd[cmd.index("-bufsize") + 1] == "12000k" and cmd[cmd.index("-maxrate") + 1] == "3000k"
    assert cmd[cmd.index("-bf") + 1] == "0"                          # latency stays low
    assert encoder.describe("software", 3000) == "software (CRF 23, max 3000k)"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_software_test_encode_still_works():
    assert encoder.probe("software") is None
