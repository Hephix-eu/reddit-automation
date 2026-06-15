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
import io
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


def _draw_cursor_on_frame(jpeg_bytes: bytes, px: float, py: float) -> bytes:
    """Overlay a cursor ring at pixel position (px, py) on a JPEG frame.

    Best-effort — returns original bytes unchanged if PIL is unavailable or
    anything fails. Draws a two-tone ring (black outer + orange inner) with a
    small white centre dot so the cursor is visible against any background.
    """
    try:
        from PIL import Image, ImageDraw
        img = Image.open(io.BytesIO(jpeg_bytes))
        draw = ImageDraw.Draw(img)
        ix, iy = int(round(px)), int(round(py))
        r = 10
        draw.ellipse([ix-r-2, iy-r-2, ix+r+2, iy+r+2], outline=(0, 0, 0), width=2)
        draw.ellipse([ix-r,   iy-r,   ix+r,   iy+r  ], outline=(255, 80, 0), width=3)
        draw.ellipse([ix-2,   iy-2,   ix+2,   iy+2  ], fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=60)
        return buf.getvalue()
    except Exception:
        return jpeg_bytes


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

    # Viewport size for cursor coordinate scaling (CSS px → frame px).
    # Assumed device pixel ratio = 1 for desktop Multilogin profiles.
    try:
        vp = page.viewport_size() or {}
    except Exception:
        vp = {}
    _vp_w = float(vp.get("width", 1280))
    _vp_h = float(vp.get("height", 720))
    # Scale from viewport CSS pixels to frame pixels (maxWidth=1280, maxHeight=720).
    _cursor_scale = min(1280.0 / _vp_w, 720.0 / _vp_h, 1.0)

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
                frame_bytes = base64.b64decode(event["data"])
                # Overlay the cursor position tracked by lib.browse mouse helpers.
                try:
                    from lib.browse import get_cursor_pos
                    pos = get_cursor_pos()
                    if pos is not None:
                        frame_bytes = _draw_cursor_on_frame(
                            frame_bytes,
                            pos[0] * _cursor_scale,
                            pos[1] * _cursor_scale,
                        )
                except Exception:
                    pass
                if ffmpeg.poll() is None and ffmpeg.stdin and not ffmpeg.stdin.closed:
                    ffmpeg.stdin.write(frame_bytes)
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
