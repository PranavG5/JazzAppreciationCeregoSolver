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

class FeedbackStore:
    """
    Remembers Cerego's right/wrong verdicts and maps questions to their
    confirmed correct answers.  Persisted to feedback.json so knowledge
    survives between sessions.  Always queried before the static knowledge
    base — Cerego's ground truth takes highest priority.
    """

    def __init__(self, path: str):
        self._path  = path
        self._store: dict[str, str] = {}   # norm_question → correct_answer
        self._load()

    def _load(self):
        if os.path.isfile(self._path):
            try:
                with open(self._path, encoding="utf-8") as f:
                    self._store = json.load(f)
            except Exception:
                self._store = {}

    def _save(self):
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._store, f, indent=2, ensure_ascii=False)
        self._write_facts_file()

    def _write_facts_file(self):
        """Write a human-readable learned_facts.txt alongside feedback.json."""
        txt_path = os.path.splitext(self._path)[0] + "_facts.txt"
        # Deduplicate: only keep entries where question != answer (skip reverses)
        # and collect unique (question, answer) pairs sorted alphabetically.
        seen: set[frozenset] = set()
        lines: list[str] = []
        for q, a in sorted(self._store.items(), key=lambda x: x[0]):
            pair = frozenset([q, _norm(a)])
            if pair in seen:
                continue
            seen.add(pair)
            lines.append(f"{a}  |  {self._store.get(_norm(a), q)}")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"Learned facts  ({len(lines)} unique pairs)\n")
            f.write("=" * 60 + "\n\n")
            for line in lines:
                f.write(line + "\n")

    def record(self, question: str, correct_answer: str):
        """Save the confirmed correct answer for this question and persist."""
        if not question.strip() or not correct_answer.strip():
            return
        self._store[_norm(question)] = correct_answer.strip()
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
        # Fuzzy match — require high similarity (≥ 88) to avoid false hits
        if _RAPIDFUZZ_OK:
            keys  = list(self._store.keys())
            match = rfprocess.extractOne(key, keys, scorer=fuzz.token_set_ratio)
            if match and match[1] >= 88:
                return self._store[match[0]]
        return None

    def size(self) -> int:
        return len(self._store)
