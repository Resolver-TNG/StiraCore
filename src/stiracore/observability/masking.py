"""Secret masking shared by tool previews, turn events, APIs and exports (#185).

Flask-free so that non-web exporters (Langfuse/OTel) can apply the same rules.
"""

from __future__ import annotations

import logging
import math
import re

__all__ = [
    "MASK",
    "MaskingFilter",
    "MaskingFormatter",
    "install_record_masking",
    "looks_high_entropy",
    "mask_log_record",
    "mask_secrets",
]

# Applied to tool previews/payloads (#188), turn events (#186) and exports.
# Known credential formats are matched precisely; the generic high-entropy rule
# is a conservative fallback that leaves hex digests (SHA/UUID), numeric IDs and
# word-like identifiers alone. Masking is idempotent (masked text re-masks to
# itself), so callers may apply it more than once.

MASK = "***"
_SECRET_WORDS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "pwd",
        "secret",
        "secrets",
        "token",
        "authorization",
        "credential",
        "credentials",
        "apikey",
        "cookie",
    }
)
# "<prefix>_key" names are secrets; a bare "key" (e.g. memory_write's key) is not.
_KEY_PREFIXES = frozenset(
    {"api", "access", "secret", "private", "signing", "encryption", "client", "master", "auth"}
)
# Counters such as max_tokens/token_count are metadata, not credentials; so are
# settings about a secret (#601: password_policy, password_min_length, secret_type).
_TOKEN_METADATA = frozenset({"count", "counts", "limit", "limits", "budget", "usage"})
_SECRET_METADATA = _TOKEN_METADATA | frozenset(
    {
        "policy",
        "policies",
        "length",
        "min",
        "max",
        "minimum",
        "maximum",
        "expiry",
        "expires",
        "expired",
        "expiration",
        "age",
        "strength",
        "rule",
        "rules",
        "required",
        "type",
        "kind",
    }
)


def _is_secret_key(name: str) -> bool:
    """Whether a mapping key / assignment name denotes a credential.

    camelCase is split into words; a name in mixed case that does not split into
    words ("PassWord", "tOkeN") is read in lower case too (#601: either reading wins)."""
    return _secret_words(name) or (name != name.lower() and _secret_words(name.lower()))


def _secret_words(name: str) -> bool:
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()
    parts = [part for part in re.split(r"[^a-z0-9]+", snake) if part]
    for index, part in enumerate(parts):
        following = parts[index + 1] if index + 1 < len(parts) else ""
        if part in _SECRET_WORDS:
            if following in _SECRET_METADATA:
                continue  # "password_policy", "token_count": about a secret, not one
            return True
        if part == "key" and index > 0 and parts[index - 1] in _KEY_PREFIXES:
            return True
    return False


