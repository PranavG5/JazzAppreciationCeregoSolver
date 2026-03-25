"""
Jazz Appreciation Cerego Solver – GUI Edition
=============================================
A floating control panel that stays on top of all windows.

Usage
-----
1. python solver.py
2. Enter your Anthropic API key in the field (saved for the session)
3. Navigate to your Cerego assignment in Chrome
4. Click  ▶ Start
5. Click  ■ Stop  or move mouse to the TOP-LEFT corner for emergency stop

The window stays on top of Chrome so you can always reach the Stop button.
"""

import base64
import io
import json
import os
import time
import threading
import tkinter as tk
from tkinter import scrolledtext, filedialog

import anthropic
import pyautogui
from PIL import Image

try:
    import pdfplumber
    _PDF_AVAILABLE = True
except ImportError:
    _PDF_AVAILABLE = False

# ── Configuration ─────────────────────────────────────────────────────────────
CLICK_DELAY   = 1.2   # wait after clicking an answer  (seconds)
ADVANCE_DELAY = 0.9   # wait after clicking Continue   (seconds)
IDLE_DELAY    = 2.0   # wait when no question found    (seconds)
MAX_IDLE      = 8     # consecutive idle cycles before pausing

pyautogui.FAILSAFE = True   # move mouse to top-left corner → emergency stop
pyautogui.PAUSE    = 0.25

# ── Claude prompts ────────────────────────────────────────────────────────────

ANSWER_PROMPT_BASE = """\
You are an expert assistant solving Cerego flashcard assignments for a Jazz Appreciation course.

{knowledge_section}

You will receive a screenshot of the Cerego quiz interface.

Cerego has two distinct screen types — you MUST identify which type this is:

1. INFO CARD: The screen is simply presenting a fact to memorize (e.g. "Louis Armstrong - Trumpet").
   There is NO question being asked. The only action button is "Got It" (usually bottom-right).
   → Set page_type = "info_card" and set click_x/click_y to the center of the "Got It" button.

2. QUESTION: The screen asks the user something (e.g. "Can you name an album by this artist?",
   multiple-choice answers, fill-in-the-blank, matching, etc.).
   The advance button is "Know It" (bottom-right), NOT "Got It".
   → Set page_type = "question", identify the CORRECT ANSWER option and set click_x/click_y
     to the center of that answer choice. Prefer course material; fall back to general jazz knowledge.

Return ONLY a JSON object — no markdown fences, no explanation outside the JSON:
{{
  "page_type": "info_card" | "question" | "other" | null,
  "assignment_complete": true | false,
  "question_text": "<the question text or fact label, or null>",
  "correct_answer": "<text of the correct answer to click, or the 'Got It' label for info cards, or null>",
  "click_x": <integer pixel x-coordinate to click, or null>,
  "click_y": <integer pixel y-coordinate to click, or null>,
  "reasoning": "<one-sentence explanation>"
}}

Rules:
- Coordinates are measured from the top-left of the FULL screenshot.
- click_x/click_y must be the CENTER of the button or answer choice to click.
- If the assignment is complete (score screen, "Well done!"), set assignment_complete=true.
- If a loading screen or non-quiz content is shown, set page_type="other" and click_x=null.
- For matching questions return ONE click per response (the next unmatched item).
"""

def build_answer_prompt(course_text: str) -> str:
    if course_text:
        knowledge_section = (
            "=== COURSE SLIDES (use these as your primary reference) ===\n"
            + course_text[:12000]   # stay well within token limits
            + "\n=== END COURSE SLIDES ==="
        )
    else:
        knowledge_section = (
            "No course slides loaded — using general jazz knowledge."
        )
    return ANSWER_PROMPT_BASE.format(knowledge_section=knowledge_section)

NEXT_PROMPT = """\
You are helping a solver advance through Cerego flashcard screens after an answer has been clicked.

Look at the screenshot and find the button that moves to the next card. It is usually near the
bottom-center of the screen and may be labeled: "Continue", "Next", "Got it", "Keep studying",
"I knew it", "I didn't know it", or shown as a right-arrow ▶.

Return ONLY a JSON object — no markdown, no extra text:
{
  "button_found": true | false,
  "button_label": "<label text or null>",
  "click_x": <integer x or null>,
  "click_y": <integer y or null>
}
"""

# ── Screenshot helper ─────────────────────────────────────────────────────────

def screenshot_b64() -> str:
    """Full-screen screenshot → base64-encoded PNG string."""
    img: Image.Image = pyautogui.screenshot()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


# ── Claude call helper ────────────────────────────────────────────────────────

