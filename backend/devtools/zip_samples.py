"""Generate sample repository ZIPs in code (no binary files in the repository).

Used by the tests, and by the manual walkthrough:

    python -m devtools.zip_samples <output folder>

writes:
    repo.zip        a realistic small repository: sources, tests, README, plus files that are
                    skipped for every reason (node_modules, .git, a binary, a huge file, a
                    minified file, a symlink, a lockfile, ...)
    zipslip.zip     an entry named ../../evil.txt (the whole archive is rejected)
    symlink.zip     a symlink entry pointing at a secret file (skipped, never created)
    bomb.zip        2 MiB of zeros compressed about 1000:1 (rejected; small and safe on disk)
    encrypted.zip   an entry marked as password-protected (rejected)
    corrupt.zip     a truncated archive (rejected)
    nested.zip      a repository containing another ZIP (the inner one is skipped)
    empty_repo.zip  only files that are never indexed (fails: nothing to index)

Every archive is byte-for-byte deterministic (fixed timestamps, fixed order).
"""

import io
import stat
import struct
import sys
import warnings
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path

MIB = 1024 * 1024
_TIME = (2024, 1, 1, 0, 0, 0)

Entry = tuple[str, bytes] | tuple[str, bytes, int]  # (name, content[, unix mode])


def build_zip(entries: Iterable[Entry]) -> bytes:
    """A ZIP with the given entries, in order. A mode (e.g. stat.S_IFLNK | 0o777) is stored in
    the entry's Unix attributes, which is how symlinks and other special files are marked."""
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns about duplicate names; we want them
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry in entries:
                name, content = entry[0], entry[1]
                info = zipfile.ZipInfo(name, date_time=_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                mode = entry[2] if len(entry) == 3 else (stat.S_IFREG | 0o644)  # type: ignore[misc]
                info.create_system = 3  # Unix, so the mode bits are meaningful
                info.external_attr = mode << 16
                archive.writestr(info, content)
    return buffer.getvalue()


# --- Patching headers (to make archives that zipfile itself refuses to write) ---------------------


def _headers(data: bytes) -> list[tuple[int, int]]:
    """(central directory header offset, local header offset) of every entry."""
    end = data.rfind(b"PK\x05\x06")
    count = struct.unpack_from("<H", data, end + 10)[0]
    offset = struct.unpack_from("<I", data, end + 16)[0]
    headers = []
    for _ in range(count):
        assert data[offset : offset + 4] == b"PK\x01\x02"
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", data, offset + 28)
        headers.append((offset, struct.unpack_from("<I", data, offset + 42)[0]))
        offset += 46 + name_len + extra_len + comment_len
    return headers


def mark_encrypted(data: bytes) -> bytes:
    """Set the 'encrypted' flag on every entry (what every ZIP reader checks)."""
    patched = bytearray(data)
    for central, local in _headers(data):
        patched[central + 8] |= 0x01
        patched[local + 6] |= 0x01
    return bytes(patched)


def set_declared_size(data: bytes, index: int, size: int) -> bytes:
    """Make entry `index` claim a different uncompressed size than it really has."""
    patched = bytearray(data)
    central, local = _headers(data)[index]
    struct.pack_into("<I", patched, central + 24, size)
    struct.pack_into("<I", patched, local + 22, size)
    return bytes(patched)


# --- Building blocks ------------------------------------------------------------------------------

_PY_MODULE = '''"""Order processing for the sample shop."""

from dataclasses import dataclass


@dataclass
class Order:
    order_id: str
    total_cents: int


def apply_discount(order: Order, percent: int) -> Order:
    """Return a copy of the order with a percentage discount applied."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return Order(order.order_id, order.total_cents * (100 - percent) // 100)
'''

_PY_UTILS = '''"""Small helpers shared by the shop modules."""


def cents_to_text(cents: int) -> str:
    """Format an amount in cents as dollars, e.g. 1999 -> "$19.99"."""
    return f"${cents // 100}.{cents % 100:02d}"


def chunked(items: list, size: int) -> list[list]:
    """Split a list into consecutive pieces of at most `size` items."""
    return [items[i : i + size] for i in range(0, len(items), size)]
'''

_TEST = """from shop.orders import Order, apply_discount


def test_discount_reduces_the_total():
    assert apply_discount(Order("a1", 1000), 10).total_cents == 900
"""

_README = """# Sample shop

A tiny example repository used to test Tasklane's repo_index workflow. It processes orders and
applies discounts. Run the tests with pytest.
"""


def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256))