_PEM = re.compile(
    r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?)-----[\s\S]*?(?:-----END \1-----|\Z)"
)
# Every pattern stays linear on large payloads (tool output is retained up to
# 1 MiB): starts are anchored by a lookbehind or a literal prefix, repetitions
# are bounded, and key names are classified in Python instead of by the regex.
_URL_USERINFO = re.compile(
    r"(?<![A-Za-z0-9+.-])([a-zA-Z][a-zA-Z0-9+.-]{0,31}://)([^\s/:@]{0,256}):([^\s/@]{1,256})@"
)
_KV = re.compile(
    r"(?<![A-Za-z0-9_.-])(?P<key>[A-Za-z0-9_.-]{1,128})"
    r"(?P<sep>[\"']?[ \t]{0,8}[:=][ \t]{0,8}[\"']?)"
    r"(?P<value>(?:(?:Basic|Bearer|Token|Digest)[ \t]+)?[^\s\"',;&}<>]{1,4096})"
)
# #601: the first pass over assignments (``_mask_secret_values``). Only the key and the
# separator are matched, so a non-secret value is never consumed: whatever it contains
# (``message="failed password=banana"``) is still scanned.
# A key: name characters, or JSON escapes of them (``pass\u0077ord``) at any level.
# It never starts inside a run of backslashes (linear on "\\\\\\..."), but may start right
# after an escaped line break or tab ("line one\\npassword=..." inside a JSON string).
_KEY_TOKEN = re.compile(
    r"(?:(?<![A-Za-z0-9_.\\-])|(?<=\\[nrt]))(?:[A-Za-z0-9_.-]|\\+[uU][0-9A-Fa-f]{4}){1,256}"
)
# A cheap pre-filter: every secret name contains one of these (escapes are decoded).
_SECRET_HINT = re.compile(r"pass|pwd|secret|token|auth|credential|key|cookie|\\", re.IGNORECASE)
_KEY_ESCAPE = re.compile(r"(\\+)[uU]([0-9A-Fa-f]{4})")
# The key's closing quote, at its encoding level (``"``, ``\"``, ``\\\"``, ``\u0022``).
_KEY_CLOSE = re.compile(r"(\\*)(?:([\"'])|(?<=\\)[uU]00(?:22|27))")
_LINE_BREAKS = frozenset("\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
_HEX4 = re.compile(r"[0-9A-Fa-f]{4}")


def _skip_space(text: str, pos: int, lines: bool) -> int:
    """Past the whitespace at ``pos`` (one forward pass, no bound): Unicode spaces, and
    the same spelled as escapes inside a string (``\\t``, ``\\u00a0``, any level).
    Line breaks (also ``\\n`` / ``\\r``) only with ``lines``: after a quoted key. A bare
    ``password`` never reaches a ``:`` on the next line, nor ``password:`` a value."""
    size = len(text)
    while pos < size:
        char = text[pos]
        if char.isspace():
            if not lines and char in _LINE_BREAKS:
                return pos
            pos += 1
            continue
        if char != "\\":
            return pos
        end = pos
        while end < size and text[end] == "\\":
            end += 1
        if end >= size:
            return pos
        escape = text[end]
        if escape == "t" or (lines and escape in "nr"):
            pos = end + 1
            continue
        if escape in "uU" and _HEX4.fullmatch(text, end + 1, end + 5):
            decoded = chr(int(text[end + 1 : end + 5], 16))
            if decoded.isspace() and (lines or decoded not in _LINE_BREAKS):
                pos = end + 5
                continue
        return pos
    return pos


_UNQUOTED_VALUE = re.compile(r"(?:(?:Basic|Bearer|Token|Digest)[ \t]+)?[^\s\"',;&}<>]+")
_AUTH_SCHEME = re.compile(r"(?:Basic|Bearer|Token|Digest)[ \t]+")
# A query / fragment parameter of a URL (``?api_key=…``, ``&token=…``). ``_KV`` alone
# misses the first one: its match starts at the scheme (``https`` + ``:``) and runs over
# the whole URL, so the parameter inside is never looked at (ADR 0012 phase 6).
_URL_PARAM = re.compile(
    # #601: the value runs to its end (no bound: a long secret is never half shown).
    r"(?<=[?&#;])(?P<key>[A-Za-z0-9_.-]{1,128})(?P<sep>=)(?P<value>[^\s\"'&#<>]+)"
)
# Prose answers ("password: no") are not values; a real one is masked whatever its length.
_NOT_SECRET_VALUES = frozenset(
    {"true", "false", "null", "none", "undefined", "no", "yes", "n/a", MASK}
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
_PREFIXED = (
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), r"\1" + "****"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE), r"\1" + MASK),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "sk-" + MASK),
    (re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{20,}"), r"\1" + MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "github_pat_" + MASK),
    (re.compile(r"\b(xox[abposre]-|xapp-)[A-Za-z0-9-]{8,}"), r"\1" + MASK),
    (re.compile(r"(hooks\.slack\.com/services/)[A-Za-z0-9/_-]+"), r"\1" + MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "AIza" + MASK),
)
# Base64/base62-ish runs; "/" and "." split tokens so paths and dotted names stay intact.
_CANDIDATE = re.compile(r"(?<![A-Za-z0-9+_=-])[A-Za-z0-9+_=-]{32,}(?![A-Za-z0-9+_=-])")
_WORD_RUN = re.compile(r"[a-z]{3,}|[A-Z]{3,}")
HIGH_ENTROPY_MIN_LEN = 32
HIGH_ENTROPY_MIN_BITS = 4.0


