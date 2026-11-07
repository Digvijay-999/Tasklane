"""Which files of a repository are indexed, and why each other file is skipped (pure functions).

The rule is an ALLOWLIST: a file is included only if its extension (or exact name, for a few
files without one) is a known text or code type. Everything else is skipped with a reason code.
Rules that need only the path run BEFORE extraction (so excluded folders like node_modules are
never even decompressed); rules that need the bytes (binary, empty, minified) run after.

Order of the path rules (the first that matches is the reason):
    path_too_long, path_too_deep, excluded_dir, system_file, hidden, nested_archive, lockfile,
    minified (by name), unsupported_type
"""

from dataclasses import dataclass
from enum import StrEnum


class SkipReason(StrEnum):
    SYMLINK = "symlink"  # never created or read
    SPECIAL_FILE = "special_file"  # device, pipe or socket entry
    PATH_TOO_LONG = "path_too_long"
    PATH_TOO_DEEP = "path_too_deep"
    EXCLUDED_DIR = "excluded_dir"  # inside a configured folder (.git, node_modules, ...)
    SYSTEM_FILE = "system_file"  # __MACOSX/, .DS_Store, Thumbs.db, desktop.ini
    HIDDEN = "hidden"  # a name starting with "." (may hold secrets, e.g. .env)
    NESTED_ARCHIVE = "nested_archive"  # never extracted
    LOCKFILE = "lockfile"
    MINIFIED = "minified"
    UNSUPPORTED_TYPE = "unsupported_type"  # not on the allowlist
    TOO_LARGE = "too_large"  # over the per-file limit (declared or real size)
    BINARY = "binary"  # NUL byte in the first 8 KiB, or mostly invalid UTF-8
    EMPTY = "empty"  # no visible characters


DEFAULT_EXCLUDED_DIRS = (
    ".git,.hg,.svn,node_modules,bower_components,dist,build,target,__pycache__,.venv,venv,"
    ".tox,.nox,.mypy_cache,.pytest_cache,.ruff_cache,.idea,.vscode,coverage,htmlcov,.next,"
    ".nuxt,.gradle,.terraform,.cache,site-packages,vendor"
)

# Extension -> language (the allowlist).
LANGUAGES: dict[str, str] = {
    **dict.fromkeys([".py", ".pyi"], "Python"),
    **dict.fromkeys([".js", ".mjs", ".cjs", ".jsx"], "JavaScript"),
    **dict.fromkeys([".ts", ".mts", ".cts", ".tsx"], "TypeScript"),
    ".java": "Java",
    **dict.fromkeys([".kt", ".kts"], "Kotlin"),
    ".scala": "Scala",
    ".groovy": "Groovy",
    ".gradle": "Gradle",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    **dict.fromkeys([".c", ".h"], "C"),
    **dict.fromkeys([".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"], "C++"),
    ".cs": "C#",
    ".fs": "F#",
    ".swift": "Swift",
    **dict.fromkeys([".m", ".mm"], "Objective-C"),
    ".dart": "Dart",
    ".lua": "Lua",
    **dict.fromkeys([".r", ".R"], "R"),
    ".jl": "Julia",
    **dict.fromkeys([".ex", ".exs"], "Elixir"),
    ".erl": "Erlang",
    ".hs": "Haskell",
    **dict.fromkeys([".clj", ".cljs"], "Clojure"),
    **dict.fromkeys([".pl", ".pm"], "Perl"),
    **dict.fromkeys([".sh", ".bash", ".zsh"], "Shell"),
    **dict.fromkeys([".ps1", ".psm1"], "PowerShell"),
    **dict.fromkeys([".bat", ".cmd"], "Batch"),
    ".sql": "SQL",
    **dict.fromkeys([".html", ".htm"], "HTML"),
    ".css": "CSS",
    **dict.fromkeys([".scss", ".sass", ".less"], "CSS"),
    ".vue": "Vue",
    ".svelte": "Svelte",
    **dict.fromkeys([".md", ".markdown"], "Markdown"),
    ".rst": "reStructuredText",
    ".txt": "Text",
    ".json": "JSON",
    **dict.fromkeys([".yaml", ".yml"], "YAML"),
    ".toml": "TOML",
    **dict.fromkeys([".ini", ".cfg", ".conf"], "Config"),
    ".xml": "XML",
    ".proto": "Protocol Buffers",
    **dict.fromkeys([".graphql", ".gql"], "GraphQL"),
    ".tf": "Terraform",
    ".cmake": "CMake",
    ".mk": "Makefile",
}

# Exact file names without a (known) extension that are included.
NAMED_FILES: dict[str, str] = {
    "readme": "Text",
    "license": "Text",
    "makefile": "Makefile",
    "dockerfile": "Dockerfile",
}