def call_claude(client: anthropic.Anthropic, system: str, img_b64: str) -> dict:
    """Send screenshot to Claude with the given system prompt; return parsed dict."""
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=512,
        system=system,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": img_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Analyze this screenshot and return the JSON response.",
                    },
                ],
            }
        ],
    )

    raw = ""
    for block in response.content:
        if block.type == "text":
            raw = block.text.strip()
            break

    # Strip accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if "```" in raw:
            raw = raw[: raw.rfind("```")]

    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        return {}


# ── GUI ───────────────────────────────────────────────────────────────────────

class SolverApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Jazz Cerego Solver")
        self.root.geometry("720x600")
        self.root.resizable(True, True)
        self.root.attributes("-topmost", True)   # float above Chrome

        self._course_text: str = ""   # extracted PDF text

        # ── API key ────────────────────────────────────────────────────────
        key_frame = tk.Frame(root, padx=16, pady=12)
        key_frame.pack(fill=tk.X)
        tk.Label(key_frame, text="API Key:", width=9, anchor="w", font=("Segoe UI", 12)).pack(side=tk.LEFT)
        self.api_var = tk.StringVar(value=os.environ.get("ANTHROPIC_API_KEY", ""))
        tk.Entry(key_frame, textvariable=self.api_var, show="*", font=("Segoe UI", 12)).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )

        # ── PDF loader ─────────────────────────────────────────────────────
        pdf_frame = tk.Frame(root, padx=16, pady=6)
        pdf_frame.pack(fill=tk.X)
        _pdf_hint = "No slides loaded (optional)" if _PDF_AVAILABLE else "Install pdfplumber to enable PDF slides"
        self.pdf_label = tk.Label(
            pdf_frame, text=_pdf_hint, anchor="w",
            fg="#888", font=("Segoe UI", 11), wraplength=400, justify="left"
        )
        self.pdf_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.pdf_btn = tk.Button(
            pdf_frame,
            text="Load PDF",
            font=("Segoe UI", 11, "bold"), relief=tk.FLAT,
            bg="#2980b9", fg="white", padx=12, pady=4,
            command=self._load_pdf,
        )
        self.pdf_btn.pack(side=tk.RIGHT, padx=(6, 0))

        # ── Buttons ────────────────────────────────────────────────────────
        btn_frame = tk.Frame(root, padx=16, pady=8)
        btn_frame.pack(fill=tk.X)

        self.start_btn = tk.Button(
            btn_frame,
            text="▶  Start",
            width=16,
            bg="#27ae60",
            fg="white",
            font=("Segoe UI", 13, "bold"),
            relief=tk.FLAT,
            padx=10, pady=8,
            command=self.start,
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 12))

        self.stop_btn = tk.Button(
            btn_frame,
            text="■  Stop",
            width=16,
            bg="#e74c3c",
            fg="white",
            font=("Segoe UI", 13, "bold"),
            relief=tk.FLAT,
            padx=10, pady=8,
            state=tk.DISABLED,
            command=self.stop,
        )
        self.stop_btn.pack(side=tk.LEFT)

        # ── Status bar ─────────────────────────────────────────────────────
        self.status_var = tk.StringVar(
            value="Ready  •  Move mouse to top-left corner to emergency-stop"
        )
        tk.Label(
            root,
            textvariable=self.status_var,
            anchor="w",
            fg="#555",
            font=("Segoe UI", 11),
        ).pack(fill=tk.X, padx=16, pady=(0, 4))

        # ── Log ────────────────────────────────────────────────────────────
        self.log = scrolledtext.ScrolledText(
            root,
            height=15,
            state=tk.DISABLED,
            wrap=tk.WORD,
            font=("Consolas", 11),
            bg="#1e1e1e",
            fg="#d4d4d4",
        )
        self.log.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 14))

        self._stop_event = threading.Event()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Force window to front
        self.root.lift()
        self.root.focus_force()

    # ── Internal helpers ───────────────────────────────────────────────────

    def _log(self, msg: str):
        def _do():
            self.log.config(state=tk.NORMAL)
            self.log.insert(tk.END, msg + "\n")
            self.log.see(tk.END)
            self.log.config(state=tk.DISABLED)
        self.root.after(0, _do)

    def _set_status(self, msg: str):
        self.root.after(0, lambda: self.status_var.set(msg))

    def _set_buttons(self, running: bool):
        def _do():
            self.start_btn.config(state=tk.DISABLED if running else tk.NORMAL)
            self.stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)
        self.root.after(0, _do)

    # ── Start / Stop ───────────────────────────────────────────────────────

    def start(self):
        api_key = self.api_var.get().strip()
        if not api_key:
            self._log("⚠  Please enter your Anthropic API key first.")
            return
        self._stop_event.clear()
        self._set_buttons(running=True)
        self._set_status("Running…")
        threading.Thread(target=self._run, args=(api_key,), daemon=True).start()

    def stop(self):
        self._stop_event.set()
        self._log("— Stop requested —")

    def _on_close(self):
        self._stop_event.set()
        self.root.destroy()

    def _load_pdf(self):
        if not _PDF_AVAILABLE:
            self._log("⚠  pdfplumber not installed.")
            self._log("   Run:  python -m pip install pdfplumber")
            self._log("   Then restart the solver.\n")
            self.pdf_label.config(text="Run: python -m pip install pdfplumber", fg="#e74c3c")
            return
        path = filedialog.askopenfilename(
            title="Select course slides PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            pages = []
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        pages.append(text)
            self._course_text = "\n\n".join(pages)
            short = os.path.basename(path)
            self.pdf_label.config(
                text=f"Slides loaded: {short}  ({len(pages)} pages)", fg="#27ae60"
            )
            self._log(f"Loaded PDF: {short} — {len(pages)} pages of course content.\n")
        except Exception as exc:
            self.pdf_label.config(text=f"Error loading PDF: {exc}", fg="#e74c3c")
            self._log(f"PDF load error: {exc}")

    def _click_on_chrome(self, x: int, y: int):
        """Temporarily drop topmost so Chrome receives the click."""
        self.root.attributes("-topmost", False)
        self.root.update()
        time.sleep(0.15)
        pyautogui.moveTo(x, y, duration=0.35)
        pyautogui.click()
        time.sleep(0.1)
        self.root.attributes("-topmost", True)

    # ── Solver loop ────────────────────────────────────────────────────────

    def _run(self, api_key: str):
        client = anthropic.Anthropic(api_key=api_key)
        idle_count = 0

        self._log("Solver started.  Switch to your Cerego tab.\n")

        while not self._stop_event.is_set():
            try:
                answer_prompt = build_answer_prompt(self._course_text)

                # ── Phase 1: Find the question and click the correct answer ──
                img = screenshot_b64()
                action = call_claude(client, answer_prompt, img)

                if action.get("assignment_complete"):
                    self._log("✅  Assignment complete!")
                    self._set_status("Done — assignment complete")
                    break

                page_type = action.get("page_type")
                if page_type in ("info_card", "question") and action.get("click_x") is not None:
                    idle_count = 0
                    q_text = action.get("question_text", "")
                    answer = action.get("correct_answer", "")
                    reason = action.get("reasoning", "")
                    cx, cy = int(action["click_x"]), int(action["click_y"])

                    if page_type == "info_card":
                        self._log(f"[Info card] {q_text}")
                        self._log(f"  → clicking Got It at ({cx}, {cy})\n")
                        self._click_on_chrome(cx, cy)
                        time.sleep(ADVANCE_DELAY)
                        # Info cards advance themselves — no NEXT_PROMPT needed

                    else:  # question
                        self._log(f"Q: {q_text}")
                        self._log(f"→ {answer}  [{reason}]")
                        self._log(f"  clicking ({cx}, {cy})")
                        self._click_on_chrome(cx, cy)
                        time.sleep(CLICK_DELAY)

                        # ── Phase 2: Find and click Know It / Continue ──────
                        img2 = screenshot_b64()
                        nxt  = call_claude(client, NEXT_PROMPT, img2)

                        if nxt.get("button_found") and nxt.get("click_x") is not None:
                            nx, ny = int(nxt["click_x"]), int(nxt["click_y"])
                            label  = nxt.get("button_label") or "Next"
                            self._log(f'  → "{label}" button at ({nx}, {ny})\n')
                            self._click_on_chrome(nx, ny)
                            time.sleep(ADVANCE_DELAY)
                        else:
                            self._log("  (no Continue button found — waiting)\n")
                            time.sleep(IDLE_DELAY)

                else:
                    idle_count += 1
                    self._log(f"No question detected (idle #{idle_count})")
                    if idle_count >= MAX_IDLE:
                        self._log(
                            "\n⚠  Paused — no question found after "
                            f"{MAX_IDLE} attempts.\n"
                            "   Manually advance the page, then click ▶ Start again.\n"
                        )
                        self._set_status("Paused — waiting for user")
                        break
                    time.sleep(IDLE_DELAY)

            except pyautogui.FailSafeException:
                self._log("⛔  Emergency stop (mouse moved to top-left corner).")
                self._set_status("Emergency stopped")
                break
            except anthropic.APIError as exc:
                self._log(f"API error: {exc}\n  Retrying in 5 s…")
                time.sleep(5)
            except Exception as exc:
                self._log(f"Unexpected error: {exc}\n  Retrying in 3 s…")
                time.sleep(3)

        self._set_buttons(running=False)
        if not self.status_var.get().startswith("Done"):
            self._set_status("Stopped")
        self._log("Solver stopped.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    SolverApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