def _entropy(text: str) -> float:
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    size = len(text)
    return -sum(n / size * math.log2(n / size) for n in counts.values())


def looks_high_entropy(token: str) -> bool:
    """Random-looking credential: long, mixed case and digits, not word-like.

    Hex digests (SHA-1/256, UUIDs) lack upper+lower+digit and are kept; so are
    camelCase/snake_case identifiers, whose letters mostly form 3+ letter runs.
    """
    if len(token) < HIGH_ENTROPY_MIN_LEN:
        return False
    upper = sum(c.isupper() for c in token)
    lower = sum(c.islower() for c in token)
    digits = sum(c.isdigit() for c in token)
    if min(upper, lower, digits) < 2:
        return False
    if _entropy(token) < HIGH_ENTROPY_MIN_BITS:
        return False
    worded = sum(len(m.group()) for m in _WORD_RUN.finditer(token))
    return worded / len(token) < 0.5


def _kept_value(plain: str) -> bool:
    """A secret-named value that is not a secret: a prose answer, or already masked."""
    return plain.lower() in _NOT_SECRET_VALUES or _AUTH_SCHEME.sub("", plain, count=1) == MASK


# An opening quote: ``"`` / ``'`` / ``` ` ```, or JSON's own ``\u0022`` / ``\u0027``,
# each written at some encoding level (``\"``, ``\\\"``, ``\\u0022`` ...).
_OPEN_QUOTE = re.compile(r"(\\*)(?:([\"'`])|(?<=\\)[uU]00(22|27))")
_QUOTE_CODES = {'"': "22", "'": "27"}
_LINE_END = re.compile(r"[^\r\n]*")
_NEXT_KEY = re.compile(r"[A-Za-z0-9_.-]{1,128}=")
# After an unquoted value, a quote that ends an enclosing string is followed by one of
# these (or the end); any other character means the value went on (fallback).
_AFTER_CLOSE = frozenset(" \t\r\n,;}])")


def _decode_steps(k: int, unicode_token: bool, levels: int) -> tuple[str, int, bool]:
    """Peel ``levels`` JSON encodings off a quote-like token preceded by ``k``
    backslashes. ``("outer", ..)``: it ends an enclosing string on the way; otherwise
    ``("here", k, unicode_token)`` as seen at the value's own level."""
    for _ in range(levels):
        if unicode_token:
            if k % 2:
                unicode_token, k = False, (k - 1) // 2  # "\u0022" -> '"'
            else:
                k //= 2  # an escaped backslash: still the text "\u0022"
        elif k % 2 == 0:
            return "outer", k, False  # unescaped at this level: its string ends
        else:
            k = (k - 1) // 2
    return "here", k, unicode_token


def _opener_levels(k: int, unicode_token: bool) -> int | None:
    """How many encodings sit between the text and the value an opener starts (``\"``
    -> 1, ``\\\"`` -> 2, ``\u0022`` -> 1), or ``None`` if it opens nothing."""
    levels = 0
    while unicode_token or k:
        if not unicode_token and k % 2 == 0:
            return None  # "\\\"" style: an escaped backslash, then a string end
        role, k, unicode_token = _decode_steps(k, unicode_token, 1)
        if role == "outer":
            return None
        levels += 1
        if levels > 8:
            return None
    return levels


