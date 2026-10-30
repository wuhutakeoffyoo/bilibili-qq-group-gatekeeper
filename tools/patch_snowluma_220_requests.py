"""Produce a private, hash-guarded SnowLuma 2.2.0 runtime hotfix.

The input must be the original runtime from the pinned image documented in
docs/snowluma-request-incident-20261030.md. Never patch the forward UI mapper.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

ORIGINAL_SHA256 = "bcc5696aacf663384a765060df6201358ac6f987fd252a72f6bf526dcc231059"
PATCHED_SHA256 = "56c090d36876fd753e28b868571203f5058be5d09ace55a7882d31dac7328691"
OLD = (b"const notifyType = raw.eventType ?? 0;\n"
       b"\t\t\tconst operationType = groupRequestOperationType(notifyType);")
NEW = (b"const notifyType = [...GROUP_REQUEST_OPERATION_TYPE].find(([, operation]) => operation === (raw.eventType ?? 0))?.[0] ?? -1;\n"
       b"\t\t\tconst operationType = notifyType < 0 ? null : (raw.eventType ?? 0);")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path)
    parser.add_argument("output", type=Path, help="New private output file; existing files are never overwritten")
    args = parser.parse_args()
    data = args.original.read_bytes()
    if hashlib.sha256(data).hexdigest() != ORIGINAL_SHA256 or data.count(OLD) != 2:
        parser.error("unsupported runtime; expected the exact pinned SnowLuma 2.2.0 bundle")
    patched = data.replace(OLD, NEW)
    if hashlib.sha256(patched).hexdigest() != PATCHED_SHA256:
        parser.error("patched output does not match the verified runtime")
    with args.output.open("xb") as target:
        target.write(patched)
    print(f"sha256={PATCHED_SHA256} read_paths=2")


if __name__ == "__main__":
    main()
