"""Plate-like OCR smoke-test images for the in-Sandbox Tesseract check (1.3.4 LPR parity).

    <python with opencv> installer/windows/tesseract-build/make-ocr-test-images.py <inputs folder>

Writes <inputs>/ocr-tests/<n>.png + <n>.txt (the expected text): dark characters on a light plate, 3x the height of a
real 1080p plate crop, the input app/lpr.py hands Tesseract after its own preprocessing. Synthetic text only: real
plates come from the owner-approved clip set.
"""
import sys
from pathlib import Path

import cv2
import numpy as np

PLATES = ["ABC1234", "7XYZ482", "KDN5021", "HT88K2P", "9MPR317"]


def render(text: str):
    font, scale, thickness = cv2.FONT_HERSHEY_DUPLEX, 3.0, 6
    (width, height), baseline = cv2.getTextSize(text, font, scale, thickness)
    image = np.full((height + baseline + 60, width + 80, 3), 235, np.uint8)
    cv2.putText(image, text, (40, height + 30), font, scale, (20, 20, 20), thickness, cv2.LINE_AA)
    return image


def main(out: str) -> int:
    folder = Path(out) / "ocr-tests"
    folder.mkdir(parents=True, exist_ok=True)
    for index, text in enumerate(PLATES, 1):
        assert cv2.imwrite(str(folder / f"plate{index}.png"), render(text))
        (folder / f"plate{index}.txt").write_text(text + "\n", encoding="ascii")
    print(f"{len(PLATES)} test images in {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
