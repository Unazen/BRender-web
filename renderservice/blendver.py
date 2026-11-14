import gzip
import re

GZIP_MAGIC = b"\x1f\x8b"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

LEGACY_HEADER = re.compile(rb"^BLENDER[-_][vV](\d{3})")
MODERN_HEADER = re.compile(rb"^BLENDER(\d{2})-(\d{2})v(\d{4})")


def read_head(path, size=32):
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic[:2] == GZIP_MAGIC:
        with gzip.open(path, "rb") as g:
            return g.read(size)
    if magic == ZSTD_MAGIC:
        try:
            import zstandard
        except ImportError as exc:
            raise RuntimeError("для .blend со сжатием zstd нужен пакет zstandard (pip install zstandard)") from exc
        with open(path, "rb") as raw:
            return zstandard.ZstdDecompressor().stream_reader(raw).read(size)
    with open(path, "rb") as f:
        return f.read(size)


def parse_header(head):
    match = MODERN_HEADER.match(head)
    if match:
        number = int(match.group(3))
        return number // 100, number % 100
    match = LEGACY_HEADER.match(head)
    if match:
        number = int(match.group(1))
        return number // 100, number % 100
    raise ValueError("не похоже на файл .blend")


def read_version(path):
    return parse_header(read_head(path))


def series_label(version):
    return f"{version[0]}.{version[1]}"