def repo_entries(*, huge_bytes: int = 6 * MIB) -> list[Entry]:
    """A realistic small repository, with at least one file skipped for every reason."""
    return [
        ("README.md", _README.encode()),
        ("LICENSE", b"MIT License\n\nPermission is hereby granted, free of charge.\n"),
        ("Makefile", b"test:\n\tpytest -q\n"),
        ("src/", b"", stat.S_IFDIR | 0o755),
        ("src/shop/__init__.py", b""),  # empty
        ("src/shop/orders.py", _PY_MODULE.encode()),
        ("src/shop/utils.py", _PY_UTILS.encode()),
        ("src/web/app.js", b"export function greet(name) {\n  return `Hello, ${name}!`;\n}\n"),
        ("src/web/app.min.js", b"function a(b){return b+1}"),  # minified (by name)
        ("src/web/widget.js", b"var w=" + b"1+" * 900 + b"1;"),  # minified (one huge line)
        ("tests/test_orders.py", _TEST.encode()),
        ("docs/guide.md", b"# Guide\n\nCreate an order, then apply a discount to it.\n"),
        ("docs/notes.txt", "Notes in UTF-16".encode("utf-16")),  # NUL bytes: binary
        ("data/huge.txt", b"lorem ipsum dolor sit amet\n" * (huge_bytes // 27 + 1)),
        ("assets/logo.png", _png()),  # unsupported type
        ("assets/fixtures.zip", build_zip([("inner.txt", b"never extracted")])),
        ("package-lock.json", b'{"lockfileVersion": 3}\n'),
        (".env", b"SECRET_TOKEN=do-not-index\n"),
        (".DS_Store", b"\x00\x00\x00\x01Bud1"),
        ("node_modules/left-pad/index.js", b"module.exports = (s) => s;\n"),
        (".git/HEAD", b"ref: refs/heads/main\n"),
        ("src/shop/current.py", b"orders.py", stat.S_IFLNK | 0o777),  # symlink
        ("src/shop/queue.py", b"", stat.S_IFIFO | 0o644),  # a named pipe: special file
        ("deep/" + "d/" * 20 + "file.py", b"x = 1\n"),  # 22 path components
        ("long/" + "n" * 240 + ".py", b"x = 1\n"),  # path longer than 240 characters
    ]


def repo_zip(*, huge_bytes: int = 6 * MIB) -> bytes:
    return build_zip(repo_entries(huge_bytes=huge_bytes))


def small_repo(extra: Iterable[Entry] = ()) -> bytes:
    """Two source files plus `extra` entries."""
    return build_zip(
        [("README.md", _README.encode()), ("src/shop/orders.py", _PY_MODULE.encode()), *extra]
    )


def zipslip_zip() -> bytes:
    return small_repo([("../../evil.txt", b"escaped!")])


def symlink_zip() -> bytes:
    return small_repo([("src/shop/settings.py", b"../../../secret.txt", stat.S_IFLNK | 0o777)])


def bomb_zip(size: int = 2 * MIB) -> bytes:
    """`size` zero bytes in one allowlisted file: compresses about 1000:1."""
    return small_repo([("data/zeros.txt", b"\x00" * size)])


def encrypted_zip() -> bytes:
    return mark_encrypted(small_repo())


def corrupt_zip() -> bytes:
    data = small_repo()
    return data[: len(data) // 2]


def nested_zip() -> bytes:
    inner = small_repo([("inner/payload.py", b"print('inside a nested archive')\n")])
    return small_repo([("vendor-archives/inner.zip", inner)])


def empty_repo_zip() -> bytes:
    return build_zip(
        [
            ("node_modules/left-pad/index.js", b"module.exports = (s) => s;\n"),
            (".git/HEAD", b"ref: refs/heads/main\n"),
            ("assets/logo.png", _png()),
            ("package-lock.json", b'{"lockfileVersion": 3}\n'),
        ]
    )


SAMPLES: dict[str, Callable[[], bytes]] = {
    "repo.zip": repo_zip,
    "zipslip.zip": zipslip_zip,
    "symlink.zip": symlink_zip,
    "bomb.zip": bomb_zip,
    "encrypted.zip": encrypted_zip,
    "corrupt.zip": corrupt_zip,
    "nested.zip": nested_zip,
    "empty_repo.zip": empty_repo_zip,
}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m devtools.zip_samples <output folder>", file=sys.stderr)
        return 2
    folder = Path(args[0])
    folder.mkdir(parents=True, exist_ok=True)
    for name, build in SAMPLES.items():
        (folder / name).write_bytes(build())
        print(f"wrote {folder / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
