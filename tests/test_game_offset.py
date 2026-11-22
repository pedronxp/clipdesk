import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from cut import GAME_H, even, game_offset


def test_game_offset_accepts_a_fractional_cam_box(tmp_path):
    """Regression: cam_box is [x, y, w, h] fractions; the old code reused w/h as the
    panel size, so `range(0, gw - w + 1, 8)` received a float and react clips fell back
    to blur with 'float object cannot be interpreted as an integer'."""
    video = tmp_path / "src.avi"
    w, h = 1280, 720
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 30, (w, h))
    assert writer.isOpened(), "OpenCV could not open the MJPG writer"

    for i in range(40):
        frame = np.full((h, w, 3), 40, np.uint8)
        cv2.rectangle(frame, (0, 0), (300, 180), (200, 200, 200), -1)  # burned-in cam
        cv2.putText(frame, f"LIVE {i}", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
        cv2.rectangle(frame, (400 + i * 3, 300), (600 + i * 3, 500), (0, 120, 255), -1)
        writer.write(frame)
    writer.release()

    gw = even(w * GAME_H / h)
    out = game_offset(video, 0, 1.2, (w, h), [0.0, 0.03, 0.23, 0.23])

    assert isinstance(out, int)
    assert 0 <= out <= gw - 1080
