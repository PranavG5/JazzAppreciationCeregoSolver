"""
knowledge.py — Offline knowledge base for Jazz Cerego Solver
=============================================================
Parses multiple study-material files (PDF, TXT, CSV) into a flat list of
Fact objects, then answers multiple-choice questions via fuzzy matching.
No API keys required.
"""

import csv
import json
import os
import re
from dataclasses import dataclass

try:
    from rapidfuzz import fuzz, process as rfprocess
    _RAPIDFUZZ_OK = True
except ImportError:
    _RAPIDFUZZ_OK = False

try:
    import pdfplumber
    _PDF_OK = True
except ImportError:
    _PDF_OK = False


@dataclass
class Fact:
    question: str   # normalised question / topic keyword(s)
    answer: str     # the correct answer text (original casing)
    source: str     # filename this fact came from (for debug logging)


class KnowledgeBase:
    def __init__(self):
        self.facts: list[Fact] = []

    # ── File loaders ──────────────────────────────────────────────────────────

    def add_file(self, path: str) -> int:
        """Parse one file and append its facts.  Returns count added."""
        ext = os.path.splitext(path)[1].lower()
        before = len(self.facts)
        if ext == ".pdf":
            self._add_pdf(path)
        elif ext == ".csv":
            self._add_csv(path)
        else:
            self._add_text(path)
        return len(self.facts) - before

    def _add_pdf(self, path: str):
        if not _PDF_OK:
            raise ImportError("pdfplumber not installed — run: pip install pdfplumber")
        source = os.path.basename(path)
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                self._parse_block(text, source)

    def _add_csv(self, path: str):
        source = os.path.basename(path)
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) >= 2:
                    q, a = row[0].strip(), row[1].strip()
                    if q and a:
                        self.facts.append(Fact(_norm(q), a, source))
                elif len(row) == 1 and row[0].strip():
                    self._parse_line(row[0].strip(), source)

    def _add_text(self, path: str):
        source = os.path.basename(path)
        with open(path, encoding="utf-8", errors="replace") as f:
            self._parse_block(f.read(), source)

    # ── Line/block parsers ────────────────────────────────────────────────────

    def _parse_block(self, text: str, source: str):
        for line in text.splitlines():
            self._parse_line(line.strip(), source)

    def _parse_line(self, line: str, source: str):
        if not line or len(line) < 3:
            return

        # "Q: ... A: ..." or "Question: ... Answer: ..."
        m = re.match(
            r"^[Qq](?:uestion)?[:\-]\s*(.+?)\s*[Aa](?:nswer)?[:\-]\s*(.+)$", line
        )
        if m:
            self.facts.append(Fact(_norm(m.group(1)), m.group(2).strip(), source))
            return

        # "Label: Value" or "Label - Value"  (label ≤ 60 chars to avoid false matches)
        m = re.match(r"^([^:\-]{2,60})[:\-]\s*(.+)$", line)
        if m:
            label = m.group(1).strip()
            value = m.group(2).strip()
            # Store both directions so queries from either side succeed
            self.facts.append(Fact(_norm(label), value, source))
            self.facts.append(Fact(_norm(value), label, source))
            return

        # Standalone line — store as self-referential fact for answer-choice matching
        self.facts.append(Fact(_norm(line), line.strip(), source))

    # ── Query ─────────────────────────────────────────────────────────────────

    def query(self, question_text: str, choices: list[str]) -> tuple[str, float]:
        """
        Pick the best answer from *choices* given *question_text*.
        Returns (best_choice_text, confidence_0_to_100).

        Strategy:
          1. Try direct flashcard match: find a stored question that closely
             matches the screen question, then score each choice against that
             fact's answer.
          2. Fallback: score each choice independently against all stored answers.
          3. Last resort (<50 confidence): return longest choice.
        """
        if not choices:
            return "", 0.0

        if not self.facts or not _RAPIDFUZZ_OK:
            return max(choices, key=len), 0.0

        q_norm = _norm(question_text)
        all_answers   = [f.answer   for f in self.facts]
        all_questions = [f.question for f in self.facts]

        scores: dict[str, float] = {}

        # --- Strategy 1: direct flashcard match ---
        q_match = rfprocess.extractOne(
            q_norm, all_questions, scorer=fuzz.token_set_ratio
        )
        if q_match and q_match[1] >= 72:
            fact_idx = all_questions.index(q_match[0])
            expected = _norm(self.facts[fact_idx].answer)
            for choice in choices:
                sim = fuzz.token_set_ratio(_norm(choice), expected)
                scores[choice] = sim * 0.85 + q_match[1] * 0.15
        else:
            # --- Strategy 2: score each choice against all stored answers ---
            for choice in choices:
                c_norm = _norm(choice)
                ans_hit = rfprocess.extractOne(
                    c_norm, all_answers, scorer=fuzz.token_set_ratio
                )
                q_hit = rfprocess.extractOne(
                    c_norm, all_questions, scorer=fuzz.token_set_ratio
                )
                ans_score = ans_hit[1] if ans_hit else 0
                q_score   = q_hit[1]   if q_hit   else 0
                scores[choice] = max(ans_score, q_score) * 0.70

        best       = max(scores, key=scores.get)
        confidence = min(scores[best], 100.0)

        if confidence < 50:
            return max(choices, key=len), confidence   # low-confidence fallback

        return best, confidence

    # ── Helpers ───────────────────────────────────────────────────────────────

    def size(self) -> int:
        return len(self.facts)

    def file_sources(self) -> list[str]:
        return sorted({f.source for f in self.facts})


