"""Pure pieces of the repo_index workflow: filter rules, line chunking, the call bound, report
helpers, and the startup checks on the ZIP settings."""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.domain.enums import ProviderCallPurpose
from app.workflows.pdf.chunking import ChunkingSettings
from app.workflows.pdf.summarize import SummarySettings, reduce_summaries, summarize_document
from app.workflows.repo.chunking import chunk_files, source_lines
from app.workflows.repo.filters import (
    DEFAULT_EXCLUDED_DIRS,
    FilterRules,
    SkipReason,
    decode_source,
    language_of,
    path_skip_reason,
)
from app.workflows.repo.report import display_path, fenced
from app.workflows.repo.stages import planned_calls

RULES = FilterRules(
    excluded_dirs=FilterRules.parse_excluded_dirs(DEFAULT_EXCLUDED_DIRS),
    max_path_length=240,
    max_path_depth=20,
    max_file_bytes=1024,
)
SHA = "a" * 64

# --- Filter rules --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("src/app.py", None),
        ("README", None),
        ("docs/LICENSE", None),
        ("Dockerfile", None),
        ("build.gradle", None),
        ("SRC/APP.PY", None),  # extensions match case-insensitively
        ("node_modules/x/index.js", SkipReason.EXCLUDED_DIR),
        ("packages/a/node_modules/x/index.js", SkipReason.EXCLUDED_DIR),
        ("Node_Modules/x.js", SkipReason.EXCLUDED_DIR),
        (".git/config", SkipReason.EXCLUDED_DIR),
        ("src/__pycache__/m.py", SkipReason.EXCLUDED_DIR),
        ("dist/app.js", SkipReason.EXCLUDED_DIR),
        ("target/classes/A.java", SkipReason.EXCLUDED_DIR),
        (".venv/lib/x.py", SkipReason.EXCLUDED_DIR),
        ("__MACOSX/._README.md", SkipReason.SYSTEM_FILE),
        ("src/.DS_Store", SkipReason.SYSTEM_FILE),
        ("Thumbs.db", SkipReason.SYSTEM_FILE),
        (".env", SkipReason.HIDDEN),
        (".github/workflows/ci.yml", SkipReason.HIDDEN),
        ("src/.secret/key.py", SkipReason.HIDDEN),
        ("libs/thing.tar.gz", SkipReason.NESTED_ARCHIVE),
        ("libs/thing.7z", SkipReason.NESTED_ARCHIVE),
        ("libs/thing.jar", SkipReason.NESTED_ARCHIVE),
        ("package-lock.json", SkipReason.LOCKFILE),
        ("api/poetry.lock", SkipReason.LOCKFILE),
        ("go.sum", SkipReason.LOCKFILE),
        ("static/jquery.min.js", SkipReason.MINIFIED),
        ("static/app.bundle.js", SkipReason.MINIFIED),
        ("image.png", SkipReason.UNSUPPORTED_TYPE),
        ("app.exe", SkipReason.UNSUPPORTED_TYPE),
        ("Procfile", SkipReason.UNSUPPORTED_TYPE),
        ("a/" * 20 + "x.py", SkipReason.PATH_TOO_DEEP),
        ("a" * 241 + ".py", SkipReason.PATH_TOO_LONG),
    ],
)
def test_path_rules(path, reason):
    assert path_skip_reason(path, RULES) == reason


def test_a_name_flagged_too_long_by_the_reader_is_skipped():
    assert path_skip_reason("short.py", RULES, too_long=True) == SkipReason.PATH_TOO_LONG


def test_languages():
    assert language_of("x/main.py") == "Python"
    assert language_of("x/App.TSX") == "TypeScript"
    assert language_of("Makefile") == "Makefile"
    assert language_of(".bashrc") is None  # a leading dot is not an extension
    assert language_of("noext") is None


def test_excluded_dirs_setting():
    assert FilterRules.parse_excluded_dirs(" Foo, bar ,,") == {"foo", "bar"}
    with pytest.raises(ValueError):
        FilterRules.parse_excluded_dirs("src/vendor")


@pytest.mark.parametrize(
    ("data", "language", "reason"),
    [
        (b"print('hi')\n", "Python", None),
        (b"x = 1\x00\x01", "Python", SkipReason.BINARY),
        ("UTF-16 text".encode("utf-16"), "Text", SkipReason.BINARY),
        (bytes(range(128, 256)) * 4, "Text", SkipReason.BINARY),  # mostly invalid UTF-8
        (b"   \n\t\n", "Python", SkipReason.EMPTY),
        (b"", "Python", SkipReason.EMPTY),
        (b"var a=" + b"1+" * 600 + b"1;", "JavaScript", SkipReason.MINIFIED),
        (b"word " * 400, "Markdown", None),  # long prose lines are fine in Markdown
    ],
)
def test_content_rules(data, language, reason):
    assert decode_source(data, language)[0] == reason


def test_text_is_decoded_with_normalized_line_endings():
    reason, text = decode_source(b"\xef\xbb\xbfa\r\nb\rc\n", "Text")
    assert (reason, text) == (None, "a\nb\nc\n")


def test_a_few_invalid_bytes_are_replaced_not_rejected():
    reason, text = decode_source(b"caf\xe9 au lait, " * 20, "Text")  # Latin-1, not UTF-8
    assert reason is None and text.count(chr(0xFFFD)) == 20


# --- Line chunking -------------------------------------------------------------------------------

