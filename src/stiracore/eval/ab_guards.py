"""Deterministic guards for harness A/B runs (#729, parts for #698).

Four pure functions. They use no clock, no randomness, no network, no model and
no file I/O; the caller reads the files and hands over bytes and strings.

- ``run_fingerprint`` / ``fingerprint_detail``: hash the harness files and the
  task inputs of one run.
- ``diff_matches_declared_change``: the harness files that differ between two
  arms must be exactly the declared change, and the inputs must be identical.
- ``check_plan``: the runs that happened are the runs that were planned, and no
  run label appears twice.
- ``leak_check``: no banned term and no held-out path in the context given to
  the run side. Fail-closed: a configuration it cannot judge is refused.

Not handled here (the caller's job): reading files, path normalisation, ``..``,
symbolic links and case folding of file names. A name is an opaque string, so
``a/b.py`` and ``./a/b.py`` are two different names. ``leak_check`` is a
tripwire on text, not an access boundary; see docs/eval/ab-run-guards.md.

Every function returns a ``Result`` whose ``reasons`` are fixed codes only: no
file name, term, path or other free text, so a result can be logged as is.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase

FINGERPRINT_SCHEMA = "stira-eval-ab-fingerprint/1"
_SECTIONS = ("harness", "inputs")
_HEX64 = re.compile(r"[0-9a-f]{64}")

# diff_matches_declared_change
E_FINGERPRINT_INVALID = "E_FINGERPRINT_INVALID"
E_DECLARED_INVALID = "E_DECLARED_INVALID"
E_DECLARED_EMPTY = "E_DECLARED_EMPTY"
E_INPUTS_DIFFER = "E_INPUTS_DIFFER"
E_CHANGE_NOT_DECLARED = "E_CHANGE_NOT_DECLARED"
E_DECLARED_NOT_CHANGED = "E_DECLARED_NOT_CHANGED"
# check_plan
E_PLAN_INVALID = "E_PLAN_INVALID"
E_PLAN_EMPTY = "E_PLAN_EMPTY"
E_ACTUAL_INVALID = "E_ACTUAL_INVALID"
E_LABEL_INVALID = "E_LABEL_INVALID"
E_LABEL_DUPLICATE = "E_LABEL_DUPLICATE"
E_ARM_UNPLANNED = "E_ARM_UNPLANNED"
E_COUNT_MISMATCH = "E_COUNT_MISMATCH"
# leak_check
E_CONTEXT_INVALID = "E_CONTEXT_INVALID"
E_CONTEXT_EMPTY = "E_CONTEXT_EMPTY"
E_HELDOUT_GLOBS_INVALID = "E_HELDOUT_GLOBS_INVALID"
E_HELDOUT_GLOBS_EMPTY = "E_HELDOUT_GLOBS_EMPTY"
E_BANNED_TERMS_INVALID = "E_BANNED_TERMS_INVALID"
E_BANNED_TERMS_EMPTY = "E_BANNED_TERMS_EMPTY"
E_LEAK_BANNED_TERM = "E_LEAK_BANNED_TERM"
E_LEAK_HELDOUT_PATH = "E_LEAK_HELDOUT_PATH"

REASON_CODES = frozenset(
    {
        E_FINGERPRINT_INVALID,
        E_DECLARED_INVALID,
        E_DECLARED_EMPTY,
        E_INPUTS_DIFFER,
        E_CHANGE_NOT_DECLARED,
        E_DECLARED_NOT_CHANGED,
        E_PLAN_INVALID,
        E_PLAN_EMPTY,
        E_ACTUAL_INVALID,
        E_LABEL_INVALID,
        E_LABEL_DUPLICATE,
        E_ARM_UNPLANNED,
        E_COUNT_MISMATCH,
        E_CONTEXT_INVALID,
        E_CONTEXT_EMPTY,
        E_HELDOUT_GLOBS_INVALID,
        E_HELDOUT_GLOBS_EMPTY,
        E_BANNED_TERMS_INVALID,
        E_BANNED_TERMS_EMPTY,
        E_LEAK_BANNED_TERM,
        E_LEAK_HELDOUT_PATH,
    }
)


@dataclass(frozen=True)
class Result:
    """``ok`` is True only when ``reasons`` is empty. ``reasons`` holds fixed codes."""

    ok: bool
    reasons: tuple[str, ...] = ()


def _result(reasons: Iterable[str]) -> Result:
    unique = tuple(dict.fromkeys(reasons))
    return Result(ok=not unique, reasons=unique)


# --- fingerprint ---------------------------------------------------------------


def _hash_section(files: object, what: str) -> dict[str, str]:
    if not isinstance(files, Mapping):
        raise TypeError(f"{what} must be a mapping of name to bytes")
    out: dict[str, str] = {}
    for name, content in files.items():
        if not isinstance(name, str) or not isinstance(content, bytes):
            raise TypeError(f"{what} must map str names to bytes")
        out[name] = hashlib.sha256(content).hexdigest()
    return out


def fingerprint_detail(
    harness_files: Mapping[str, bytes], inputs: Mapping[str, bytes]
) -> dict[str, dict[str, str]]:
    """Per-file sha256 of one run: ``{"harness": {name: hex}, "inputs": {name: hex}}``.

    This is what ``diff_matches_declared_change`` compares. It holds names and
    digests, never content. Raises ``TypeError`` for anything but ``str`` names
    and ``bytes`` content (text must be encoded by the caller, so the encoding
    is the caller's explicit choice).
    """
    return {
        "harness": _hash_section(harness_files, "harness_files"),
        "inputs": _hash_section(inputs, "inputs"),
    }


def run_fingerprint(harness_files: Mapping[str, bytes], inputs: Mapping[str, bytes]) -> str:
    """One sha256 hex digest for the harness files and task inputs of a run.

    The same names and bytes give the same digest regardless of mapping order;
    one changed byte, a renamed file, an added or removed file, or a file moved
    between ``harness_files`` and ``inputs`` gives another. Names are opaque
    strings: no path normalisation, no ``..`` or symlink resolution.
    """
    detail = fingerprint_detail(harness_files, inputs)
    h = hashlib.sha256()
    h.update(FINGERPRINT_SCHEMA.encode("ascii") + b"\n")
    for section in _SECTIONS:
        files = detail[section]
        h.update(f"{section} {len(files)}\n".encode("ascii"))
        # Length-prefixed names: no name can imitate a record boundary.
        for name, digest in sorted(
            files.items(), key=lambda kv: kv[0].encode("utf-8", "surrogatepass")
        ):
            raw = name.encode("utf-8", "surrogatepass")
            h.update(f"{len(raw)}:".encode("ascii") + raw + b":" + digest.encode("ascii") + b"\n")
    return h.hexdigest()


def _valid_detail(detail: object) -> bool:
    if not isinstance(detail, Mapping) or set(detail) != set(_SECTIONS):
        return False
    for section in _SECTIONS:
        files = detail[section]
        if not isinstance(files, Mapping):
            return False
        for name, digest in files.items():
            if not isinstance(name, str) or not isinstance(digest, str):
                return False
            if not _HEX64.fullmatch(digest):
                return False
    return True


def _changed(a: Mapping[str, str], b: Mapping[str, str]) -> set[str]:
    return {name for name in set(a) | set(b) if a.get(name) != b.get(name)}


def diff_matches_declared_change(
    fp_a_detail: Mapping[str, Mapping[str, str]],
    fp_b_detail: Mapping[str, Mapping[str, str]],
    declared: set[str],
) -> Result:
    """The harness files that differ between arm A and arm B are exactly ``declared``.

    ``fp_*_detail`` come from ``fingerprint_detail``. A file that exists in one
    arm only counts as differing. ``declared`` is the set of harness file names
    of the one declared change (one change may touch several files); it must not
    be empty, so two identical arms never pass. The inputs must be identical in
    both arms. A result that fails here is invalid as an A/B comparison.
    """
    reasons: list[str] = []
    details_ok = _valid_detail(fp_a_detail) and _valid_detail(fp_b_detail)
    if not details_ok:
        reasons.append(E_FINGERPRINT_INVALID)
    declared_ok = isinstance(declared, (set, frozenset)) and all(
        isinstance(name, str) for name in declared
    )
    if not declared_ok:
        reasons.append(E_DECLARED_INVALID)
    elif not declared:
        reasons.append(E_DECLARED_EMPTY)
    if not details_ok or not declared_ok or not declared:
        return _result(reasons)

    if _changed(fp_a_detail["inputs"], fp_b_detail["inputs"]):
        reasons.append(E_INPUTS_DIFFER)
    changed = _changed(fp_a_detail["harness"], fp_b_detail["harness"])
    if changed - declared:
        reasons.append(E_CHANGE_NOT_DECLARED)
    if declared - changed:
        reasons.append(E_DECLARED_NOT_CHANGED)
    return _result(reasons)


# --- plan ----------------------------------------------------------------------


def _materialise_strings(values: object) -> tuple[str, ...] | None:
    """A tuple of the strings in ``values``, or None when it is not an iterable of str.

    A bare ``str`` or ``bytes`` is refused: iterating it would silently check
    single characters.
    """
    if isinstance(values, (str, bytes, bytearray, Mapping)):
        return None
    try:
        items = tuple(values)  # type: ignore[arg-type]
    except TypeError:
        return None
    if not all(isinstance(item, str) for item in items):
        return None
    return items


def check_plan(planned: Mapping[str, int], actual: Iterable[str]) -> Result:
    """The runs that happened are the planned ones: same count per arm, no duplicate label.

    ``planned`` maps an arm name to its planned number of runs (an ``int`` of 1
    or more; the name is non-empty and has no ``/``). ``actual`` holds one label
    per finished run in the form ``"<arm>/<run id>"``, split at the first ``/``.
    A label seen twice, a label of an unplanned arm, and a count that differs
    from the plan (more or fewer, including a planned arm with no run) are each
    refused.
    """
    if not isinstance(planned, Mapping):
        return _result([E_PLAN_INVALID])
    if not planned:
        return _result([E_PLAN_EMPTY])
    for arm, count in planned.items():
        if not isinstance(arm, str) or not arm or "/" in arm:
            return _result([E_PLAN_INVALID])
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            return _result([E_PLAN_INVALID])
    labels = _materialise_strings(actual)
    if labels is None:
        return _result([E_ACTUAL_INVALID])

    reasons: list[str] = []
    seen: set[str] = set()
    counts = dict.fromkeys(planned, 0)
    for label in labels:
        arm, sep, run_id = label.partition("/")
        if not sep or not arm or not run_id:
            reasons.append(E_LABEL_INVALID)
            continue
        if label in seen:
            reasons.append(E_LABEL_DUPLICATE)
            continue
        seen.add(label)
        if arm not in counts:
            reasons.append(E_ARM_UNPLANNED)
            continue
        counts[arm] += 1
    if any(counts[arm] != planned[arm] for arm in planned):
        reasons.append(E_COUNT_MISMATCH)
    return _result(reasons)


# --- leak ----------------------------------------------------------------------

_WS = re.compile(r"\s+")
# Characters that end a path-like token in running text.
_TOKEN_SPLIT = re.compile(r"[\s\"'`<>()\[\]{}|,;:=]+")
_TOKEN_TRIM = ".!?"


def _fold(text: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def _path_candidates(part: str) -> set[str]:
    """Tokens of ``part`` and their ``/``-suffixes (``/abs/repo/tests/x`` also yields ``tests/x``)."""
    out: set[str] = set()
    for token in _TOKEN_SPLIT.split(part.replace("\\", "/")):
        token = token.rstrip(_TOKEN_TRIM)
        while token:
            out.add(token)
            _, sep, token = token.partition("/")
            if not sep:
                break
    return out


def leak_check(
    context_parts: Iterable[str],
    heldout_globs: Iterable[str],
    banned_terms: Iterable[str],
) -> Result:
    """No banned term and no held-out path appears in the run-side context. Fail-closed.

    ``context_parts`` is every piece of text handed to the run side (prompt,
    instructions, tool descriptions, file listings ...), as ``str``.

    - A banned term matches as a substring after NFKC, case folding and
      collapsing whitespace.
    - A held-out glob (``fnmatch`` syntax, case-sensitive, ``*`` also crosses
      ``/``) is matched against each path-like token of the context and against
      the token's suffixes after each ``/``; ``\\`` is read as ``/``.

    Refused without judging (not ok): a wrong type anywhere (including a bare
    ``str`` where an iterable of ``str`` is expected), an empty or blank term or
    glob, no context parts, no held-out globs, or no banned terms. "Nothing to
    check" is not the same as "checked and clean", so it does not pass.

    The reasons never say which term or path matched: those are evaluation
    information themselves.
    """
    reasons: list[str] = []
    parts = _materialise_strings(context_parts)
    if parts is None:
        reasons.append(E_CONTEXT_INVALID)
    elif not parts:
        reasons.append(E_CONTEXT_EMPTY)

    globs = _materialise_strings(heldout_globs)
    if globs is None or any(not g.strip() for g in globs):
        reasons.append(E_HELDOUT_GLOBS_INVALID)
    elif not globs:
        reasons.append(E_HELDOUT_GLOBS_EMPTY)

    terms = _materialise_strings(banned_terms)
    folded_terms = [_fold(t) for t in terms] if terms is not None else []
    if terms is None or any(not t for t in folded_terms):
        reasons.append(E_BANNED_TERMS_INVALID)
    elif not terms:
        reasons.append(E_BANNED_TERMS_EMPTY)

    if reasons or parts is None or globs is None:
        return _result(reasons)

    norm_globs = [g.replace("\\", "/") for g in globs]
    for part in parts:
        folded = _fold(part)
        if any(term in folded for term in folded_terms):
            reasons.append(E_LEAK_BANNED_TERM)
        candidates = _path_candidates(part)
        if any(fnmatchcase(c, g) for c in candidates for g in norm_globs):
            reasons.append(E_LEAK_HELDOUT_PATH)
    return _result(reasons)
