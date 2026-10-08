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


from dataclasses import dataclass
import json
from pathlib import PurePosixPath


@dataclass(frozen=True)
class ModelValidationResult:
    available: bool
    complete: bool
    reason: str
    files: tuple[str, ...] = ()


def validate_transformers_model(path):
    """Validate Marian layout without importing inference libraries or loading weights."""
    files = []
    def invalid(reason):
        return ModelValidationResult(False, False, reason, tuple(files))
    try:
        folder = Path(path)
        if not folder.is_dir():
            return invalid("Папка модели не найдена")
        def read_json(name):
            file = folder / name
            if not file.is_file() or not file.stat().st_size:
                raise ValueError("Не найден или пуст файл " + name)
            data = json.loads(file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Ожидается JSON-объект в " + name)
            files.append(name)
            return data
        config = read_json("config.json")
        if config.get("model_type") != "marian":
            return invalid("Поддерживаются только модели Marian")
        tokenizer_config = read_json("tokenizer_config.json")
        read_json("vocab.json")
        if tokenizer_config.get("separate_vocabs"):
            read_json("target_vocab.json")
        for name in ("source.spm", "target.spm"):
            if not (folder / name).is_file() or not (folder / name).stat().st_size:
                return invalid("Не найден или пуст файл " + name)
            files.append(name)
        if (folder / "model.safetensors").is_file():
            weights = ["model.safetensors"]
        else:
            index = read_json("model.safetensors.index.json")
            weight_map = index.get("weight_map")
            if not isinstance(weight_map, dict) or not weight_map:
                return invalid("Индекс safetensors не содержит weight_map")
            if any(not isinstance(key, str) or not isinstance(value, str) for key, value in weight_map.items()):
                return invalid("Некорректные записи weight_map")
            weights = sorted(set(weight_map.values()))
        for name in weights:
            relative = PurePosixPath(name)
            if (relative.is_absolute() or ".." in relative.parts or "\\" in name
                    or not name.endswith(".safetensors")):
                return invalid("Недопустимое имя файла весов в индексе")
            file = folder / name
            if not file.is_file() or not file.stat().st_size:
                return invalid("Не найден или пуст файл весов " + name)
            files.append(name)
        return ModelValidationResult(True, True, "", tuple(files))
    except (OSError, ValueError, TypeError) as exc:
        return invalid(str(exc))


def has_transformers_model(path):
    return validate_transformers_model(path).available


def cached_model_path(cache_dir, repo_id):
    """Select a complete local snapshot; registry and backend use the same path."""
    root = Path(cache_dir) / ("models--" + repo_id.replace("/", "--"))
    snapshots = root / "snapshots"
    reference = root / "refs" / "main"
    try:
        candidates = []
        if reference.is_file():
            revision = reference.read_text(encoding="utf-8").strip()
            if revision not in (".", "..") and revision and all(c.isalnum() or c in "._-" for c in revision):
                candidates.append(snapshots / revision)
        if snapshots.is_dir():
            candidates.extend(sorted(snapshots.iterdir(), reverse=True))
        for candidate in candidates:
            if has_transformers_model(candidate):
                return str(candidate)
    except OSError:
        return None
    return None