SETTINGS = ChunkingSettings(target_chars=200, max_chars=300, overlap_chars=40)


def _file(lines: int) -> str:
    return "".join(f"line {n}: value = compute({n})\n" for n in range(1, lines + 1))


def test_chunks_are_whole_lines_with_correct_ranges():
    chunks = chunk_files([("a.py", _file(30))], input_sha256=SHA, settings=SETTINGS)
    lines = source_lines(_file(30))
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.text == "\n".join(lines[chunk.line_start - 1 : chunk.line_end])
        assert len(chunk.text) <= SETTINGS.max_chars
    assert chunks[0].line_start == 1 and chunks[-1].line_end == 30
    # Overlap: each chunk starts at or before the line after the previous chunk's end.
    for previous, chunk in zip(chunks, chunks[1:], strict=False):
        assert chunk.line_start <= previous.line_end + 1


def test_chunks_never_span_files_and_ids_are_global_and_deterministic():
    files = [("a.py", _file(12)), ("b.py", _file(12))]
    first = chunk_files(files, input_sha256=SHA, settings=SETTINGS)
    second = chunk_files(files, input_sha256=SHA, settings=SETTINGS)
    assert first == second
    assert [c.index for c in first] == list(range(len(first)))
    assert {c.path for c in first} == {"a.py", "b.py"}
    assert all(c.chunk_id.startswith(f"c{c.index:04d}-") for c in first)
    other = chunk_files(files, input_sha256="b" * 64, settings=SETTINGS)
    assert {c.chunk_id for c in first}.isdisjoint(c.chunk_id for c in other)


def test_a_huge_line_is_cut_into_pieces_that_keep_its_line_number():
    chunks = chunk_files(
        [("m.js", "a\n" + "x" * 1000 + "\nb")], input_sha256=SHA, settings=SETTINGS
    )
    assert all(len(c.text) <= SETTINGS.max_chars for c in chunks)
    assert "".join(c.text for c in chunks).count("x") >= 1000
    assert {(c.line_start, c.line_end) for c in chunks if "x" in c.text} <= {
        (1, 2),
        (2, 2),
        (2, 3),
    }


def test_blank_lines_and_indentation_are_kept():
    text = "def f():\n\n    return 1\n"
    [chunk] = chunk_files([("f.py", text)], input_sha256=SHA, settings=SETTINGS)
    assert chunk.text == "def f():\n\n    return 1"
    assert (chunk.line_start, chunk.line_end) == (1, 3)


# --- The call bound ------------------------------------------------------------------------------


@pytest.mark.parametrize("chunks_per_file", [[1], [3], [1, 1], [5, 1, 2], [1] * 40, [12] * 9])
def test_planned_calls_is_an_upper_bound(chunks_per_file):
    settings = SummarySettings(
        chunk_max_chars=100, final_max_chars=200, combine_max_input_chars=500
    )
    calls = []

    def call(text, purpose, chunk_id, max_chars):
        calls.append(purpose)
        return "s" * max_chars  # always as long as allowed: the worst case

    summaries = []
    for k in chunks_per_file:
        doc = summarize_document([(f"c{i}", "t") for i in range(k)], call, settings)
        summaries.append(doc.final)
    if len(summaries) > 1:
        reduce_summaries([s[: settings.final_max_chars] for s in summaries], call, settings)
    assert len(calls) <= planned_calls(chunks_per_file, settings)
    assert calls.count(ProviderCallPurpose.CHUNK) == sum(chunks_per_file)


# --- Report helpers ------------------------------------------------------------------------------


def test_display_path_escapes_markdown_and_invisible_characters():
    rlo, zero_width = chr(0x202E), chr(0x200B)
    shown = display_path(f"a/[x](y)_{rlo}{zero_width}*.py")
    # Markdown renders "\\u202e" as the visible text ‮.
    assert shown == r"a/\[x\](y)\_\\u202e\\u200b\*.py"


def test_html_entities_in_names_stay_literal_text():
    """Without escaping "&", Markdown would turn "&#x202e;" into the invisible character."""
    assert display_path("src/&#x202e;gnp.py") == r"src/\&\#x202e;gnp.py"


def test_fences_are_longer_than_anything_inside():
    block = fenced("a\n~~~~~\nb")
    assert block.startswith("~~~~~~\n") and block.endswith("\n~~~~~~")
    assert fenced("plain") == "~~~\nplain\n~~~"


# --- Settings ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"zip_max_file_bytes": 10 * 2**20, "zip_max_total_bytes": 5 * 2**20},
        {"zip_max_file_bytes": 200 * 2**20, "zip_max_total_bytes": 500 * 2**20},  # memory
        {"repo_excluded_dirs": "a/b"},
        {"stub_delay_seconds": 20},  # not below the 20 s provider timeout
        {"zip_extract_timeout_seconds": 3600, "celery_task_time_limit_seconds": 3600},
    ],
    ids=["file over total", "memory too small", "path in excluded dirs", "stub delay", "limit"],
)
def test_invalid_zip_settings_are_refused_at_startup(unit_settings, overrides):
    with pytest.raises(ValidationError):
        Settings(**{**unit_settings.model_dump(), **overrides})


def test_an_empty_excluded_dirs_setting_keeps_the_defaults(unit_settings):
    settings = Settings(**{**unit_settings.model_dump(), "repo_excluded_dirs": ""})
    assert "node_modules" in settings.repo_excluded_dir_names
