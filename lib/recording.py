"""Session video recording via CDP screencast → ffmpeg → mp4.

Captures JPEG frames at ~6 fps from a CDP-connected Playwright page and pipes
them to ffmpeg, producing a single mp4 per session. One file per session in
`accounts/<user>/recordings/<utc-timestamp>_<sid8>.mp4`.

Rotation handled by /etc/cron.d/reddit-recordings-cleanup (7-day default).

Usage:
    from lib import recording
    with recording.record_session(page, account_dir / "recordings", session_id=sid) as video_path:
        # ... do warmup work ...
        # mp4 closes when the with-block exits

Failures are swallowed — recording is best-effort, never crashes the session.
"""
import base64
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


@contextmanager
def record_session(page, output_dir: Path, session_id: Optional[str] = None, fps: int = 6):
    """Record the page as an mp4 via CDP screencast.

    Yields the Path of the mp4 being written. If setup fails (e.g. the page
    already crashed), still yields the path so callers can rely on the contract,
    but no frames are captured.

    NB: @contextmanager REQUIRES exactly one yield in this function — never put
    yield inside a try/except that could yield twice. That triggers Python's
    "generator didn't stop after throw()" RuntimeError when an exception fires
    inside the with-block. (Previous version had two yields and was the source
    of session_crash errors observed 2026-05-30/31.)
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sid = (session_id or uuid.uuid4().hex)[:8]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    output_path = output_dir / f"{stamp}_{sid}.mp4"

    ffmpeg = None
    cdp = None
    started = False
    frames_received = [0]

    # ── setup (best-effort, never propagates) ─────────────────────────────
    try:
        ffmpeg = subprocess.Popen([
            "ffmpeg",
            "-y",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-r", str(fps),
            "-i", "-",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-loglevel", "error",
            str(output_path),
        ], stdin=subprocess.PIPE)

        cdp = page.context.new_cdp_session(page)

        def on_frame(event):
            try:
                if ffmpeg.poll() is None and ffmpeg.stdin and not ffmpeg.stdin.closed:
                    ffmpeg.stdin.write(base64.b64decode(event["data"]))
                    frames_received[0] += 1
                cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
            except BrokenPipeError:
                pass
            except Exception:
                pass

        cdp.on("Page.screencastFrame", on_frame)

        cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": 60,
            "maxWidth": 1280,
            "maxHeight": 720,
            "everyNthFrame": max(1, 30 // fps),
        })
        started = True
        print(f"[recording] started → {output_path}", file=sys.stderr)
    except Exception as e:
        print(f"[recording] setup failed: {type(e).__name__}: {e}", file=sys.stderr)
        # Clean up any partial ffmpeg now so the teardown phase has nothing to do.
        if ffmpeg is not None:
            try:
                if ffmpeg.stdin: ffmpeg.stdin.close()
            except Exception:
                pass
            try:
                ffmpeg.terminate()
            except Exception:
                pass
            ffmpeg = None
        cdp = None
        # started stays False — teardown will skip

    # ── single yield — exactly once, no matter what setup did ─────────────
    try:
        yield output_path
    finally:
        if started and cdp is not None:
            try:
                cdp.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                cdp.detach()
            except Exception:
                pass
        if ffmpeg is not None:
            try:
                if ffmpeg.stdin:
                    ffmpeg.stdin.close()
                ffmpeg.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    ffmpeg.kill()
                except Exception:
                    pass
            except Exception:
                pass
        if started:
            print(
                f"[recording] stopped → {output_path} "
                f"(frames={frames_received[0]}, size={output_path.stat().st_size if output_path.exists() else 0} bytes)",
                file=sys.stderr,
            )