# ── Module-level helpers ──────────────────────────────────────────────────────

def _norm(text: str) -> str:
    """Lowercase, strip punctuation and extra whitespace for comparison."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ── Feedback store ────────────────────────────────────────────────────────────

# Strings that indicate the solver's own UI was accidentally OCR'd instead of
# quiz content.  Any Q or A containing one of these is silently discarded.
_UI_NOISE = frozenset([
    "jazz cerego solver", "calibration saved", "facts from",
    "solver started", "solver stopped", "load materials",
    "anthropic api key", "no button", "running...",
    "switch to your cerego", "cerego memory",
    "click start", "click amstart",
])


def _is_quality(text: str) -> bool:
    """
    Return True only if *text* looks like real natural language.
    Rejects OCR noise (random chars, single-letter tokens, UI fragments).
    """
    text = text.strip()
    if not text or len(text) < 3 or len(text) > 250:
        return False
    if "\n" in text:          # multi-line = OCR read several rows at once
        return False
    tl = text.lower()
    if any(n in tl for n in _UI_NOISE):
        return False
    tokens = text.split()
    if not tokens:
        return False
    # At least 45 % of tokens must be ≥ 3 characters long
    if sum(1 for t in tokens if len(t) >= 3) / len(tokens) < 0.45:
        return False
    # Average token length ≥ 2.5  (blocks "a b c d e" style noise)
    if sum(len(t) for t in tokens) / len(tokens) < 2.5:
        return False
    # At least 45 % of tokens must be ≥ 55 % alphabetic
    alpha_ok = sum(
        1 for t in tokens
        if len(t) >= 2 and sum(c.isalpha() for c in t) / len(t) >= 0.55
    )
    if alpha_ok / len(tokens) < 0.45:
        return False
    return True


class FeedbackStore:
    """
    Remembers Cerego's right/wrong verdicts and maps questions to their
    confirmed correct answers.  Persisted to feedback.json so knowledge
    survives between sessions.  Always queried before the static knowledge
    base — Cerego's ground truth takes highest priority.

    Storage format (feedback.json):
        { "<norm_question>": {"answer": "<correct_answer>", "original": "<question_text>"} }
    """

    def __init__(self, path: str):
        self._path  = path
        self._store: dict[str, str] = {}   # norm_question → correct_answer
        self._orig:  dict[str, str] = {}   # norm_question → original question text
        self._load()

    def _load(self):
        if not os.path.isfile(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                if isinstance(v, dict):
                    answer   = v.get("answer", "")
                    original = v.get("original", k)
                else:
                    answer   = v
                    original = k
                # Discard garbage that snuck in during previous sessions
                if _is_quality(k) and _is_quality(answer):
                    self._store[k] = answer
                    self._orig[k]  = original
        except Exception:
            self._store = {}
            self._orig  = {}

    def _save(self):
        data = {
            k: {"answer": self._store[k], "original": self._orig.get(k, k)}
            for k in self._store
        }
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self._write_facts_file()

    def _write_facts_file(self):
        """Write clean Q→A pairs to feedback_facts.txt."""
        txt_path = os.path.splitext(self._path)[0] + "_facts.txt"
        pairs: list[tuple[str, str]] = []   # (display_question, answer)
        for norm_q, answer in self._store.items():
            if not answer.strip():
                continue
            display_q = self._orig.get(norm_q, norm_q)
            pairs.append((display_q, answer))
        pairs.sort(key=lambda p: p[0].lower())
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"# Learned Q→A pairs  ({len(pairs)} facts)\n")
            f.write("# Format: Q: <question> → A: <answer>\n\n")
            for q, a in pairs:
                f.write(f"Q: {q}\nA: {a}\n\n")

    def record(self, question: str, correct_answer: str):
        """Save the confirmed correct answer for this question and persist."""
        if not question.strip() or not correct_answer.strip():
            return
        # Reject OCR noise — only store real words/sentences
        if not _is_quality(question) or not _is_quality(correct_answer):
            return
        norm_q = _norm(question)
        self._store[norm_q] = correct_answer.strip()
        # Prefer the longest/most-original question text seen for this key
        existing = self._orig.get(norm_q, "")
        if len(question.strip()) > len(existing):
            self._orig[norm_q] = question.strip()
        self._save()

    def query(self, question: str) -> str | None:
        """
        Return the confirmed correct answer if known, else None.
        Uses fuzzy matching so minor OCR variations still hit the record.
        """
        if not self._store:
            return None
        key = _norm(question)
        # Exact normalised match first (fast path)
        if key in self._store:
            return self._store[key]
        # Fuzzy match — threshold 80 to handle OCR noise across sessions
        if _RAPIDFUZZ_OK:
            keys  = list(self._store.keys())
            match = rfprocess.extractOne(key, keys, scorer=fuzz.token_set_ratio)
            if match and match[1] >= 80:
                return self._store[match[0]]
        return None

    def size(self) -> int:
        return len(self._store)
