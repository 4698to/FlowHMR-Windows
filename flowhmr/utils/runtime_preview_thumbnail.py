"""Small local thumbnails for the job history."""

import math
from pathlib import Path
from threading import Lock


_decode_lock = Lock()


def _pillow_first_frame(video):
    """First frame of images OpenCV cannot open, e.g. an animated WebP."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(str(video)) as image:
            image.seek(0)
            return np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
    except Exception:
        return None


def ensure_thumbnail(video, thumbnail):
    import cv2

    path = Path(thumbnail)
    if path.is_file():
        return True
    with _decode_lock:
        if path.is_file():
            return True
        capture = cv2.VideoCapture(str(video))
        try:
            if capture.isOpened():
                count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
                if math.isfinite(count) and count > 4:
                    capture.set(cv2.CAP_PROP_POS_FRAMES, min(int(count * 0.18), 90))
                ok, frame = capture.read()
                if not ok:
                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, frame = capture.read()
            else:
                ok, frame = False, None
        finally:
            capture.release()
        if not ok or frame is None:
            frame = _pillow_first_frame(video)
        if frame is None:
            return False
        height, width = frame.shape[:2]
        scale = min(1, 200 / max(height, width))
        frame = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))),
                           interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 83])
        if not ok:
            return False
        pending = path.with_suffix(".jpg.part")
        pending.write_bytes(encoded.tobytes())
        pending.replace(path)
        return True
