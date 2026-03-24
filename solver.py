"""
Jazz Appreciation Cerego Solver
================================
Automatically solves Cerego assignments for Jazz Appreciation by:
  1. Taking a screenshot
  2. Sending it to Claude (vision) to identify the question and correct answer
  3. Moving the mouse to the answer and clicking it
  4. Repeating until the assignment is complete

Controls
--------
  Press  Ctrl+Q  at any time to exit safely.
  Press  Ctrl+S  to start (or re-start) the solver.

Setup
-----
  pip install -r requirements.txt
  export ANTHROPIC_API_KEY="your-key-here"
  python solver.py
"""

import base64
import io
import json
import os
import sys
import time
import threading

import anthropic
import pyautogui
import keyboard
from PIL import Image

# ── Configuration ────────────────────────────────────────────────────────────

# How long to wait after each click before taking the next screenshot (seconds)
CLICK_DELAY = 1.5

# How long to wait when the solver sees no actionable question (seconds)
IDLE_DELAY = 2.0

# Maximum consecutive "no question found" responses before pausing and asking
# the user to manually advance (prevents infinite spin on completion screens)
MAX_IDLE_COUNT = 10

# PyAutoGUI safety margin (pixels) – moves cursor to corner to abort if panicked
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.3   # small pause between pyautogui calls

# ── Globals ──────────────────────────────────────────────────────────────────

running = False          # solver loop active
stop_event = threading.Event()

client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from env


# ── Helpers ──────────────────────────────────────────────────────────────────

def screenshot_b64() -> str:
    """Take a full-screen screenshot and return it as a base64-encoded PNG."""
    img: Image.Image = pyautogui.screenshot()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


SYSTEM_PROMPT = """
You are an expert assistant that helps solve Cerego assignments for a Jazz Appreciation course.

The user will send you a screenshot of the Cerego quiz interface. Your job is to:

1. Identify whether an actionable question is currently displayed.
2. Determine the correct answer using your knowledge of jazz music history.
3. Return ONLY a JSON object (no markdown, no explanation outside JSON) with the following schema:

{
  "question_visible": true | false,
  "question_type": "multiple_choice" | "matching" | "listening" | "other" | null,
  "question_text": "<the question text, or null>",
  "correct_answer": "<text of the correct answer option, or null>",
  "click_x": <integer pixel x-coordinate to click, or null>,
  "click_y": <integer pixel y-coordinate to click, or null>,
  "assignment_complete": true | false,
  "reasoning": "<brief explanation of your answer choice>"
}

Question types you may encounter:
- multiple_choice: Click the option that correctly answers the question about jazz.
- matching / connect: Two columns; you may need to click the left item first, then the right item.
  Return one pair per response (the first unmatched pair).
- listening (Deep Listening): A short audio clip plays; click the matching genre/era/artist.
- other: Any other interactive element.

Rules:
- Use your deep knowledge of jazz history, artists, instruments, eras, and styles.
- If you can see that the assignment is already complete (e.g. a "Well done!" or results screen),
  set assignment_complete to true and question_visible to false.
- If the screen shows a loading spinner, blank page, or non-quiz content, set question_visible
  to false and assignment_complete to false.
- The click coordinates should be the CENTER of the answer button/option you want to click.
  Estimate from the screenshot dimensions (typically 1920x1080 or similar).
- For matching questions, return the coordinates for ONE click at a time (the next unmatched item
  to click). The solver will call you again after each click to get the next click.
- Provide coordinates even if you are not 100% certain; wrong answers have no penalty.
"""


def ask_claude(img_b64: str) -> dict:
    """Send the screenshot to Claude and get back a parsed action dict."""
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
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
                        "text": (
                            "Please analyze this Cerego screenshot and return the JSON action "
                            "object as described in your instructions."
                        ),
                    },
                ],
            }
        ],
    )

    # Extract the text block (thinking blocks may precede it)
    raw_text = ""
    for block in response.content:
        if block.type == "text":
            raw_text = block.text
            break

    # Strip any accidental markdown fences
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[-1]
        if raw_text.endswith("```"):
            raw_text = raw_text[: raw_text.rfind("```")]

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        print(f"[WARN] Could not parse JSON from Claude response:\n{raw_text}")
        return {
            "question_visible": False,
            "assignment_complete": False,
            "click_x": None,
            "click_y": None,
        }


