"""The sandboxed PDF parser: real child processes, real limits."""

import json
import time

import pytest

from app.domain.errors import JobError
from app.workflows.pdf.sandbox import ParserLimits, run_parser
from devtools import pdf_samples

LIMITS = ParserLimits(
    max_pages=500, max_chars=2_000_000, memory_bytes=1024 * 1024 * 1024, output_max_bytes=64 * 2**20
)


def _run(tmp_path, pdf: bytes, mode: str, *, limits=LIMITS, timeout=30, **kwargs):
    source = tmp_path / "input.pdf"
    source.write_bytes(pdf)
    output = tmp_path / "out"
    result = run_parser(
        mode,
        source,
        output,
        limits=limits,
        timeout_seconds=timeout,
        keep_alive=kwargs.pop("keep_alive", lambda: None),
        keep_alive_interval=kwargs.pop("keep_alive_interval", 5),
        **kwargs,
    )
    return result, output


def _error(tmp_path, pdf: bytes, mode: str, **kwargs) -> JobError:
    with pytest.raises(JobError) as exc:
        _run(tmp_path, pdf, mode, **kwargs)
    return exc.value


# --- Real PDFs ------------------------------------------------------------------------------------


def test_validate_reports_pages(tmp_path):
    result, _ = _run(tmp_path, pdf_samples.report_pdf(page_count=4), "validate")
    assert result["pages"] == 4
    assert result["owner_password_only"] is False


def test_extract_writes_one_line_per_page_and_reports_progress(tmp_path):
    progress = []
    result, output = _run(
        tmp_path,
        pdf_samples.report_pdf(page_count=3),
        "extract",
        on_progress=lambda done, total: progress.append((done, total)),
    )
    lines = [json.loads(line) for line in output.read_text().splitlines()]
    assert [line["page"] for line in lines] == [1, 2, 3]
    assert "Section 1 discusses" in lines[0]["text"]
    assert progress == [(1, 3), (2, 3), (3, 3)]
    assert result["characters"] == sum(len(line["text"]) for line in lines)


def test_password_protected_pdf_is_rejected(tmp_path):
    assert _error(tmp_path, pdf_samples.encrypted_pdf(), "validate").error_class == "pdf_encrypted"


def test_owner_password_only_pdf_opens(tmp_path):
    result, _ = _run(tmp_path, pdf_samples.encrypted_pdf(user_password=""), "validate")
    assert result["owner_password_only"] is True


@pytest.mark.parametrize(
    "data",
    [pdf_samples.corrupt_pdf(), b"%PDF-1.7\n" + b"\x00garbage" * 50],
    ids=["truncated", "garbage"],
)
def test_corrupt_pdf_is_rejected(tmp_path, data):
    assert _error(tmp_path, data, "validate").error_class == "pdf_corrupt"


def test_image_only_pdf_extracts_no_text(tmp_path):
    _, output = _run(tmp_path, pdf_samples.image_only_pdf(page_count=2), "extract")
    assert all(json.loads(line)["text"].strip() == "" for line in output.read_text().splitlines())


def test_page_limit(tmp_path):
    limits = ParserLimits(2, LIMITS.max_chars, LIMITS.memory_bytes, LIMITS.output_max_bytes)
    error = _error(tmp_path, pdf_samples.report_pdf(page_count=4), "validate", limits=limits)
    assert error.error_class == "pdf_too_many_pages"
    assert error.safe_message == "The PDF has 4 pages; the limit is 2."


def test_text_limit_fails_instead_of_truncating(tmp_path):
    limits = ParserLimits(500, 1000, LIMITS.memory_bytes, LIMITS.output_max_bytes)
    error = _error(tmp_path, pdf_samples.report_pdf(page_count=4), "extract", limits=limits)
    assert error.error_class == "pdf_text_too_large"


# --- Enforcement mechanisms, with stand-in child scripts ------------------------------------------


def _child(tmp_path, body: str):
    script = tmp_path / "fake_child.py"
    script.write_text("import json, os, sys, time\n" + body)
    return script


def test_a_hanging_parser_is_killed_at_the_deadline(tmp_path):
    child = _child(tmp_path, "time.sleep(60)\n")
    started = time.monotonic()
    error = _error(tmp_path, b"%PDF-", "extract", timeout=1, child_script=child)
    assert error.error_class == "pdf_too_complex"
    assert time.monotonic() - started < 10


def test_a_parser_that_uses_too_much_memory_is_stopped(tmp_path):
    child = _child(tmp_path, "data = bytearray(900 * 1024 * 1024)\nprint('allocated')\n")
    limits = ParserLimits(500, 1000, 256 * 1024 * 1024, LIMITS.output_max_bytes)
    error = _error(tmp_path, b"%PDF-", "extract", limits=limits, child_script=child)
    assert error.error_class == "pdf_too_complex"


