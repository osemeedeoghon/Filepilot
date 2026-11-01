"""Deterministic, explainable document analysis (no AI).

- Summary: *extractive*. It picks the most representative sentences from the
  document and quotes them verbatim, so nothing is invented.
- Category: weighted keyword evidence from the content (not the extension).
  The matched keywords are returned so every suggestion can be explained.
"""

from __future__ import annotations

import math
import re
from collections import Counter

MIN_WORDS_FOR_SUMMARY = 60

# Content categories shared with the optional local-AI provider.
CONTENT_CATEGORIES = (
    "Invoice or receipt",
    "Financial statement",
    "Resume or CV",
    "Contract or legal",
    "Academic or research",
    "Meeting notes",
    "Letter or correspondence",
    "Report",
    "Technical documentation",
    "Medical",
    "Travel",
    "Recipe",
    "Data table",
    "General document",
)

# category -> {phrase: weight}. Phrases are matched as whole words, case-insensitively.
KEYWORDS: dict[str, dict[str, float]] = {
    "Invoice or receipt": {
        "invoice": 3, "invoice number": 4, "receipt": 3, "bill to": 3, "amount due": 4, "subtotal": 3,
        "total due": 3, "payment terms": 3, "due date": 2, "tax": 1, "vat": 2, "qty": 2, "unit price": 3,
        "paid": 1, "order number": 2,
    },
    "Financial statement": {
        "statement": 1, "account number": 3, "balance": 2, "opening balance": 4, "closing balance": 4,
        "transactions": 2, "deposit": 2, "withdrawal": 3, "interest": 1, "portfolio": 2, "dividend": 3,
        "net income": 3, "revenue": 2, "balance sheet": 4, "cash flow": 3,
    },
    "Resume or CV": {
        "resume": 3, "curriculum vitae": 4, "work experience": 4, "experience": 1, "education": 2,
        "skills": 2, "references": 1, "objective": 1, "employment history": 4, "linkedin": 2,
        "bachelor": 2, "master of": 2, "internship": 2, "gpa": 2,
    },
    "Contract or legal": {
        "agreement": 3, "contract": 3, "party": 1, "parties": 2, "hereby": 3, "hereinafter": 4,
        "whereas": 3, "terms and conditions": 3, "governing law": 4, "indemnify": 4, "liability": 2,
        "termination": 2, "confidentiality": 2, "signature": 1, "clause": 2, "lease": 2, "tenant": 2,
    },
    "Academic or research": {
        "abstract": 3, "introduction": 1, "methodology": 3, "methods": 2, "results": 1, "discussion": 1,
        "conclusion": 1, "references": 1, "et al": 4, "hypothesis": 3, "literature review": 4,
        "doi": 3, "journal": 2, "dataset": 1, "experiment": 2, "thesis": 3, "university": 1,
    },
    "Meeting notes": {
        "meeting": 2, "minutes": 2, "agenda": 3, "attendees": 4, "action items": 4, "action item": 4,
        "next steps": 2, "discussed": 2, "follow up": 2, "decisions": 2, "notes": 1,
    },
    "Letter or correspondence": {
        "dear": 3, "sincerely": 4, "regards": 2, "best regards": 3, "yours truly": 4, "to whom it may concern": 5,
        "thank you for": 1, "i am writing": 3, "cover letter": 4,
    },
    "Report": {
        "report": 2, "executive summary": 4, "findings": 3, "recommendations": 3, "summary": 1,
        "analysis": 1, "quarter": 2, "quarterly": 3, "annual": 2, "kpi": 3, "overview": 1,
    },
    "Technical documentation": {
        "installation": 3, "install": 2, "configuration": 2, "usage": 2, "api": 2, "function": 1,
        "parameters": 2, "command": 2, "requirements": 1, "version": 1, "license": 1, "readme": 3,
        "endpoint": 2, "troubleshooting": 3, "setup": 1,
    },
    "Medical": {
        "patient": 3, "diagnosis": 4, "prescription": 4, "dosage": 3, "symptoms": 2, "clinic": 2,
        "physician": 3, "medical": 2, "treatment": 2, "blood pressure": 3, "allergies": 2,
    },
    "Travel": {
        "itinerary": 4, "flight": 3, "boarding": 3, "hotel": 3, "reservation": 2, "check-in": 2,
        "departure": 3, "arrival": 3, "passport": 2, "booking": 2, "gate": 1,
    },
    "Recipe": {
        "ingredients": 4, "tablespoon": 3, "teaspoon": 3, "tbsp": 3, "tsp": 3, "preheat": 4, "oven": 2,
        "bake": 2, "stir": 2, "servings": 3, "cups": 1, "minutes": 1,
    },
}

# CSV column-name signals -> category (header names are facts; the category is an interpretation).
CSV_COLUMN_SIGNALS: dict[str, tuple[str, ...]] = {
    "Financial statement": ("amount", "balance", "debit", "credit", "price", "total", "cost", "revenue", "transaction"),
    "Invoice or receipt": ("invoice", "qty", "quantity", "unit price", "sku", "order"),
    "Travel": ("flight", "departure", "arrival", "hotel"),
    "Academic or research": ("participant", "trial", "sample", "measurement", "score"),
}

