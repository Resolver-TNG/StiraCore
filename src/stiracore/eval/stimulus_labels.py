"""Japanese stimulus mini-set (#164): label schema, agreement and classifier scoring.

Pure functions only (no model calls). ``scripts/jp_stimulus_labels.py`` does the
generation / labeling calls and uses this module to validate and aggregate.

The labels produced with this module are *model-pair draft labels* (two LLM
labelers), not blind human labels — see docs/eval/emotion-ablation-prereg.md §13.
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

CORE6 = ("joy", "threat", "achievement", "loss", "uncertainty", "connection")
MISSING = ("failure", "frustration", "criticism", "pressure", "threat_resolved")
NONE = "none"
LABELS = (*CORE6, *MISSING, NONE)
CORE6_OR_NONE = (*CORE6, NONE)
POLARITIES = ("worsening", "resolving", NONE)
FIELDS = ("label", "core6", "polarity")
REASON_MAX = 120

# Generation intents (never shown to labelers) and the label each intends.
GEN_TARGETS: dict[str, tuple[str, str]] = {
    "joy": ("joy", NONE),
    "threat": ("threat", NONE),
    "achievement": ("achievement", NONE),
    "loss": ("loss", NONE),
    "uncertainty": ("uncertainty", NONE),
    "connection": ("connection", NONE),
    "failure": ("failure", NONE),
    "frustration": ("frustration", NONE),
    "criticism": ("criticism", NONE),
    "pressure": ("pressure", NONE),
    "threat_resolved": ("threat_resolved", "resolving"),
    "threat_escalated": ("threat", "worsening"),
    "neutral": (NONE, NONE),
    "noise": (NONE, NONE),
}

# Coarse screen for things a synthetic set must not contain. Only the *kind* and
# the count of a hit are ever stored; the matched text is never copied.
PII_PATTERNS: dict[str, str] = {
    "email": r"[\w.+-]+@[\w-]+\.[\w.]+",
    "url": r"https?://[^\s<>\"'`）」』]+",
    "phone_jp": r"\b0\d{1,4}-\d{1,4}-\d{3,4}\b",
    "aws_key_id": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
}
# Reserved documentation hosts (RFC 2606/6761). A URL passes only if its *host*
# is one of these or a subdomain of one (``example.com.evil.test`` does not pass).
SAFE_URL_HOSTS = ("example.com", "example.org", "example.net", "example", "localhost")


def _safe_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in SAFE_URL_HOSTS)


def _pii_spans(text: str) -> list[tuple[int, int, str]]:
    spans = []
    for kind, pattern in PII_PATTERNS.items():
        for m in re.finditer(pattern, text):
            if kind == "url" and _safe_url(m.group(0)):
                continue
            spans.append((m.start(), m.end(), kind))
    return sorted(spans)


def find_pii(text: str) -> dict[str, int]:
    """Kinds and counts of screen hits (never the matched strings)."""
    counts: dict[str, int] = {}
    for _s, _e, kind in _pii_spans(text):
        counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items()))


def redact_pii(text: str) -> str:
    """``text`` with every screen hit replaced by ``[REDACTED:<kind>]``."""
    out, pos = [], 0
    for start, end, kind in _pii_spans(text):
        if start < pos:  # overlapping hit (e.g. e-mail inside a URL)
            continue
        out.append(text[pos:start])
        out.append(f"[REDACTED:{kind}]")
        pos = end
    out.append(text[pos:])
    return "".join(out)


class LabelError(ValueError):
    """A labeler record or batch does not match the fixed schema."""


def extract_json(text: str) -> Any:
    """Parse the first JSON object in a model reply (tolerates ``` fences)."""
    text = text.strip()
    # Only a reply that *starts* with a fence is unwrapped: replies may quote
    # utterances that themselves contain ``` (code-mixed data).
    fence = re.match(r"```(?:json)?\s*(.*?)```\s*$", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise LabelError("no JSON object in reply")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LabelError(f"invalid JSON: {exc}") from exc


def validate_record(raw: Mapping[str, Any]) -> dict[str, str]:
    """Check one labeler item against the fixed schema; return the normalized record."""
    if not isinstance(raw, Mapping):
        raise LabelError("item is not an object")
    missing = {"id", *FIELDS, "reason"} - set(raw)
    if missing:
        raise LabelError(f"missing keys {sorted(missing)}")
    rec = {k: str(raw[k]).strip() for k in ("id", *FIELDS, "reason")}
    if rec["label"] not in LABELS:
        raise LabelError(f"label {rec['label']!r} not in {LABELS}")
    if rec["core6"] not in CORE6_OR_NONE:
        raise LabelError(f"core6 {rec['core6']!r} not in {CORE6_OR_NONE}")
    if rec["polarity"] not in POLARITIES:
        raise LabelError(f"polarity {rec['polarity']!r} not in {POLARITIES}")
    if rec["label"] in CORE6 and rec["core6"] != rec["label"]:
        raise LabelError("core6 must equal label when label is one of the current 6")
    rec["reason"] = rec["reason"][:REASON_MAX]
    return rec


def parse_batch(text: str, expected_ids: Sequence[str]) -> list[dict[str, str]]:
    """Parse a labeler reply ``{"items": [...]}`` covering exactly ``expected_ids``."""
    obj = extract_json(text)
    items = obj.get("items") if isinstance(obj, Mapping) else None
    if not isinstance(items, list):
        raise LabelError("reply has no 'items' list")
    out = [validate_record(item) for item in items]
    got = [r["id"] for r in out]
    if len(set(got)) != len(got):
        raise LabelError("duplicate ids in reply")
    if set(got) != set(expected_ids):
        extra = sorted(set(got) - set(expected_ids))
        lost = sorted(set(expected_ids) - set(got))
        raise LabelError(f"ids differ from the batch (extra={extra}, missing={lost})")
    return out


# --- agreement -----------------------------------------------------------------


def cohen_kappa(a: Sequence[str], b: Sequence[str]) -> float | None:
    """Cohen's kappa for two raters over the same items (None if undefined)."""
    if len(a) != len(b):
        raise ValueError("raters must label the same items")
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if pe == 1.0:
        return None
    return (po - pe) / (1 - pe)


def _percentile(values: list[float], q: float) -> float:
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def kappa_interval(
    a: Sequence[str], b: Sequence[str], *, reps: int = 2000, seed: int = 0
) -> dict[str, Any]:
    """Item-bootstrap 95% percentile interval for Cohen's kappa."""
    if len(a) != len(b):
        raise ValueError("raters must label the same items")
    n = len(a)
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(reps):
        idx = [rng.randrange(n) for _ in range(n)]
        k = cohen_kappa([a[i] for i in idx], [b[i] for i in idx])
        if k is not None:
            values.append(k)
    if not values:
        return {"low": None, "high": None, "valid_reps": 0}
    return {
        "low": _percentile(values, 0.025),
        "high": _percentile(values, 0.975),
        "valid_reps": len(values),
    }


def specific_agreement(a: Sequence[str], b: Sequence[str], categories: Iterable[str]) -> dict:
    """Positive specific agreement per category: 2*both / (n_a + n_b)."""
    out = {}
    for c in categories:
        both = sum(x == c and y == c for x, y in zip(a, b, strict=True))
        na, nb = sum(x == c for x in a), sum(y == c for y in b)
        out[c] = {
            "both": both,
            "n_a": na,
            "n_b": nb,
            "psa": (2 * both / (na + nb)) if na + nb else None,
        }
    return out


def confusion(a: Sequence[str], b: Sequence[str], categories: Sequence[str]) -> dict:
    """Confusion matrix rows=a, cols=b (counts), over the given category order."""
    m = {r: dict.fromkeys(categories, 0) for r in categories}
    for x, y in zip(a, b, strict=True):
        m[x][y] += 1
    return m


def kappa_band(kappa: float | None) -> str:
    """Pre-registered adoption band (prereg §13.4); fixed before measurement."""
    if kappa is None:
        return "undefined"
    if kappa >= 0.6:
        return "adopt-as-draft-reference"
    if kappa >= 0.4:
        return "descriptive-only-revise-definitions"
    return "do-not-use"


def agreement_report(
    labels_a: Mapping[str, Mapping[str, str]],
    labels_b: Mapping[str, Mapping[str, str]],
    *,
    reps: int = 2000,
    seed: int = 0,
) -> dict[str, Any]:
    """Per-field kappa, interval, raw agreement, specific agreement, confusion."""
    ids = sorted(set(labels_a) & set(labels_b))
    cats = {"label": LABELS, "core6": CORE6_OR_NONE, "polarity": POLARITIES}
    out: dict[str, Any] = {
        "n_items": len(ids),
        "only_a": sorted(set(labels_a) - set(labels_b)),
        "only_b": sorted(set(labels_b) - set(labels_a)),
        "fields": {},
    }
    for f in FIELDS:
        a = [labels_a[i][f] for i in ids]
        b = [labels_b[i][f] for i in ids]
        k = cohen_kappa(a, b)
        psa = specific_agreement(a, b, cats[f])
        out["fields"][f] = {
            "kappa": k,
            "kappa_ci95": kappa_interval(a, b, reps=reps, seed=seed),
            "band": kappa_band(k),
            "raw_agreement": (sum(x == y for x, y in zip(a, b, strict=True)) / len(ids))
            if ids
            else None,
            "specific_agreement": psa,
            "review_definition": sorted(
                c for c, v in psa.items() if v["psa"] is not None and v["psa"] < 0.6
            ),
            "confusion_rows_a_cols_b": confusion(a, b, cats[f]),
        }
    return out


def index_labels(
    records: Iterable[Mapping[str, Any]], population: Sequence[str], *, who: str = "labeler"
) -> dict[str, Mapping[str, Any]]:
    """Index one labeler's records by id against the utterance population.

    Duplicate ids and ids outside the population are errors (they cannot be
    silently collapsed or dropped); ids the labeler did not return are simply
    absent, so ``reference_labels`` marks them as needing human judgement.
    """
    pop = check_population(population)
    out: dict[str, Mapping[str, Any]] = {}
    for rec in records:
        i = rec["id"]
        if i in out:
            raise LabelError(f"{who}: duplicate id {i!r}")
        if i not in pop:
            raise LabelError(f"{who}: id {i!r} is not in the utterance population")
        out[i] = rec
    return out


def check_population(population: Sequence[str]) -> set[str]:
    dup = sorted(i for i, n in Counter(population).items() if n > 1)
    if dup:
        raise LabelError(f"duplicate utterance ids {dup}")
    return set(population)


def reference_labels(
    labels_a: Mapping[str, Mapping[str, str]],
    labels_b: Mapping[str, Mapping[str, str]],
    population: Sequence[str],
) -> dict[str, dict[str, str | None]]:
    """Per utterance id and field: the agreed value, or None (= needs human judgement).

    The population is every utterance id. A label for an id outside it is an
    error; an id missing from either labeler is ``None`` in every field.
    """
    pop = check_population(population)
    for who, labels in (("a", labels_a), ("b", labels_b)):
        extra = sorted(set(labels) - pop)
        if extra:
            raise LabelError(f"labeler {who}: ids outside the population {extra}")
    out: dict[str, dict[str, str | None]] = {}
    for i in sorted(pop):
        ra, rb = labels_a.get(i), labels_b.get(i)
        out[i] = {f: (ra[f] if ra and rb and ra[f] == rb[f] else None) for f in FIELDS}
    return out


# --- classifier scoring --------------------------------------------------------


def precision_recall(
    reference: Sequence[str], predicted: Sequence[str], categories: Sequence[str]
) -> dict[str, Any]:
    """Per-category precision/recall/F1 (None when the denominator is 0)."""
    rows = {}
    for c in categories:
        tp = sum(r == c and p == c for r, p in zip(reference, predicted, strict=True))
        n_pred = sum(p == c for p in predicted)
        n_ref = sum(r == c for r in reference)
        prec = tp / n_pred if n_pred else None
        rec = tp / n_ref if n_ref else None
        f1 = (2 * prec * rec / (prec + rec)) if prec and rec else (0.0 if n_pred or n_ref else None)
        rows[c] = {
            "tp": tp,
            "n_pred": n_pred,
            "n_ref": n_ref,
            "precision": prec,
            "recall": rec,
            "f1": f1,
        }
    return rows


def macro(
    rows: Mapping[str, Mapping[str, Any]], key: str, categories: Iterable[str]
) -> float | None:
    vals = [rows[c][key] for c in categories if rows[c][key] is not None]
    return sum(vals) / len(vals) if vals else None


def detection_scores(reference: Sequence[str], predicted: Sequence[str]) -> dict[str, Any]:
    """Binary 'something detected' vs 'none' precision/recall."""
    ref = [r != NONE for r in reference]
    pred = [p != NONE for p in predicted]
    tp = sum(r and p for r, p in zip(ref, pred, strict=True))
    return {
        "tp": tp,
        "n_pred": sum(pred),
        "n_ref": sum(ref),
        "precision": tp / sum(pred) if sum(pred) else None,
        "recall": tp / sum(ref) if sum(ref) else None,
    }


def crosstab(rows: Sequence[str], cols: Sequence[str]) -> dict[str, dict[str, int]]:
    out: dict[str, Counter] = {}
    for r, c in zip(rows, cols, strict=True):
        out.setdefault(r, Counter())[c] += 1
    return {r: dict(sorted(v.items())) for r, v in sorted(out.items())}


def human_sample(
    reference: Mapping[str, Mapping[str, str | None]],
    *,
    n_agreed: int = 14,
    n_disagreed: int = 6,
    seed: int = 0,
) -> dict[str, list[str]]:
    """Prereg §13.6: stratified pick for later human spot-checks (no judging here)."""
    rng = random.Random(f"human-sample:{seed}")
    agreed: dict[str, list[str]] = {}
    disagreed: list[str] = []
    for i, ref in sorted(reference.items()):
        if ref["label"] is None:
            disagreed.append(i)
        else:
            agreed.setdefault(str(ref["label"]), []).append(i)
    picked: list[str] = []
    strata = sorted(agreed)
    pools = {s: rng.sample(agreed[s], len(agreed[s])) for s in strata}
    while len(picked) < n_agreed and any(pools.values()):
        for s in strata:
            if pools[s] and len(picked) < n_agreed:
                picked.append(pools[s].pop())
    dis = rng.sample(disagreed, min(n_disagreed, len(disagreed)))
    rest = [i for s in strata for i in pools[s]]
    rng.shuffle(rest)
    while len(picked) + len(dis) < n_agreed + n_disagreed and rest:
        picked.append(rest.pop())
    return {"agreed": sorted(picked), "disagreed": sorted(dis)}
