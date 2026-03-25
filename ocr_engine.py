"""
ocr_engine.py — Screen capture and OCR for Jazz Cerego Solver
=============================================================
Uses mss for fast region capture and pytesseract for text recognition.
No API keys required.
"""

import os
from dataclasses import dataclass

from PIL import Image, ImageFilter, ImageOps

try:
    import mss
    _MSS_OK = True
except ImportError:
    _MSS_OK = False

try:
    import pytesseract
    # Honour env-var override first, then fall back to default Windows path
    _tess_cmd = os.environ.get(
        "TESSERACT_CMD",
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    )
    if os.path.isfile(_tess_cmd):
        pytesseract.pytesseract.tesseract_cmd = _tess_cmd
    _TESS_OK = True
except ImportError:
    _TESS_OK = False

TESSERACT_DOWNLOAD = "https://github.com/UB-Mannheim/tesseract/wiki"


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class AnswerChoice:
    text: str
    cx: int   # center x in logical screen coordinates
    cy: int   # center y in logical screen coordinates


class ScreenState:
    INFO_CARD = "info_card"
    QUESTION  = "question"
    COMPLETE  = "complete"
    LOADING   = "loading"


# ── Tesseract health-check ────────────────────────────────────────────────────

def check_tesseract() -> bool:
    """Return True if Tesseract is ready; raise RuntimeError with a helpful message if not."""
    if not _TESS_OK:
        raise RuntimeError(
            "pytesseract not installed.\n"
            "Run:  pip install pytesseract"
        )
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        raise RuntimeError(
            "Tesseract binary not found.\n\n"
            f"Download installer from:\n  {TESSERACT_DOWNLOAD}\n\n"
            "After installing, either:\n"
            "  • Accept the default path  (C:\\Program Files\\Tesseract-OCR\\tesseract.exe)\n"
            "  • Or set TESSERACT_CMD environment variable to the correct path."
        )


# ── Screen capture ────────────────────────────────────────────────────────────

def capture_region(region: dict) -> Image.Image:
    """Capture screen region {x, y, w, h} → PIL Image (RGB)."""
    if _MSS_OK:
        with mss.mss() as sct:
            mon = {
                "left":   region["x"],
                "top":    region["y"],
                "width":  region["w"],
                "height": region["h"],
            }
            raw = sct.grab(mon)
            return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    # Fall back to pyautogui if mss is unavailable
    import pyautogui
    full = pyautogui.screenshot()
    return full.crop((
        region["x"],
        region["y"],
        region["x"] + region["w"],
        region["y"] + region["h"],
    ))


# ── Image preprocessing ───────────────────────────────────────────────────────

def _preprocess(img: Image.Image) -> Image.Image:
    """Grayscale → 2× upscale → sharpen → binarize for Tesseract accuracy."""
    img = ImageOps.grayscale(img)
    w, h = img.size
    img = img.resize((w * 2, h * 2), Image.LANCZOS)
    img = img.filter(ImageFilter.SHARPEN)
    # Binarize: white background, dark text
    img = img.point(lambda p: 255 if p > 140 else 0, "L")
    return img


# ── OCR helpers ───────────────────────────────────────────────────────────────

def ocr_region(region: dict, psm: int = 6) -> str:
    """Capture a region and return OCR'd text, stripped."""
    img = _preprocess(capture_region(region))
    cfg = f"--psm {psm} --oem 3"
    return pytesseract.image_to_string(img, config=cfg).strip()


def find_answer_choices(region: dict) -> list[AnswerChoice]:
    """
    OCR the answer-choices region with word-level bounding boxes.
    Clusters nearby words into choice blocks and returns their screen-space
    center coordinates.

    region: dict with keys x, y, w, h (logical screen coords)
    """
    img     = capture_region(region)
    proc    = _preprocess(img)   # 2× upscaled
    scale   = 2                  # undo upscale when converting coords

    data = pytesseract.image_to_data(
        proc,
        config="--psm 11 --oem 3",
        output_type=pytesseract.Output.DICT,
    )

    # Collect confident word-level bounding boxes
    entries = []
    for i in range(len(data["text"])):
        word = data["text"][i].strip()
        conf = int(data["conf"][i])
        if not word or conf < 30:
            continue
        x = data["left"][i]  // scale
        y = data["top"][i]   // scale
        w = data["width"][i] // scale
        h = data["height"][i] // scale
        entries.append({"text": word, "x": x, "y": y, "w": w, "h": h,
                         "cy": y + h // 2})

    if not entries:
        return []

    # --- Step 1: cluster words into lines by vertical proximity (≤ 20 px) ---
    entries.sort(key=lambda e: e["cy"])
    lines: list[list[dict]] = []
    cur_line = [entries[0]]
    for e in entries[1:]:
        if abs(e["cy"] - cur_line[-1]["cy"]) <= 20:
            cur_line.append(e)
        else:
            lines.append(cur_line)
            cur_line = [e]
    lines.append(cur_line)

    # --- Step 2: merge nearby lines into choice blocks (gap ≤ 50 px) ---
    if not lines:
        return []

    blocks: list[list[list[dict]]] = []
    cur_block = [lines[0]]
    for line in lines[1:]:
        prev_cy = sum(e["cy"] for e in cur_block[-1]) / len(cur_block[-1])
        curr_cy = sum(e["cy"] for e in line)          / len(line)
        if curr_cy - prev_cy <= 50:
            cur_block.append(line)
        else:
            blocks.append(cur_block)
            cur_block = [line]
    blocks.append(cur_block)

    # --- Step 3: build AnswerChoice from each block ---
    choices: list[AnswerChoice] = []
    for block in blocks:
        all_words = [e for line in block for e in line]
        text = " ".join(e["text"] for e in all_words).strip()
        if not text:
            continue

        min_x = min(e["x"]           for e in all_words)
        max_x = max(e["x"] + e["w"]  for e in all_words)
        min_y = min(e["y"]           for e in all_words)
        max_y = max(e["y"] + e["h"]  for e in all_words)

        # Convert from image-local coords → logical screen coords
        cx_screen = region["x"] + (min_x + max_x) // 2
        cy_screen = region["y"] + (min_y + max_y) // 2
        choices.append(AnswerChoice(text=text, cx=cx_screen, cy=cy_screen))

    return choices


# ── Screen-state classifier ───────────────────────────────────────────────────

def detect_screen_state(regions: dict) -> str:
    """
    OCR key screen areas to classify the current Cerego screen.
    Returns one of the ScreenState constants.

    regions must contain keys: 'question' and 'choices', each a {x,y,w,h} dict.
    """
    question_text = ocr_region(regions["question"], psm=6).lower()

    complete_kw = [
        "well done", "assignment complete", "finished",
        "you completed", "great job", "all done",
    ]
    if any(kw in question_text for kw in complete_kw):
        return ScreenState.COMPLETE

    choices_text = ocr_region(regions["choices"], psm=11).lower().strip()

    if choices_text and len(choices_text) > 5:
        return ScreenState.QUESTION

    if question_text and len(question_text) > 5:
        return ScreenState.INFO_CARD

    return ScreenState.LOADING
