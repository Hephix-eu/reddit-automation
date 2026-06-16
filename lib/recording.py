"""Session video recording via CDP screencast → ffmpeg → mp4.

Architecture (revised):
  - CDP screencast callback stores the latest JPEG in a shared slot.
  - A background sender thread fires at a fixed fps, reads the latest JPEG,
    overlays the cursor at the current _last_mouse_pos, and pipes to ffmpeg.
  - This decouples cursor-overlay frame rate from Chrome's repaint rate.
    Chrome only repaints when content changes; cursor movement rarely causes
    repaints, so without the sender thread cursor moves appear to teleport.

Usage:
    from lib import recording
    with recording.record_session(page, account_dir / "recordings", session_id=sid) as video_path:
        # ... do warmup work ...
"""
import base64
import io
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


def _draw_cursor_on_frame(jpeg_bytes: bytes, px: float, py: float) -> bytes:
    """Overlay the cursor.in arrow cursor at pixel position (px, py) on a JPEG frame."""
    try:
        from PIL import Image, ImageDraw
        img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGBA")
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)

        ix, iy = int(round(px)), int(round(py))
        S = 1.5
        hx, hy = 8.2, 4.9

        def poly(*coords):
            return [(ix + (x - hx) * S, iy + (y - hy) * S) for x, y in coords]

        # Layer 1 — white arrow body (outline)
        d.polygon(poly(
            (8.2, 20.9), (8.2, 4.9), (19.8, 16.5), (13.0, 16.5), (12.6, 16.6),
        ), fill=(255, 255, 255, 255))

        # Layer 2 — white tail outline
        d.polygon(poly(
            (17.3, 21.6), (13.7, 23.1), (9.0, 12.0), (12.7, 10.5),
        ), fill=(255, 255, 255, 255))

        # Layer 3 — black tail shaft
        d.polygon(poly(
            (11.030, 14.293), (12.874, 13.519), (15.971, 20.895), (14.127, 21.669),
        ), fill=(0, 0, 0, 255))

        # Layer 4 — black arrow body
        d.polygon(poly(
            (9.2, 7.3), (9.2, 18.5), (12.2, 15.6), (12.6, 15.5), (17.4, 15.5),
        ), fill=(0, 0, 0, 255))

        img.alpha_composite(overlay)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=60)
        return buf.getvalue()
    except Exception:
        return jpeg_bytes


@contextmanager
def record_session(page, output_dir: Path, session_id: Optional[str] = None, fps: int = 6):
    """Record the page as an mp4 via CDP screencast.

    Yields the Path of the mp4 being written.

    The sender thread runs at `fps` Hz, sampling the latest CDP JPEG and the
    current cursor position (_last_mouse_pos from lib.browse) each tick.
    This makes cursor movement visible even when Chrome doesn't repaint.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sid = (session_id or uuid.uuid4().hex)[:8]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    output_path = output_dir / f"{stamp}_{sid}.mp4"

    try:
        vp = page.viewport_size or {}
    except Exception:
        vp = {}
    _vp_w = float(vp.get("width", 1280))
    _vp_h = float(vp.get("height", 720))
    _cursor_scale = min(1280.0 / _vp_w, 720.0 / _vp_h, 1.0)

    ffmpeg = None
    cdp = None
    started = False
    frames_sent = [0]

    # Shared state between CDP callback and sender thread (GIL protects assignments)
    latest_jpeg: list[Optional[bytes]] = [None]
    running = threading.Event()

    def _sender_loop():
        interval = 1.0 / fps
        while running.is_set():
            t0 = time.time()
            jpeg = latest_jpeg[0]
            if (jpeg is not None
                    and ffmpeg is not None
                    and ffmpeg.poll() is None
                    and ffmpeg.stdin
                    and not ffmpeg.stdin.closed):
                try:
                    from lib.browse import get_cursor_pos
                    pos = get_cursor_pos()
                    if pos is None:
                        pos = (_vp_w / 2, _vp_h / 2)
                    frame = _draw_cursor_on_frame(
                        jpeg,
                        pos[0] * _cursor_scale,
                        pos[1] * _cursor_scale,
                    )
                except Exception:
                    frame = jpeg
                try:
                    ffmpeg.stdin.write(frame)
                    frames_sent[0] += 1
                except (BrokenPipeError, OSError):
                    break
            elapsed = time.time() - t0
            remaining = interval - elapsed
            if remaining > 0:
                time.sleep(remaining)

    sender_thread = threading.Thread(target=_sender_loop, daemon=True, name="recording-sender")

    # ── setup ─────────────────────────────────────────────────────────────────
    try:
        ffmpeg = subprocess.Popen([
            "ffmpeg", "-y",
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
                latest_jpeg[0] = base64.b64decode(event["data"])
            except Exception:
                pass
            try:
                cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
            except Exception:
                pass

        cdp.on("Page.screencastFrame", on_frame)

        # everyNthFrame=1 → capture every Chrome render; the sender thread
        # controls output fps independently so we don't flood ffmpeg.
        cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": 60,
            "maxWidth": 1280,
            "maxHeight": 720,
            "everyNthFrame": 1,
        })

        running.set()
        sender_thread.start()
        started = True
        print(f"[recording] started → {output_path}", file=sys.stderr)

    except Exception as e:
        print(f"[recording] setup failed: {type(e).__name__}: {e}", file=sys.stderr)
        running.clear()
        if ffmpeg is not None:
            try:
                if ffmpeg.stdin:
                    ffmpeg.stdin.close()
            except Exception:
                pass
            try:
                ffmpeg.terminate()
            except Exception:
                pass
            ffmpeg = None
        cdp = None

    # ── yield ─────────────────────────────────────────────────────────────────
    try:
        yield output_path
    finally:
        # Stop the sender thread first so it can't write after ffmpeg closes
        running.clear()
        if sender_thread.is_alive():
            sender_thread.join(timeout=3)

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
                f"(frames={frames_sent[0]}, size={output_path.stat().st_size if output_path.exists() else 0} bytes)",
                file=sys.stderr,
            )
