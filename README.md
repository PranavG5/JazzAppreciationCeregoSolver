# Jazz Appreciation Cerego Solver

Automatically solves Cerego assignments for your Jazz Appreciation course.
It takes a screenshot, asks Claude (vision AI) to identify the question and correct answer, then clicks the answer — repeating until the assignment is complete.

---

## Setup

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Set your Anthropic API key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

> Get a key at https://console.anthropic.com

---

## Running the solver

```bash
python solver.py
```

You will see:

```
============================================================
  Jazz Appreciation Cerego Solver
============================================================
  Ctrl+S  →  Start / resume the solver
  Ctrl+Q  →  Quit safely at any time
  Move mouse to TOP-LEFT corner → emergency stop
============================================================

Navigate to your Cerego assignment in the browser, then press Ctrl+S.
```

1. Open your Cerego assignment in the browser on the **same screen**.
2. Press **Ctrl+S** to start — the solver takes over and you just watch.
3. Press **Ctrl+Q** at any time to stop safely.

---

## What it handles

| Question type | What happens |
|---|---|
| Multiple choice | Clicks the correct answer option |
| Matching / Connect | Clicks the items one at a time in order |
| Era / Instrument identification | Clicks the correct option |
| Deep Listening | Waits for audio, then clicks the correct genre/artist |

Since wrong answers carry no penalty, the solver keeps trying until each question is marked complete.

---

## Safety features

| Feature | Description |
|---|---|
| **Ctrl+Q** | Graceful exit at any time |
| **PyAutoGUI failsafe** | Moving the mouse to the top-left corner stops all mouse movement immediately |
| **Idle detection** | If no question is found after 10 attempts, the solver pauses and asks you to manually advance the page |
| **API error recovery** | Automatically retries on API errors with a short delay |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ANTHROPIC_API_KEY not set` | Run `export ANTHROPIC_API_KEY="sk-ant-..."` before starting |
| Solver clicks the wrong place | The screen layout may be unusual; it will self-correct on the next question |
| Solver gets stuck | Press Ctrl+Q to stop, manually advance the page, then run again |
| `keyboard` module needs root | On Linux, run with `sudo python solver.py` or add your user to the `input` group |
