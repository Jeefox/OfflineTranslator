#!/usr/bin/env python3
"""Делит большой release-архив на части меньше лимита GitHub."""
from __future__ import annotations

import argparse
from pathlib import Path


# GitHub ограничивает один ReleaseAsset значением 2 GiB.
DEFAULT_CHUNK_SIZE = 1_800_000_000
READ_SIZE = 64 * 1024 * 1024


def split_archive(source: Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> list[Path]:
    if not source.is_file():
        raise FileNotFoundError(source)
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if source.stat().st_size <= chunk_size:
        return [source]

    parts: list[Path] = []
    with source.open("rb") as stream:
        index = 1
        while True:
            part = source.with_name(f"{source.name}.{index:03d}")
            written = 0
            with part.open("wb") as output:
                while written < chunk_size:
                    block = stream.read(min(READ_SIZE, chunk_size - written))
                    if not block:
                        break
                    output.write(block)
                    written += len(block)
            if written == 0:
                part.unlink()
                break
            parts.append(part)
            index += 1

    source.unlink()
    return parts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    args = parser.parse_args()
    parts = split_archive(args.archive, args.chunk_size)
    for part in parts:
        print(f"Created {part} ({part.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