def test_a_parser_that_writes_too_much_output_is_stopped(tmp_path):
    child = _child(
        tmp_path,
        "with open(sys.argv[3], 'wb') as f:\n    f.write(b'x' * (8 * 1024 * 1024))\n",
    )
    limits = ParserLimits(500, 1000, LIMITS.memory_bytes, 1024 * 1024)
    error = _error(tmp_path, b"%PDF-", "extract", limits=limits, child_script=child)
    assert error.error_class == "pdf_too_complex"


def test_the_lease_is_renewed_while_waiting(tmp_path):
    child = _child(
        tmp_path,
        "time.sleep(1.2)\nprint(json.dumps({'type': 'result', 'pages': 1}))\nsys.stdout.flush()\n",
    )
    renewals = []
    _run(
        tmp_path,
        b"%PDF-",
        "validate",
        child_script=child,
        keep_alive=lambda: renewals.append(time.monotonic()),
        keep_alive_interval=0.2,
    )
    assert len(renewals) >= 4  # at least every interval while the child was busy


def test_losing_the_job_kills_the_parser(tmp_path):
    child = _child(tmp_path, "time.sleep(60)\n")

    class Fenced(Exception):
        pass

    def fenced():
        raise Fenced

    started = time.monotonic()
    with pytest.raises(Fenced):
        _run(
            tmp_path,
            b"%PDF-",
            "extract",
            child_script=child,
            keep_alive=fenced,
            keep_alive_interval=0.1,
        )
    assert time.monotonic() - started < 10


def test_a_crashing_parser_is_an_internal_error_not_an_input_error(tmp_path):
    child = _child(tmp_path, "raise ValueError('bug in our parser code')\n")
    with pytest.raises(RuntimeError):
        _run(tmp_path, b"%PDF-", "extract", child_script=child)


def test_unknown_error_classes_from_the_child_are_not_trusted(tmp_path):
    child = _child(
        tmp_path,
        "print(json.dumps({'type': 'error', 'error_class': 'made_up', 'params': {}}))\n",
    )
    assert _error(tmp_path, b"%PDF-", "extract", child_script=child).error_class == "internal_error"


def test_the_parser_gets_no_secrets_and_cannot_import_the_app(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "super-secret")
    monkeypatch.setenv("SOME_API_KEY", "sk-123")
    child = _child(
        tmp_path,
        "try:\n    import app\n    importable = True\nexcept ImportError:\n    importable = False\n"
        "print(json.dumps({'type': 'result', 'env': sorted(os.environ), 'app': importable}))\n",
    )
    result, _ = _run(tmp_path, b"%PDF-", "validate", child_script=child)
    assert "POSTGRES_PASSWORD" not in result["env"] and "SOME_API_KEY" not in result["env"]
    assert result["app"] is False


def test_limits_are_applied_inside_the_child(tmp_path):
    """limited_launcher.py sets the limits in the child itself (no preexec_fn in a threaded
    worker)."""
    child = _child(
        tmp_path,
        "import resource\n"
        "limits = {name: resource.getrlimit(getattr(resource, name))[0]\n"
        "          for name in ('RLIMIT_AS', 'RLIMIT_CPU', 'RLIMIT_FSIZE', 'RLIMIT_CORE')}\n"
        "print(json.dumps({'type': 'result', **limits}))\n",
    )
    limits = ParserLimits(500, 1000, 512 * 1024 * 1024, 2 * 1024 * 1024)
    result, _ = _run(tmp_path, b"%PDF-", "validate", limits=limits, timeout=7, child_script=child)
    assert result == {
        "RLIMIT_AS": 512 * 1024 * 1024,
        "RLIMIT_CPU": 12,  # timeout + 5
        "RLIMIT_FSIZE": 2 * 1024 * 1024,
        "RLIMIT_CORE": 0,
    }


def _alive(pid: int) -> bool:
    """True unless the process is gone or only a zombie (killed, waiting to be reaped)."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def test_processes_started_by_the_parser_are_killed_too(tmp_path):
    """The whole process group is killed, not just the direct child."""
    pid_file = tmp_path / "grandchild.pid"
    child = _child(
        tmp_path,
        "import subprocess\n"
        "helper = subprocess.Popen(['sleep', '60'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(helper.pid))\n"
        "time.sleep(60)\n",
    )
    assert _error(tmp_path, b"%PDF-", "extract", timeout=1, child_script=child)
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild)