def do_click(x: int, y: int) -> None:
    """Move to (x, y) and left-click."""
    pyautogui.moveTo(x, y, duration=0.4)
    pyautogui.click()


# ── Solver loop ───────────────────────────────────────────────────────────────

def solver_loop() -> None:
    global running
    idle_count = 0

    print("\n[Solver] Started. Watching the screen…")
    print("[Solver] Move mouse to the top-left corner to trigger PyAutoGUI failsafe.\n")

    while not stop_event.is_set():
        try:
            img_b64 = screenshot_b64()
            action = ask_claude(img_b64)

            q_type    = action.get("question_type")
            q_text    = action.get("question_text", "")
            answer    = action.get("correct_answer", "")
            reasoning = action.get("reasoning", "")
            cx        = action.get("click_x")
            cy        = action.get("click_y")
            complete  = action.get("assignment_complete", False)
            visible   = action.get("question_visible", False)

            if complete:
                print("\n[Solver] ✅  Assignment complete! Stopping.")
                running = False
                stop_event.set()
                break

            if visible and cx is not None and cy is not None:
                idle_count = 0
                print(f"[Solver] Q ({q_type}): {q_text}")
                print(f"[Solver] → Answer: {answer}")
                print(f"[Solver] → Reason: {reasoning}")
                print(f"[Solver] → Clicking ({cx}, {cy})\n")
                do_click(int(cx), int(cy))
                time.sleep(CLICK_DELAY)
            else:
                idle_count += 1
                print(f"[Solver] No actionable question detected (idle #{idle_count})…")

                if idle_count >= MAX_IDLE_COUNT:
                    print(
                        "\n[Solver] ⚠️  No question found after "
                        f"{MAX_IDLE_COUNT} consecutive attempts.\n"
                        "         The assignment may be complete, or the page may need "
                        "manual interaction.\n"
                        "         Press Ctrl+S to resume the solver after advancing the page,"
                        " or Ctrl+Q to quit.\n"
                    )
                    # Pause the loop; wait for user to press Ctrl+S again or Ctrl+Q
                    stop_event.wait()
                    if stop_event.is_set():
                        break
                    # If somehow resumed (shouldn't happen via current keybindings) reset
                    idle_count = 0
                else:
                    time.sleep(IDLE_DELAY)

        except pyautogui.FailSafeException:
            print("\n[Solver] ⛔  PyAutoGUI failsafe triggered (mouse moved to corner). Stopping.")
            running = False
            stop_event.set()
            break
        except anthropic.APIError as exc:
            print(f"[Solver] API error: {exc}. Retrying in 5 s…")
            time.sleep(5)
        except Exception as exc:
            print(f"[Solver] Unexpected error: {exc}. Retrying in 3 s…")
            time.sleep(3)

    running = False
    print("[Solver] Stopped.")


# ── Keyboard hooks ────────────────────────────────────────────────────────────

def on_start() -> None:
    global running
    if running:
        print("[Keys] Solver already running.")
        return
    running = True
    stop_event.clear()
    t = threading.Thread(target=solver_loop, daemon=True)
    t.start()


def on_quit() -> None:
    global running
    print("\n[Keys] Ctrl+Q pressed – stopping solver and exiting…")
    running = False
    stop_event.set()
    sys.exit(0)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "ERROR: ANTHROPIC_API_KEY environment variable is not set.\n"
            "       Export it before running:  export ANTHROPIC_API_KEY='sk-ant-…'\n"
        )
        sys.exit(1)

    print("=" * 60)
    print("  Jazz Appreciation Cerego Solver")
    print("=" * 60)
    print("  Ctrl+S  →  Start / resume the solver")
    print("  Ctrl+Q  →  Quit safely at any time")
    print("  Move mouse to TOP-LEFT corner → emergency stop")
    print("=" * 60)
    print("\nNavigate to your Cerego assignment in the browser, then press Ctrl+S.\n")

    keyboard.add_hotkey("ctrl+s", on_start)
    keyboard.add_hotkey("ctrl+q", on_quit)

    # Block main thread
    keyboard.wait()


if __name__ == "__main__":
    main()
