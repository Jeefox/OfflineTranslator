"""Локальные источники моделей без импортов inference-библиотек."""
import os
from contextvars import ContextVar
from contextlib import contextmanager

_request_paths = ContextVar("model_request_paths", default=None)


@contextmanager
def using_paths(paths):
    token = _request_paths.set(paths)
    try:
        yield
    finally:
        _request_paths.reset(token)

from pathlib import Path
import sys

_paths = {}
ENV_KEYS = {"en-ru": "OFFLINE_TRANSLATOR_MARIAN_EN_RU",
            "ru-en": "OFFLINE_TRANSLATOR_MARIAN_RU_EN",
            "gguf": "OFFLINE_TRANSLATOR_GGUF"}


def configure_paths(en_ru="", ru_en="", gguf=""):
    global _paths
    _paths = {"en-ru": en_ru, "ru-en": ru_en, "gguf": gguf}


def model_directory():
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent
    return base / "models"


def configured_path(direction):
    return bool(_paths.get(direction))


def local_path(direction):
    captured = _request_paths.get()
    if captured is not None:
        return captured.get(direction)
    explicit = _paths.get(direction) or os.environ.get(ENV_KEYS[direction], "")
    if explicit:
        return str(Path(explicit).expanduser().resolve())
    if direction != "gguf":
        path = model_directory() / ("marian-" + direction)
        if path.is_dir():
            return str(path)
    return None


def has_transformers_model(path):
    folder = Path(path)
    return (folder.is_dir() and (folder / "config.json").is_file()
            and any((folder / name).is_file() for name in
                    ("model.safetensors", "pytorch_model.bin",
                     "model.safetensors.index.json", "pytorch_model.bin.index.json")))