STOPWORDS = frozenset(
    """a about above after again against all am an and any are as at be because been before being below
    between both but by can could did do does doing down during each few for from further had has have
    having he her here hers herself him himself his how i if in into is it its itself just me more most my
    myself no nor not now of off on once only or other our ours ourselves out over own same she should so
    some such than that the their theirs them themselves then there these they this those through to too
    under until up very was we were what when where which while who whom why will with would you your
    yours yourself yourselves also may might must shall one two us via per etc""".split()
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(“])|\n{2,}|\n(?=\s*[-*•]\s)")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]+")


def _prose(text: str) -> str:
    """Blank out headings and table rows so they don't merge into sentences."""
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        is_table_row = " | " in stripped
        is_heading = stripped and len(stripped.split()) < 6 and not stripped.endswith((".", "!", "?", ":", ";", ","))
        lines.append("" if is_table_row or is_heading else line)
    return "\n".join(lines)


def _sentences(text: str) -> list[str]:
    out, seen = [], set()
    for raw in _SENTENCE_SPLIT.split(_prose(text)):
        s = " ".join(raw.split())
        n = len(s.split())
        if 6 <= n <= 60 and sum(ch.isalpha() for ch in s) > len(s) * 0.5 and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def summarize(text: str, max_sentences: int = 3, max_chars: int = 600) -> tuple[str | None, str]:
    """Extractive summary. Returns (summary or None, explanation)."""
    words = _WORD.findall(text)
    word_count = len(re.findall(r"\w+", text))  # same count as the "Words" fact
    if word_count < MIN_WORDS_FOR_SUMMARY:
        return None, f"Not enough text for a summary (needs at least {MIN_WORDS_FOR_SUMMARY} words, found {word_count})."
    sentences = _sentences(text[:200_000])
    if len(sentences) < 2:
        return None, "The text has no clear sentences to summarise (it may be a list or table)."

    freq = Counter(w.lower() for w in words if w.lower() not in STOPWORDS and len(w) > 2)
    top = freq.most_common(1)[0][1] if freq else 1
    scored = []
    for i, s in enumerate(sentences):
        terms = [w.lower() for w in _WORD.findall(s) if w.lower() not in STOPWORDS and len(w) > 2]
        if not terms:
            continue
        score = sum(freq[t] / top for t in terms) / math.pow(len(terms), 0.6)
        score *= 1.25 if i < 3 else 1.0  # opening sentences tend to state the topic
        scored.append((score, i, s))
    best = sorted(sorted(scored, reverse=True)[:max_sentences], key=lambda x: x[1])
    summary, used = [], 0
    for _, _, s in best:
        if used + len(s) > max_chars and summary:
            break
        summary.append(s if len(s) <= max_chars else s[: max_chars - 1] + "…")
        used += len(s)
    return " ".join(summary), f"The {len(summary)} most representative sentence(s), quoted from the document."


def _count(text_lower: str, phrase: str) -> int:
    return len(re.findall(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text_lower))


def categorize(text: str, *, kind: str, columns: list[str] | None = None) -> list[dict]:
    """Score content categories. Returns up to 3 suggestions, strongest first."""
    lower = text[:200_000].lower()
    n_words = max(1, len(_WORD.findall(lower)))
    scores: dict[str, float] = {}
    evidence: dict[str, list[str]] = {}
    for category, phrases in KEYWORDS.items():
        total, hits = 0.0, []
        for phrase, weight in phrases.items():
            c = _count(lower, phrase)
            if c:
                total += weight * min(c, 3)
                hits.append(phrase)
        if total:
            # Long documents mention many words by chance; damp the score slightly with length.
            scores[category] = total / (1 + math.log10(max(1, n_words / 500)))
            evidence[category] = [f"“{h}”" for h in hits]

    if kind == "csv":
        cols = [c.lower() for c in (columns or [])]
        scores["Data table"] = scores.get("Data table", 0) + 4
        evidence.setdefault("Data table", []).append("tabular rows and columns")
        for category, signals in CSV_COLUMN_SIGNALS.items():
            matched = [c for c in cols if any(sig in c for sig in signals)]
            if matched:
                scores[category] = scores.get(category, 0) + 3 * len(matched)
                evidence.setdefault(category, []).extend(f"column “{m}”" for m in matched[:5])

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    out = []
    for i, (category, score) in enumerate(ranked[:3]):
        if score < 3:
            continue
        runner_up = ranked[i + 1][1] if i + 1 < len(ranked) else 0
        if i == 0 and score >= 10 and score >= runner_up * 1.5:
            confidence = "high"
        elif score >= 6:
            confidence = "medium"
        else:
            confidence = "low"
        out.append({
            "name": category,
            "confidence": confidence,
            "score": round(score, 1),
            "evidence": evidence[category][:8],
        })
    if not out:
        out.append({
            "name": "General document",
            "confidence": "low",
            "score": 0,
            "evidence": ["no strong content signals"],
        })
    return out


def analyze(text: str, *, kind: str, title: str | None = None, columns: list[str] | None = None) -> dict:
    summary, summary_note = summarize(text) if kind != "csv" else (None, "Tables are described by their columns and rows instead of a summary.")
    return {
        "generated_by": "rules",
        "label": "Rule-based (no AI)",
        "summary": summary,
        "summary_note": summary_note,
        "categories": categorize(text, kind=kind, columns=columns),
        "category_note": "Suggested from words found in the content, not from the file name or extension.",
    }