def _quoted_end(text: str, start: int, quote: str, levels: int = 0) -> tuple[int, int | None]:
    """End of a value quoted by ``quote`` at ``levels`` encodings (one forward pass).

    It ends at its own closing quote (``"``/``'`` or ``\u0022``/``\u0027``, decoded
    level by level), at a quote that closes an enclosing string, at a line break (even
    right after a backslash: the next line is never masked) or at the end of the
    text. A YAML single-quoted ``''`` is a quote inside the value. Returns ``(end,
    after)``: ``after`` is the index past its own closing quote, else ``None``."""
    index, size, run = start, len(text), 0
    code = _QUOTE_CODES.get(quote)
    while index < size:
        char = text[index]
        if char in "\r\n":
            return index - run, None
        token = None
        if char == quote:
            token = False
        elif (
            code is not None and run and char in "uU" and text[index + 1 : index + 5] == "00" + code
        ):
            token = True
        if token is False and quote == "'" and levels == 0 and text[index + 1 : index + 2] == "'":
            # YAML '' inside a single-quoted value (YAML has no backslash escapes there,
            # so this wins over the backslash count; masking a little more is safe).
            index, run = index + 2, 0
            continue
        if token is not None:
            role, k, still_unicode = _decode_steps(run, token, levels)
            if role == "outer":
                return index - run, None
            if not still_unicode and k % 2 == 0:
                return index - run, index + (5 if token else 1)
        run = run + 1 if char == "\\" else 0
        index += 1
    return size, None


def _closes_here(text: str, pos: int) -> bool:
    """Whether a value really ended at ``pos``: what follows is the end, whitespace or a
    separator, possibly after quotes that end enclosing strings (``"``, ``\\"``,
    ``\\u0022`` ... or an escaped line break). Anything else glued to it (``"a"SECRET``)
    means the value went on. Bounded: at most 16 enclosing quotes are looked at."""
    size = len(text)
    for _ in range(16):
        start = pos
        while pos < size and text[pos] == "\\":
            pos += 1
        if pos >= size:
            return True
        char = text[pos]
        if char in "\"'`":
            pos += 1
            continue
        if pos > start:
            if char in "nrt":
                return True  # an escaped line break / tab of an enclosing string
            if char in "uU" and text[pos + 1 : pos + 5] in ("0022", "0027"):
                pos += 5
                continue
            return False
        return char in _AFTER_CLOSE
    return False


def _line_rest(text: str, start: int) -> tuple[int, int]:
    """The fallback: from ``start`` to the end of its line (never the next line)."""
    return start, _LINE_END.match(text, start).end()


def _container_end(text: str, start: int, levels: int) -> int:
    """End of a JSON array/object value at ``levels`` encodings (one forward pass):
    brackets outside its strings are counted; a quote that ends an enclosing string, or
    a raw line break inside an escaped one, ends it too. Unclosed: the end of the text."""
    index, size, run, depth, in_string = start, len(text), 0, 0, False
    while index < size:
        char = text[index]
        if char in "\r\n" and levels:
            return index - run
        token = None
        if char == '"':
            token = False
        elif char in "uU" and run and text[index + 1 : index + 5] == "0022":
            token = True
        if token is not None:
            role, k, still_unicode = _decode_steps(run, token, levels)
            if role == "outer":
                return index - run
            if not still_unicode and k % 2 == 0:
                in_string = not in_string
        elif not in_string and char in "[{":
            depth += 1
        elif not in_string and char in "]}":
            depth -= 1
            if depth <= 0:
                return index + 1
        run = run + 1 if char == "\\" else 0
        index += 1
    return size


