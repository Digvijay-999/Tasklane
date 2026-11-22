"""Masking secrets before anything is written to a log.

Every string that goes into a log line (the message, every `extra=` value, the traceback) passes
through `redact()`. It masks, in this order:

1. private key blocks (-----BEGIN ... PRIVATE KEY----- ... -----END ...);
2. credentials inside URLs: postgresql://user:pass@host -> postgresql://[REDACTED]@host;
3. `Bearer <token>` / `Basic <token>` (Authorization header values);
4. well-known token formats: OpenAI/Anthropic-style `sk-...`, AWS access key ids, GitHub,
   GitLab and Slack tokens, Google API keys, JSON Web Tokens;
5. `name=value` / `name: value` / `"name": "value"` / `name="a quoted value"` where the name
   looks sensitive (password, secret, token, api_key, authorization, credential, ...);
6. the literal values of secrets registered at startup: the database password and every
   environment variable whose *name* looks sensitive (see register_environment_secrets).

`redact_value()` also masks a whole value when its *key* looks sensitive (an `extra=` field
called `api_key`), whatever the value looks like.

This is a safety net, not a licence to log secrets: code still never logs document text, file
contents, archive file names or credentials on purpose. Pure standard library (no app imports),
so it can be tested on its own.
"""

import os
import re
import threading
from collections.abc import Mapping

REDACTED = "[REDACTED]"
# Secrets shorter than this are not registered as literals: masking a short common string
# ("test", "1234") everywhere would garble ordinary log text. Patterns 1-5 still apply.
MIN_LITERAL_SECRET_LENGTH = 8

_SENSITIVE_WORDS = (
    r"pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|client[_-]?secret|authorization|credentials?|cookie|session[_-]?id"
)
# A key (log field name, environment variable, JSON key) whose name says it holds a secret.
# Whole words only (split on _ . - and on camelCase humps), so `total_tokens` or `author` don't
# match but `accessToken` and `dbPassword` do.
_SENSITIVE_KEY = re.compile(
    rf"(?i)(?:^|[_.\-])(?:{_SENSITIVE_WORDS})(?:$|[_.\-])|api_?key|access_?key|private_?key"
)
_CAMEL_HUMP = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# Inside free text, a name merely *containing* a sensitive word counts (DB_PASSWORD_FILE=...).
_SENSITIVE_IN_NAME = re.compile(rf"(?i){_SENSITIVE_WORDS}")

# Longer strings are cut before redaction: a log field this long is abnormal, and no pattern may
# ever be fed unbounded input.
MAX_REDACT_CHARS = 64 * 1024

# Every pattern below runs in linear time on any input (a request path is attacker-controlled):
# possessive quantifiers (`*+`, `++`, `{n,}+`) never give characters back, and the lookbehinds
# let a match start only at the beginning of a run of name characters, not inside it. A test
# times redaction on a hostile input.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----"
            r".*?(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----|$)",
            re.DOTALL,
        ),
        f"{REDACTED} private key",
    ),
    # scheme://userinfo@host: the whole userinfo goes (user names are often secrets too, e.g.
    # https://<token>@github.com).
    (
        re.compile(r"(?i)(?<![a-z0-9+.\-])([a-z][a-z0-9+.\-]*+://)[^\s/@\"']++@"),
        rf"\1{REDACTED}@",
    ),
    (re.compile(r"(?i)\b(bearer|basic)\s++[A-Za-z0-9._~+/=\-]{6,}+"), rf"\1 {REDACTED}"),
    (
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_\-]{16,}+"
            r"|AKIA[0-9A-Z]{16}"
            r"|gh[pousr]_[A-Za-z0-9]{20,}+"
            r"|github_pat_[A-Za-z0-9_]{20,}+"
            r"|glpat-[A-Za-z0-9_\-]{20,}+"
            r"|xox[abprs]-[A-Za-z0-9\-]{10,}+"
            r"|AIza[0-9A-Za-z_\-]{35}"
            r"|eyJ[A-Za-z0-9_\-]{8,}+\.eyJ[A-Za-z0-9_\-]{8,}+\.[A-Za-z0-9_\-]{8,}+)"
        ),
        REDACTED,
    ),
]

# name=value, name: value, "name": "value", name='a quoted value'. The whole name is matched
# first (possessively); _mask_value() then decides whether it looks sensitive. An unquoted value
# stops at whitespace, quotes and separators; a quoted one at its closing quote.
_NAME_VALUE = re.compile(
    r"(?<![\w.\-])([\w.\-]++)"  # 1: the name
    r"([\"']?+[ \t]*+[=:][ \t]*+)"  # 2: an optional closing quote (JSON key), the separator
    r"(?:([\"'])([^\"'\n]*+)\3"  # 3, 4: a quoted value
    r"|(?!\[REDACTED\])([^\s\"',;&}\]]++))"  # 5: an unquoted value
)


def _mask_value(match: re.Match[str]) -> str:
    if not _SENSITIVE_IN_NAME.search(match.group(1)):
        return match.group(0)
    if match.group(3):
        return f"{match.group(1)}{match.group(2)}{match.group(3)}{REDACTED}{match.group(3)}"
    return f"{match.group(1)}{match.group(2)}{REDACTED}"


_literals: set[str] = set()
_literals_lock = threading.Lock()
_literal_pattern: re.Pattern[str] | None = None


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(_CAMEL_HUMP.sub("_", key)))


def register_secret(value: str | None) -> None:
    """Mask this exact value wherever it appears in a log line from now on."""
    global _literal_pattern
    if not value or len(value) < MIN_LITERAL_SECRET_LENGTH:
        return
    with _literals_lock:
        if value in _literals:
            return
        _literals.add(value)
        # Longest first, so a secret that contains another is masked whole.
        alternatives = sorted(_literals, key=len, reverse=True)
        _literal_pattern = re.compile("|".join(re.escape(v) for v in alternatives))


def register_environment_secrets(environ: Mapping[str, str] | None = None) -> None:
    """Register the value of every environment variable whose name looks sensitive
    (POSTGRES_PASSWORD, OPENAI_API_KEY, ...), so a log line that happens to contain one (an
    environment dump, a driver error) is masked."""
    for name, value in (os.environ if environ is None else environ).items():
        if is_sensitive_key(name):
            register_secret(value)


def redact(text: str) -> str:
    if not text:
        return text
    if len(text) > MAX_REDACT_CHARS:
        text = f"{text[:MAX_REDACT_CHARS]}... [truncated {len(text) - MAX_REDACT_CHARS} characters]"
    pattern = _literal_pattern
    if pattern is not None:
        text = pattern.sub(REDACTED, text)
    for regex, replacement in _PATTERNS:
        text = regex.sub(replacement, text)
    return _NAME_VALUE.sub(_mask_value, text)


def redact_value(value: object, key: str | None = None) -> object:
    """Redact one log field: strings are redacted (or masked whole if `key` looks sensitive);
    lists, tuples and dicts are walked; numbers, booleans and None pass through."""
    if isinstance(value, str):
        return REDACTED if key is not None and is_sensitive_key(key) and value else redact(value)
    if isinstance(value, Mapping):
        return {k: redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_value(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))
