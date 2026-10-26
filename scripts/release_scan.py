"""Secret / name / internal-URL scan for a release snapshot. Never prints matched values.

Personal and company names are not written here: they are read from a file outside the
repository (default ~/.config/stira-release/denylist.txt, one word per line, # comments).
"""

from __future__ import annotations

import base64
import re
from collections import defaultdict
from pathlib import Path

DENYLIST = Path.home() / ".config" / "stira-release" / "denylist.txt"
TRACKER_LINK = "tracker_link"

PATTERNS: dict[str, re.Pattern[str]] = {
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}"),
    "slack_token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "api_key_sk": re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}"),
    "discord_webhook": re.compile(r"discord(?:app)?\.com/api/webhooks/\d+/"),
    "aws_account_arn": re.compile(r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:\d{12}:"),
    "private_ip": re.compile(
        r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"
    ),
    "home_abs_path": re.compile(
        r"(?:/Users/|/home/|[A-Za-z]:\\\\?Users\\\\?)(?!<|\{|\$|you\b|user\b|me\b|runner\b|USER|YOU\b|ALICE\b)"
        r"[A-Za-z0-9._-]+"
    ),
    "email": re.compile(
        r"\b[A-Za-z0-9._%+-]+@(?!(?:users\.noreply\.github\.com|example\.(?:com|org|net)))"
        r"[A-Za-z0-9-]+\.[A-Za-z0-9.-]*[A-Za-z]{2,}\b"
    ),
    "internal_host": re.compile(r"\b[a-z0-9-]+\.(?:local|lan|internal|corp)\b(?::\d+)?/"),
    # Issue / pull request links of the owner's repositories. The numbers in the docs are
    # from the private development tracker, so such a link is wrong in the public tree.
    TRACKER_LINK: re.compile(r"github\.com/Resolver-TNG/[^/\s]+/(?:issues|pull)/\d+"),
}


def blocking_kinds(scan_result: dict) -> list[str]:
    """Scan kinds that must not appear in a public snapshot: denylist words, tracker links.

    The other kinds are reported for review (tests carry dummy keys, IPs and paths).
    """
    return sorted(
        kind
        for kind, hit in scan_result.items()
        if (kind == TRACKER_LINK or kind.startswith("denylist_word_")) and hit.get("count")
    )


BINARY_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf"}


def load_denylist(path: Path = DENYLIST) -> list[str] | None:
    if not path.is_file():
        return None
    words = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.append(line)
    return words


def scan(root: Path, files: list[str], denylist: list[str] | None) -> dict:
    """Return {kind: {"count": n, "files": [paths]}} — counts and paths only."""
    hits: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    deny = [re.compile(re.escape(w), re.IGNORECASE) for w in (denylist or [])]
    for rel in files:
        if Path(rel).suffix.lower() in BINARY_SUFFIX:
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for kind, pat in PATTERNS.items():
            n = len(pat.findall(text))
            if n:
                hits[kind][rel] += n
        for i, pat in enumerate(deny):
            n = len(pat.findall(text))
            if n:
                hits[f"denylist_word_{i + 1}"][rel] += n
    return {
        kind: {"count": sum(per.values()), "files": sorted(per)}
        for kind, per in sorted(hits.items())
    }


# Names from the source of the previous sample persona (a game) that must never ship again.
# Not secret, so they live in the repository rather than in the denylist. Stored base64-encoded
# so that this file (which is in the snapshot) and the tests never contain or match them.
_PERSONA_WORDS_B64 = (
    "UGVyc2ljYQ==",
    "44Oa44Or44K344Kr",
    "UGVyc2ljYXJpYQ==",
    "MTZMQUI=",
    "5biw6Zu2",
    "44OL44Ol44O844Op44Or44Kv44Op44Km44OJ",
)
PERSONA_WORDS = tuple(base64.b64decode(w).decode("utf-8") for w in _PERSONA_WORDS_B64)
PERSONA_PATTERN = re.compile("|".join(re.escape(w) for w in PERSONA_WORDS), re.IGNORECASE)


def persona_word_scan(root: Path, files: list[str]) -> dict:
    """Files naming a PERSONA_WORDS word, in their text or path: {"count", "files"}.

    The snapshot must have none (make_release_snapshot exits 1 otherwise). Counts and paths
    only, like scan(). Binary files are matched by path only.
    """
    per: dict[str, int] = {}
    for rel in files:
        n = len(PERSONA_PATTERN.findall(rel))
        if Path(rel).suffix.lower() not in BINARY_SUFFIX:
            try:
                n += len(PERSONA_PATTERN.findall((root / rel).read_text(encoding="utf-8")))
            except (UnicodeDecodeError, OSError):
                pass
        if n:
            per[rel] = n
    return {"count": sum(per.values()), "files": sorted(per)}


def persona_report(root: Path, files: list[str]) -> dict:
    """Where the persona needs replacing: lines with the earlier persona name (file:line)."""
    leva = re.compile(r"\bLeva\b")
    leva_lines = []
    for rel in files:
        if Path(rel).suffix.lower() in BINARY_SUFFIX:
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for no, line in enumerate(text.splitlines(), 1):
            if leva.search(line):
                leva_lines.append(f"{rel}:{no}")
    return {"leva_lines": leva_lines}