def _value_extent(text: str, start: int, json_key: bool, levels: int = 0) -> tuple[int, int] | None:
    """``(start, end)`` of the value at ``start``; ``None`` only for an empty value.

    After a quoted (JSON) key any whitespace may come first, line breaks included;
    after a bare key only spaces on the same line. A form that is not recognized falls
    back to the rest of the line: a secret-named key with a separator never leaves its
    value as it was."""
    start = _skip_space(text, start, lines=json_key)
    size = len(text)
    if start >= size or text[start] in "\r\n":
        return None
    if text[start] in "[{":
        return start, _container_end(text, start, levels)
    opener = _OPEN_QUOTE.match(text, start)
    if opener is not None:
        unicode_token = opener.group(2) is None
        quote = '"' if opener.group(3) == "22" else "'" if unicode_token else opener.group(2)
        levels = _opener_levels(len(opener.group(1)), unicode_token)
        if levels is not None:
            end, after = _quoted_end(text, opener.end(), quote, levels)
            if (after is None and end == opener.end()) or (
                after is not None and not _closes_here(text, after)
            ):
                # An empty value that is not properly closed, or 'password="a"SECRET':
                # the extent is not known, so the rest of the line is masked.
                return _line_rest(text, opener.end())
            return opener.end(), end
        if not opener.group(1):
            return None
    run = _UNQUOTED_VALUE.match(text, start)
    if run is None:
        if text[start] in ",;}])":
            return None  # an empty value before a terminator
        if text[start] == "&" and _NEXT_KEY.match(text, start + 1):
            return None  # "password=&user=bob": empty
        return _line_rest(text, start)
    end = run.end()
    while end > start and text[end - 1] == "\\" and end < size and text[end] in "\"'":
        end -= 1  # "password=***"" inside a JSON string: the backslash is the outer quote's
    if end < size:
        char = text[end]
        if char in "\"'`\\" and not _closes_here(text, end):
            return _line_rest(text, start)  # 'password=ab"cd tail': the value goes on
        if char == "&" and not _NEXT_KEY.match(text, end + 1):
            return _line_rest(text, start)  # "&quot;" and the like: not a next parameter
        if char == "<":
            return _line_rest(text, start)
    return start, end


def _assignment(text: str, match: re.Match) -> tuple[int, bool, int] | None:
    """For a secret-named key: ``(value position, quoted key + ":", key level)``.

    The key is decoded first (``pass\u0077ord``, ``\u0022`` quotes, upper case). A
    quoted key may be followed by any whitespace before its ":" (line breaks too); a
    bare key only by spaces on the same line."""
    raw = match.group()
    key = _KEY_ESCAPE.sub(lambda m: chr(int(m.group(2), 16)), raw) if "\\" in raw else raw
    if not _is_secret_key(key.strip("\"'")):
        return None
    quoted, levels, pos = False, 0, match.end()
    if key[-1:] in "\"'":  # its closing quote was a \u0022 inside the token
        last = list(_KEY_ESCAPE.finditer(raw))[-1]
        quoted, levels = True, _opener_levels(len(last.group(1)), True) or 0
    else:
        close = _KEY_CLOSE.match(text, pos)
        if close is not None:
            quoted = True
            levels = _opener_levels(len(close.group(1)), close.group(2) is None) or 0
            pos = close.end()
    pos = _skip_space(text, pos, lines=quoted)
    if pos >= len(text) or text[pos] not in ":=":
        return None
    return pos + 1, quoted and text[pos] == ":", levels


def _mask_secret_values(text: str) -> str:
    """#601: mask the whole value of every secret-named assignment, whatever its length.

    Forms: ``key=value``, ``key: value``, ``"key": "value"`` (JSON, also with a line
    break before the value), each quoted or not, and each also escaped inside another
    string (``\"key\": \"value\"``). A quoted value runs to its closing quote (spaces
    and escaped quotes included); an unquoted one to its end. Linear: each character
    is scanned a bounded number of times, and a masked value is skipped."""
    pieces: list[str] = []
    cursor = 0
    for match in _KEY_TOKEN.finditer(text):
        if match.start() < cursor or _SECRET_HINT.search(match.group()) is None:
            continue
        found = _assignment(text, match)
        if found is None:
            continue
        extent = _value_extent(text, *found)
        if extent is None:
            continue
        value_start, value_end = extent
        plain = text[value_start:value_end]
        if not plain or _kept_value(plain):
            continue
        pieces.append(text[cursor:value_start])
        pieces.append(MASK)
        cursor = value_end
    if not pieces:
        return text
    pieces.append(text[cursor:])
    return "".join(pieces)


