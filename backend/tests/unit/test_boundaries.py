"""Module boundaries from CLAUDE.md, enforced by inspecting imports.

(The stricter rule for `domain` lives in test_state_machine.py.)
"""

import ast
from pathlib import Path

import pytest

from app.api.errors import STATUS_BY_CODE
from app.services.errors import ErrorCode

APP = Path(__file__).resolve().parents[2] / "app"

# package -> module prefixes it must not import
FORBIDDEN = {
    # HTTP only: go through services; never talk to Celery.
    "api": ("app.db", "app.storage", "app.queue", "celery", "kombu"),
    # No HTTP knowledge; Celery stays in `queue` (tasks call services, not the other way round).
    # (app.queue.broker, a plain Redis ping used by the health check, is allowed.)
    "services": (
        "fastapi",
        "starlette",
        "app.api",
        "celery",
        "kombu",
        "app.queue.celery_app",
        "app.queue.tasks",
        "app.queue.dispatcher",
        "app.queue.worker_process",
        "app.queue.worker_main",
    ),
    "storage": ("app.db", "app.services", "app.api", "sqlalchemy", "fastapi", "starlette"),
    "db": ("app.api", "app.services", "app.storage", "app.queue", "fastapi", "starlette", "celery"),
    # Celery setup, dispatcher, task entry points: no HTTP.
    "queue": ("fastapi", "starlette", "app.api"),
    # Stage logic gets a context (and later a provider and storage); no HTTP, Celery, or database.
    "workflows": (
        "fastapi",
        "starlette",
        "celery",
        "kombu",
        "sqlalchemy",
        "app.api",
        "app.db",
        "app.queue",
        "app.services",
    ),
}


def _imports(path: Path) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.append(node.module)
    return names


CASES = [(package, path) for package in FORBIDDEN for path in sorted((APP / package).rglob("*.py"))]


def test_every_package_has_files():
    for package in FORBIDDEN:
        assert list((APP / package).rglob("*.py")), package


@pytest.mark.parametrize(
    ("package", "path"), CASES, ids=[f"{p}/{f.relative_to(APP / p)}" for p, f in CASES]
)
def test_package_respects_its_boundary(package, path):
    bad = [name for name in _imports(path) if name.startswith(FORBIDDEN[package])]
    assert bad == []


def test_every_service_error_code_has_an_http_status():
    """A code missing from the table would silently become a 400."""
    assert set(ErrorCode) <= set(STATUS_BY_CODE)
