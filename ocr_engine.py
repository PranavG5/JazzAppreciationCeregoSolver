"""
ocr_engine.py — Screen capture and OCR for Jazz Cerego Solver
=============================================================
Uses mss for fast region capture and pytesseract for text recognition.
No API keys required.
"""

import os
import re
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


# ── Button label reader ───────────────────────────────────────────────────────

def read_button_label(button_xy: tuple) -> str:
    """
    Detect whether the bottom-right button says 'Got It' or 'Know It'
    by scanning pixel colors — no OCR needed.

    The green button is always present on both page types.
    The distinguishing signal is whether a second ('Don't Know It') button
    sits to its left — that only appears on question pages.

    Returns:
      'got_it'   — green button only (info card)
      'know_it'  — green button + second button to its left (question)
      'unknown'  — no green button at all (choose-choice page / loading)
    """
    x, y = button_xy

    # ── 1. Scan a generous region around the calibrated button ────────────────
    btn_region = {"x": max(0, x - 110), "y": max(0, y - 40), "w": 220, "h": 80}
    img        = capture_region(btn_region).convert("RGB")
    w, h       = img.size

    green_count = 0
    total       = 0
    for py in range(0, h, 3):
        for px in range(0, w, 3):
            r, g, b = img.getpixel((px, py))
            total += 1
            # Cerego green: G clearly above R and B, not too dark
            if g > r + 15 and g > b + 15 and g > 80:
                green_count += 1

    green_ratio = green_count / total if total else 0

    # Threshold 0.05 — even a partially visible green button qualifies
    if green_ratio < 0.05:
        return "unknown"   # no green button → choose-choice page or loading

    # ── 2. Check for 'Don't Know It' button to the left ───────────────────────
    # On question pages a second white/grey button sits ~150-250 px left.
    # On info-card pages only the green button exists.
    left_region = {"x": max(0, x - 320), "y": max(0, y - 40), "w": 180, "h": 80}
    left_img    = capture_region(left_region).convert("RGB")
    lw, lh      = left_img.size

    dark_count = 0
    ltotal     = 0
    for py in range(0, lh, 3):
        for px in range(0, lw, 3):
            r, g, b = left_img.getpixel((px, py))
            ltotal += 1
            brightness = (r + g + b) / 3
            # A button has visible borders / fill that isn't pure white
            if brightness < 230:
                dark_count += 1

    left_ratio = dark_count / ltotal if ltotal else 0

    if left_ratio > 0.10:
        return "know_it"   # second button present → question page
    return "got_it"        # only the green button → info card


# ── Post-answer feedback detector ─────────────────────────────────────────────

def detect_answer_feedback(regions: dict) -> tuple[str, str]:
    """
    After an answer is clicked, detect Cerego's visual feedback by scanning
    the choices region for colored highlight boxes.

    Cerego shows:
      ✓  Green box  = the CORRECT answer
      ✗  Red/pink box = the WRONG answer that was clicked (only when incorrect)

    Logic:
      green only  → verdict "correct"   (we clicked the green box)
      green + red → verdict "incorrect" (red = us, green = right answer)
      neither     → verdict "unknown"

    Returns (verdict, correct_answer_text) where correct_answer_text is the
    OCR'd text of the green-highlighted choice (empty if not found).
    """
    img     = capture_region(regions["choices"])
    img_rgb = img.convert("RGB")
    w, h    = img_rgb.size

    # ── Step 1: scan every 3rd row for green / red signatures ─────────────────
    # Green box:     R~185-215  G~225-245  B~175-210  (G clearly dominant)
    # Red/pink box:  R~235-250  G~180-205  B~180-205  (R clearly dominant)
    # Grey (normal): all channels roughly equal and high (>220)

    row_green = []   # y values with a green signature
    row_red   = []   # y values with a red/pink signature

    for y in range(0, h, 3):
        r_sum = g_sum = b_sum = n = 0
        for x in range(0, w, 8):
            r, g, b = img_rgb.getpixel((x, y))
            r_sum += r; g_sum += g; b_sum += b; n += 1
        if n == 0:
            continue
        ra, ga, ba = r_sum / n, g_sum / n, b_sum / n

        # Green: G > R and G > B by a clear margin, background is light
        if ga > ra + 12 and ga > ba + 12 and ga > 190:
            row_green.append(y)
        # Red/pink: R > G and R > B by a clear margin, background is light
        elif ra > ga + 18 and ra > ba + 18 and ra > 210:
            row_red.append(y)

    # ── Step 2: merge adjacent rows into bands ─────────────────────────────────
    green_band = _rows_to_band(row_green, gap=18)
    red_band   = _rows_to_band(row_red,   gap=18)

    # ── Step 3: determine verdict ──────────────────────────────────────────────
    if green_band and red_band:
        verdict = "incorrect"
    elif green_band:
        verdict = "correct"
    else:
        return "unknown", ""

    # ── Step 4: OCR the green band to extract the correct answer text ──────────
    correct_answer = ""
    if green_band:
        y1, y2  = green_band
        # Add a small margin so we don't clip ascenders/descenders
        y1 = max(0, y1 - 6)
        y2 = min(h, y2 + 6)
        band_img = img.crop((0, y1, w, y2))
        band_img = _preprocess(band_img)
        text = pytesseract.image_to_string(
            band_img, config="--psm 7 --oem 3"
        ).strip()
        # Strip checkmark glyphs and OCR noise — keep printable text
        text = re.sub(r"[^\w\s/\-\(\)\.,']", "", text).strip()
        correct_answer = text

    return verdict, correct_answer


def _rows_to_band(rows: list[int], gap: int = 18) -> tuple[int, int] | None:
    """
    Merge a sorted list of row y-values into contiguous bands (runs where
    consecutive values differ by ≤ gap).  Return the largest band as
    (y_start, y_end), or None if the list is empty.
    """
    if not rows:
        return None

    bands: list[tuple[int, int]] = []
    start = rows[0]
    prev  = rows[0]

    for y in rows[1:]:
        if y - prev > gap:
            bands.append((start, prev))
            start = y
        prev = y
    bands.append((start, prev))

    return max(bands, key=lambda b: b[1] - b[0])
