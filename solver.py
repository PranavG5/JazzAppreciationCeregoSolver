"""
Jazz Appreciation Cerego Solver – Offline Edition
==================================================
No API key required.  Decisions are made locally using a knowledge base
built from your own study materials (PDFs, flashcard text files, CSVs).

Setup (one time)
----------------
1. Install Tesseract 5:  https://github.com/UB-Mannheim/tesseract/wiki
2. pip install -r requirements.txt
3. python solver.py
4. Click  "Load Materials"  and select your PDF / flashcard files
5. Click  "Calibrate"  and follow the on-screen instructions
6. Navigate to your Cerego assignment in Chrome
7. Click  ▶ Start

Emergency stop
--------------
Move the mouse to the TOP-LEFT corner of the screen at any time.
"""

import json
import os
import time
import threading
import tkinter as tk
from tkinter import scrolledtext, filedialog, messagebox

import pyautogui

from knowledge   import KnowledgeBase
from ocr_engine  import (
    check_tesseract,
    find_answer_choices,
    detect_screen_state,
    ScreenState,
)

# ── Configuration ─────────────────────────────────────────────────────────────

CLICK_DELAY   = 1.2   # seconds to wait after clicking an answer
ADVANCE_DELAY = 0.9   # seconds to wait after clicking Know It / Got It
IDLE_DELAY    = 2.0   # seconds to wait when no question is detected
MAX_IDLE      = 8     # consecutive idle cycles before pausing

CALIB_FILE    = os.path.join(os.path.dirname(__file__), "calibration.json")

# Sensible defaults for a 1920×1080 screen at 100 % DPI.
# Used only when calibration.json is absent.
DEFAULT_CALIB = {
    "know_it_xy":      [960, 900],
    "got_it_xy":       [960, 900],
    "question_region": {"x": 480, "y": 180, "w": 960, "h": 200},
    "choices_region":  {"x": 360, "y": 400, "w": 1200, "h": 380},
}

pyautogui.FAILSAFE = True
pyautogui.PAUSE    = 0.25


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_calibration() -> dict:
    if os.path.isfile(CALIB_FILE):
        with open(CALIB_FILE) as f:
            return json.load(f)
    return dict(DEFAULT_CALIB)