def _mask_kv(match: re.Match) -> str:
    value = match.group("value")
    plain = value.strip("\"'")
    # #601: a secret-named value is masked whole whatever it looks like (digits, short,
    # ending in "***"); only an already masked value ("***", "Bearer ***") is kept.
    if not _is_secret_key(match.group("key")) or _kept_value(plain):
        return match.group(0)
    return match.group("key") + match.group("sep") + MASK


def _mask_string(text: str) -> str:
    text = _PEM.sub(lambda m: f"-----BEGIN {m.group(1)}-----{MASK}-----END {m.group(1)}-----", text)
    # #601: secret-named values first, on the text as written: no later pass may move a
    # quote or drop a backslash before their extent is known. Then URL credentials and
    # URL parameters (their own value grammar: "," belongs to a query value).
    text = _mask_secret_values(text)
    text = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{m.group(2)}:{MASK}@", text)
    text = _URL_PARAM.sub(_mask_kv, text)
    text = _KV.sub(_mask_kv, text)
    text = _JWT.sub("eyJ" + MASK, text)
    for pattern, replacement in _PREFIXED:
        text = pattern.sub(replacement, text)
    return _CANDIDATE.sub(lambda m: MASK if looks_high_entropy(m.group()) else m.group(), text)


def mask_secrets(value):
    """Return a masked copy: secret-named keys become ``***``; strings are scanned."""
    if isinstance(value, dict):
        return {
            k: MASK if isinstance(k, str) and _is_secret_key(k) else mask_secrets(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(mask_secrets(v) for v in value)
    if isinstance(value, str):
        return _mask_string(value)
    return value


class MaskingFormatter(logging.Formatter):
    """A ``logging.Formatter`` whose whole output goes through :func:`mask_secrets`.

    Message, arguments and the formatted traceback alike (#291): an exception whose
    text carries a credential is logged masked.
    """

    def format(self, record: logging.LogRecord) -> str:
        return mask_secrets(super().format(record))


_TRACEBACK_FORMATTER = logging.Formatter()


def mask_log_record(record: logging.LogRecord) -> logging.LogRecord:
    """Mask a record in place, before any handler or formatter sees it (#291).

    The message is rendered once (``msg % args``) and masked, ``args`` dropped; a
    traceback is rendered into ``exc_text`` and masked (formatters reuse
    ``exc_text``), likewise ``stack_info``. Idempotent. A message that cannot be
    rendered keeps its shape with every argument masked as text.
    """
    if getattr(record, "_stira_masked", False):
        return record
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - logging's own error path reports it
        record.msg = mask_secrets(str(record.msg))
        if isinstance(record.args, dict):
            record.args = {k: mask_secrets(str(v)) for k, v in record.args.items()}
        elif record.args:
            record.args = tuple(mask_secrets(str(arg)) for arg in record.args)
    else:
        record.msg, record.args = mask_secrets(message), None
    if record.exc_info and not record.exc_text:
        record.exc_text = _TRACEBACK_FORMATTER.formatException(record.exc_info)
    if record.exc_text:
        record.exc_text = mask_secrets(record.exc_text)
    if record.stack_info:
        record.stack_info = mask_secrets(record.stack_info)
    record._stira_masked = True
    return record


class MaskingFilter(logging.Filter):
    """A handler/logger filter that masks every record it passes (never drops one)."""

    def filter(self, record: logging.LogRecord) -> bool:
        mask_log_record(record)
        return True


def install_record_masking() -> None:
    """Mask every record at creation, whichever handler writes it (process-wide).

    Wraps the current log-record factory, so handlers registered before (e.g. by
    ``logging.basicConfig``) or after this call never see plain text. Idempotent.
    """
    factory = logging.getLogRecordFactory()
    if getattr(factory, "_stira_masking", False):
        return

    def masked(*args, **kwargs):
        return mask_log_record(factory(*args, **kwargs))

    masked._stira_masking = True
    masked._stira_previous = factory
    logging.setLogRecordFactory(masked)
