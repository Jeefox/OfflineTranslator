#!/usr/bin/env python3
"""Prepare safe weights from pinned official Helsinki releases (build step only).

The application never loads pickle weights. This fixed-source converter uses
patched PyTorch and weights_only=True; it does not accept user model paths.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from huggingface_hub import snapshot_download
from dictionary_manager import model_cache_dir
from local_models import has_transformers_model

MODEL_REVISIONS = {
    "Helsinki-NLP/opus-mt-en-ru": "bb09c99d180016eac6819df3dae68edb1690fdee",
    "Helsinki-NLP/opus-mt-ru-en": "fbd6dc73284f95536648512cc21d57f19191961a",
}
MODELS = tuple(MODEL_REVISIONS)


def prepare_release_model(repo_id, cache_dir):
    if repo_id not in MODEL_REVISIONS:
        raise ValueError("Only pinned official Helsinki models can be converted")
    import torch
    from packaging.version import Version
    if Version(torch.__version__.split("+")[0]) < Version("2.10.0"):
        raise RuntimeError("Conversion requires PyTorch >= 2.10.0 (GHSA-63cw-57p8-fm3p)")
    from transformers import AutoModelForSeq2SeqLM
    revision = MODEL_REVISIONS[repo_id]
    root = Path(cache_dir) / ("models--" + repo_id.replace("/", "--"))
    safe_revision = hashlib.sha1((revision + ":offline-translator-safetensors-v1").encode()).hexdigest()
    target = root / "snapshots" / safe_revision
    if not has_transformers_model(target):
        source = Path(snapshot_download(repo_id=repo_id, revision=revision, cache_dir=str(cache_dir),
                                       allow_patterns=["*.json", "*.spm", "pytorch_model.bin",
                                                       "README.md", "LICENSE*", "*.txt"]))
        config = json.loads((source / "config.json").read_text(encoding="utf-8"))
        if config.get("model_type") != "marian":
            raise ValueError("Pinned release must have Marian architecture")
        model = AutoModelForSeq2SeqLM.from_pretrained(
            str(source), local_files_only=True, trust_remote_code=False,
            use_safetensors=False, weights_only=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent, prefix="safe-model-") as folder:
            staging = Path(folder) / "model"
            staging.mkdir()
            for file in source.iterdir():
                if file.is_file() and (file.suffix.lower() in (".json", ".spm", ".md", ".txt")
                                       or file.name.upper().startswith("LICENSE")):
                    shutil.copy2(file, staging / file.name)
            model.save_pretrained(staging, safe_serialization=True)
            (staging / "conversion.json").write_text(json.dumps({
                "source": repo_id, "revision": revision,
                "torch": torch.__version__, "format": "safetensors"}, indent=2), encoding="utf-8")
            if not has_transformers_model(staging):
                raise ValueError("Conversion did not produce safetensors weights")
            # A failed existing snapshot is retained until conversion succeeds.
            if target.exists():
                raise ValueError("Incomplete prepared snapshot; remove it explicitly before retry: " + str(target))
            os.replace(staging, target)
        del model
    (root / "refs").mkdir(parents=True, exist_ok=True)
    reference = root / "refs" / "main"
    temporary = reference.with_suffix(".tmp")
    temporary.write_text(safe_revision, encoding="utf-8")
    os.replace(temporary, reference)
    return target


def main():
    cache_dir = Path(model_cache_dir())
    for repo_id in MODELS:
        print(prepare_release_model(repo_id, cache_dir))


if __name__ == "__main__":
    main()