def save_calibration(data: dict):
    with open(CALIB_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── GUI ───────────────────────────────────────────────────────────────────────

class SolverApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Jazz Cerego Solver  (Offline)")
        self.root.geometry("720x620")
        self.root.resizable(True, True)
        self.root.attributes("-topmost", True)

        self._kb    = KnowledgeBase()
        self._calib = load_calibration()
        self._stop_event = threading.Event()

        self._build_gui()

        # Check Tesseract on startup (non-blocking)
        self.root.after(300, self._check_tesseract_async)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.lift()
        self.root.focus_force()

    # ── GUI construction ───────────────────────────────────────────────────

    def _build_gui(self):
        # ── Materials row ──────────────────────────────────────────────────
        mat_frame = tk.Frame(self.root, padx=16, pady=10)
        mat_frame.pack(fill=tk.X)

        self.mat_label = tk.Label(
            mat_frame,
            text="No study materials loaded",
            anchor="w",
            fg="#888",
            font=("Segoe UI", 11),
            wraplength=420,
            justify="left",
        )
        self.mat_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Button(
            mat_frame,
            text="Load Materials",
            font=("Segoe UI", 11, "bold"),
            relief=tk.FLAT,
            bg="#2980b9", fg="white",
            padx=12, pady=4,
            command=self._load_materials,
        ).pack(side=tk.RIGHT, padx=(6, 0))

        # ── Calibration row ────────────────────────────────────────────────
        cal_frame = tk.Frame(self.root, padx=16, pady=4)
        cal_frame.pack(fill=tk.X)

        calib_status = "Calibration loaded" if os.path.isfile(CALIB_FILE) else "Not calibrated (using defaults)"
        calib_color  = "#27ae60" if os.path.isfile(CALIB_FILE) else "#e67e22"
        self.cal_label = tk.Label(
            cal_frame,
            text=calib_status,
            anchor="w",
            fg=calib_color,
            font=("Segoe UI", 11),
        )
        self.cal_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Button(
            cal_frame,
            text="Calibrate",
            font=("Segoe UI", 11, "bold"),
            relief=tk.FLAT,
            bg="#8e44ad", fg="white",
            padx=12, pady=4,
            command=self._calibrate,
        ).pack(side=tk.RIGHT, padx=(6, 0))

        # ── Start / Stop buttons ───────────────────────────────────────────
        btn_frame = tk.Frame(self.root, padx=16, pady=8)
        btn_frame.pack(fill=tk.X)

        self.start_btn = tk.Button(
            btn_frame,
            text="▶  Start",
            width=16,
            bg="#27ae60", fg="white",
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
            bg="#e74c3c", fg="white",
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
            self.root,
            textvariable=self.status_var,
            anchor="w",
            fg="#555",
            font=("Segoe UI", 11),
        ).pack(fill=tk.X, padx=16, pady=(0, 4))

        # ── Activity log ───────────────────────────────────────────────────
        self.log = scrolledtext.ScrolledText(
            self.root,
            height=15,
            state=tk.DISABLED,
            wrap=tk.WORD,
            font=("Consolas", 11),
            bg="#1e1e1e", fg="#d4d4d4",
        )
        self.log.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 14))

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

    def _click_on_chrome(self, x: int, y: int):
        """Temporarily drop topmost so Chrome receives the click."""
        self.root.attributes("-topmost", False)
        self.root.update()
        time.sleep(0.15)
        pyautogui.moveTo(x, y, duration=0.35)
        pyautogui.click()
        time.sleep(0.1)
        self.root.attributes("-topmost", True)

    def _check_tesseract_async(self):
        try:
            check_tesseract()
            self._log("Tesseract OCR: ready\n")
        except RuntimeError as e:
            self._log(f"WARNING — Tesseract not found:\n{e}\n")
            self._set_status("Tesseract missing — see log")

    # ── Start / Stop ───────────────────────────────────────────────────────

    def start(self):
        if self._kb.size() == 0:
            if not messagebox.askyesno(
                "No Materials",
                "No study materials loaded.\n\n"
                "The solver will still run but will guess answers.\n\n"
                "Continue anyway?",
            ):
                return
        self._stop_event.clear()
        self._set_buttons(running=True)
        self._set_status("Running…")
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop_event.set()
        self._log("— Stop requested —")

    def _on_close(self):
        self._stop_event.set()
        self.root.destroy()

    # ── Material loading ───────────────────────────────────────────────────

    def _load_materials(self):
        paths = filedialog.askopenfilenames(
            title="Select study materials",
            filetypes=[
                ("Supported files", "*.pdf *.txt *.csv *.tsv"),
                ("PDF",  "*.pdf"),
                ("Text", "*.txt *.tsv"),
                ("CSV",  "*.csv"),
                ("All",  "*.*"),
            ],
        )
        if not paths:
            return

        total = 0
        for path in paths:
            try:
                n = self._kb.add_file(path)
                self._log(f"Loaded {os.path.basename(path)} — {n} facts")
                total += n
            except Exception as exc:
                self._log(f"Error loading {os.path.basename(path)}: {exc}")

        sources = self._kb.file_sources()
        summary = f"{self._kb.size()} facts from {len(sources)} file(s)"
        self.mat_label.config(text=summary, fg="#27ae60")
        self._log(f"\nKnowledge base ready: {summary}\n")
        self._set_status(summary)

    # ── Calibration ────────────────────────────────────────────────────────

    def _calibrate(self):
        """
        Walk the user through recording button positions and screen regions.
        Runs in the GUI thread (blocks with after() scheduling so the window
        stays responsive) by using a simple state machine driven by spacebar.
        """
        # Disable start during calibration
        self.start_btn.config(state=tk.DISABLED)
        self._log("\n=== Calibration mode ===")
        self._log("Follow the instructions below, then press SPACE each time.\n")

        calib: dict = {}
        steps = [
            ("know_it_xy",
             "1/5  Hover over the  KNOW IT / GOT IT  button, then press SPACE."),
            ("question_tl",
             "2/5  Hover over the TOP-LEFT corner of the QUESTION text area, press SPACE."),
            ("question_br",
             "3/5  Hover over the BOTTOM-RIGHT corner of the QUESTION text area, press SPACE."),
            ("choices_tl",
             "4/5  Hover over the TOP-LEFT corner of the ANSWER CHOICES area, press SPACE."),
            ("choices_br",
             "5/5  Hover over the BOTTOM-RIGHT corner of the ANSWER CHOICES area, press SPACE."),
        ]

        step_idx = [0]   # mutable container so nested func can modify it

        def wait_for_space():
            key = [None]
            win = tk.Toplevel(self.root)
            win.title("Calibration")
            win.attributes("-topmost", True)
            win.resizable(True, True)

            lbl = tk.Label(
                win,
                text=steps[step_idx[0]][1],
                wraplength=600,
                font=("Segoe UI", 13),
                justify="center",
            )
            lbl.pack(expand=True, fill=tk.BOTH, padx=30, pady=30)

            prog = tk.Label(win, text="Press SPACE to record, or ESC to cancel.",
                            fg="#888", font=("Segoe UI", 11))
            prog.pack(pady=(0, 16))

            # Let tkinter calculate the required size, then set it
            win.update_idletasks()
            win.geometry(f"{win.winfo_reqwidth() + 60}x{win.winfo_reqheight() + 40}")

            def on_key(event):
                if event.keysym == "space":
                    key[0] = "space"
                    win.destroy()
                elif event.keysym == "Escape":
                    key[0] = "esc"
                    win.destroy()

            win.bind("<KeyPress>", on_key)
            win.focus_force()
            win.wait_window()
            return key[0]

        def do_step():
            if step_idx[0] >= len(steps):
                _finish()
                return

            k, prompt = steps[step_idx[0]]
            self._log(f"  {prompt}")

            result = wait_for_space()
            if result == "esc":
                self._log("Calibration cancelled.\n")
                self.start_btn.config(state=tk.NORMAL)
                return

            x, y = pyautogui.position()
            calib[k] = [x, y]
            self._log(f"    Recorded ({x}, {y})")
            step_idx[0] += 1
            self.root.after(100, do_step)

        def _finish():
            # Build the final calibration dict
            kx, ky = calib["know_it_xy"]
            q_tl = calib["question_tl"]
            q_br = calib["question_br"]
            c_tl = calib["choices_tl"]
            c_br = calib["choices_br"]

            result = {
                "know_it_xy": [kx, ky],
                "got_it_xy":  [kx, ky],   # same button
                "question_region": {
                    "x": q_tl[0], "y": q_tl[1],
                    "w": max(1, q_br[0] - q_tl[0]),
                    "h": max(1, q_br[1] - q_tl[1]),
                },
                "choices_region": {
                    "x": c_tl[0], "y": c_tl[1],
                    "w": max(1, c_br[0] - c_tl[0]),
                    "h": max(1, c_br[1] - c_tl[1]),
                },
            }
            save_calibration(result)
            self._calib = result
            self.cal_label.config(text="Calibration saved", fg="#27ae60")
            self._log("\nCalibration saved to calibration.json\n")
            self.start_btn.config(state=tk.NORMAL)

        self.root.after(100, do_step)

    # ── Solver loop ────────────────────────────────────────────────────────

    def _run(self):
        self._log("Solver started.  Switch to your Cerego tab.\n")

        # Check Tesseract is available before starting
        try:
            check_tesseract()
        except RuntimeError as e:
            self._log(f"Cannot start: {e}")
            self._set_buttons(running=False)
            self._set_status("Tesseract missing")
            return

        calib      = self._calib
        q_region   = calib["question_region"]
        c_region   = calib["choices_region"]
        know_it_xy = tuple(calib["know_it_xy"])
        got_it_xy  = tuple(calib["got_it_xy"])
        regions    = {"question": q_region, "choices": c_region}

        idle_count = 0

        while not self._stop_event.is_set():
            try:
                state = detect_screen_state(regions)

                if state == ScreenState.COMPLETE:
                    self._log("Assignment complete!")
                    self._set_status("Done — assignment complete")
                    break

                elif state == ScreenState.INFO_CARD:
                    idle_count = 0
                    self._log("[Info card] — clicking Got It")
                    self._click_on_chrome(*got_it_xy)
                    time.sleep(ADVANCE_DELAY)

                elif state == ScreenState.QUESTION:
                    idle_count = 0

                    # Read question text
                    from ocr_engine import ocr_region
                    question_text = ocr_region(q_region, psm=6)

                    # Find answer choices with their screen positions
                    choices = find_answer_choices(c_region)

                    if not choices:
                        self._log("  (question detected but no choices found — waiting)")
                        time.sleep(IDLE_DELAY)
                        continue

                    choice_texts = [c.text for c in choices]
                    best_text, confidence = self._kb.query(question_text, choice_texts)

                    # Find the AnswerChoice object that matches best_text
                    from rapidfuzz import fuzz, process as rfp
                    match = rfp.extractOne(
                        best_text, choice_texts, scorer=fuzz.token_set_ratio
                    )
                    if match:
                        idx = choice_texts.index(match[0])
                        chosen = choices[idx]
                    else:
                        chosen = choices[0]   # last-resort: first choice

                    conf_pct = f"{confidence:.0f}%"
                    warn     = "  [LOW CONFIDENCE — GUESS]" if confidence < 50 else ""
                    self._log(f"Q: {question_text[:80]}")
                    self._log(f"→ {chosen.text}  [confidence: {conf_pct}]{warn}")

                    self._click_on_chrome(chosen.cx, chosen.cy)
                    time.sleep(CLICK_DELAY)
                    self._click_on_chrome(*know_it_xy)
                    time.sleep(ADVANCE_DELAY)

                else:  # LOADING / OTHER
                    idle_count += 1
                    self._log(f"Waiting for question… (idle #{idle_count})")
                    if idle_count >= MAX_IDLE:
                        self._log(
                            f"\nPaused — no question found after {MAX_IDLE} attempts.\n"
                            "Manually advance the page, then click ▶ Start again.\n"
                        )
                        self._set_status("Paused — waiting for user")
                        break
                    time.sleep(IDLE_DELAY)

            except pyautogui.FailSafeException:
                self._log("Emergency stop (mouse moved to top-left corner).")
                self._set_status("Emergency stopped")
                break
            except Exception as exc:
                self._log(f"Error: {exc}\n  Retrying in 3 s…")
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
