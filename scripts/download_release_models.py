#!/usr/bin/env python3
"""Скачивает обязательные Marian-модели в HF-кэш сборочной машины."""
from __future__ import annotations

from pathlib import Path

from huggingface_hub import snapshot_download

from dictionary_manager import model_cache_dir


MODELS = (
    "Helsinki-NLP/opus-mt-en-ru",
    "Helsinki-NLP/opus-mt-ru-en",
)


def main() -> None:
    cache_dir = Path(model_cache_dir())
    print(f"Downloading release models to {cache_dir}")
    for repo_id in MODELS:
        print(f"  -> {repo_id}")
        snapshot_download(repo_id=repo_id, cache_dir=str(cache_dir),
                          allow_patterns=["*.json", "*.spm", "*.safetensors",
                                          "pytorch_model*.bin", "README.md", "LICENSE*", "*.txt"])
    print("All release models are ready")


if __name__ == "__main__":
    main()