LOCKFILES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "poetry.lock",
        "pipfile.lock",
        "uv.lock",
        "pdm.lock",
        "cargo.lock",
        "composer.lock",
        "gemfile.lock",
        "go.sum",
        "mix.lock",
        "pubspec.lock",
        "packages.lock.json",
        "flake.lock",
        "gradle.lockfile",
    }
)

NESTED_ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".tbz",
    ".tbz2",
    ".xz",
    ".txz",
    ".zst",
    ".lz",
    ".lz4",
    ".lzma",
    ".z",
    ".7z",
    ".rar",
    ".cab",
    ".iso",
    ".dmg",
    ".jar",
    ".war",
    ".ear",
    ".aar",
    ".apk",
    ".ipa",
    ".whl",
    ".egg",
    ".nupkg",
    ".vsix",
    ".deb",
    ".rpm",
)

SYSTEM_FOLDERS = frozenset({"__macosx"})
SYSTEM_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini"})
MINIFIED_MARKERS = (".min.", "-min.", ".bundle.")
# Content checks.
BINARY_SNIFF_BYTES = 8192
REPLACEMENT_CHAR = chr(0xFFFD)  # what errors="replace" puts in place of invalid bytes
BYTE_ORDER_MARK = chr(0xFEFF)
MAX_INVALID_UTF8_SHARE = 0.10  # more than 10% replacement characters: treated as binary
MINIFIABLE_LANGUAGES = frozenset({"JavaScript", "TypeScript", "CSS", "JSON", "HTML"})
MINIFIED_MIN_CHARS = 1000
MINIFIED_AVERAGE_LINE = 250  # characters per line on average


@dataclass(frozen=True)
class FilterRules:
    excluded_dirs: frozenset[str]  # casefolded folder names
    max_path_length: int
    max_path_depth: int  # path components, e.g. "a/b/c.py" has 3
    max_file_bytes: int

    @staticmethod
    def parse_excluded_dirs(value: str) -> frozenset[str]:
        names = frozenset(name.strip().casefold() for name in value.split(",") if name.strip())
        if any("/" in name or "\\" in name for name in names):
            raise ValueError("REPO_EXCLUDED_DIRS lists folder names, not paths")
        return names


def language_of(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    if name.casefold() in NAMED_FILES:
        return NAMED_FILES[name.casefold()]
    dot = name.rfind(".")
    if dot <= 0:
        return None
    extension = name[dot:]
    return LANGUAGES.get(extension) or LANGUAGES.get(extension.lower())


def path_skip_reason(path: str, rules: FilterRules, *, too_long: bool = False) -> SkipReason | None:
    """Why a file is skipped based on its path alone, or None if it may be included."""
    parts = path.split("/")
    folders = [part.casefold() for part in parts[:-1]]
    name = parts[-1].casefold()
    if too_long or len(path) > rules.max_path_length:
        return SkipReason.PATH_TOO_LONG
    if len(parts) > rules.max_path_depth:
        return SkipReason.PATH_TOO_DEEP
    if any(folder in rules.excluded_dirs for folder in folders):
        return SkipReason.EXCLUDED_DIR
    if any(folder in SYSTEM_FOLDERS for folder in folders) or name in SYSTEM_NAMES:
        return SkipReason.SYSTEM_FILE
    if any(part.startswith(".") for part in parts):
        return SkipReason.HIDDEN
    if name.endswith(NESTED_ARCHIVE_SUFFIXES):
        return SkipReason.NESTED_ARCHIVE
    if name in LOCKFILES:
        return SkipReason.LOCKFILE
    if any(marker in name for marker in MINIFIED_MARKERS):
        return SkipReason.MINIFIED
    if language_of(path) is None:
        return SkipReason.UNSUPPORTED_TYPE
    return None


def decode_source(data: bytes, language: str) -> tuple[SkipReason | None, str]:
    """(reason, text): reason is set if the content can't be indexed; text is the decoded file
    with normalized line endings (only meaningful when reason is None)."""
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        return SkipReason.BINARY, ""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
        if text.count(REPLACEMENT_CHAR) > len(text) * MAX_INVALID_UTF8_SHARE:
            return SkipReason.BINARY, ""
    text = text.removeprefix(BYTE_ORDER_MARK).replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return SkipReason.EMPTY, ""
    if language in MINIFIABLE_LANGUAGES and len(text) >= MINIFIED_MIN_CHARS:
        lines = text.count("\n") + 1
        if len(text) / lines > MINIFIED_AVERAGE_LINE:
            return SkipReason.MINIFIED, ""
    return None, text
