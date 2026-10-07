#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Кроссплатформенная сборка OfflineTranslator в автономный бандль (PyInstaller).

Запуск:
    python build.py

Результат:
    dist/OfflineTranslator/               (onedir-бандль)
    dist/OfflineTranslator/OfflineTranslator   (Linux/macOS)
    dist/OfflineTranslator/OfflineTranslator.exe (Windows)
    dist/OfflineTranslator/_internal/     (Python-зависимости и данные)

Предварительно (см. .github/workflows/build.yml):
    pip install torch --index-url https://download.pytorch.org/whl/cpu
    pip install -r requirements.txt
    pip install pyinstaller
  (torch ставим из CPU-индекса, чтобы в бандль не попали CUDA-библиотеки.)

Что попадает в бандль:
  - код приложения (main.py + модули + backends/);
  - dictionary.json — read-only копия в корне бандля (_MEIPASS/dictionary.json);
    при первом запуске приложение само копирует её в user-директорию
    (dictionary_manager.default_dictionary_path), существующий словарь
    не перезаписывается;
  - зависимости: torch (CPU), transformers, sentencepiece, sacremoses,
    customtkinter, pystray и Pillow; llama-cpp-python и plyer — если
    установлены.

Чего НЕТ в бандле (сознательно):
  - переводные модели. Обе базовые Marian-модели встраиваются в бандль из
    заранее заполненного локального HF-кэша;
  - GGUF-модель (Hy-MT2) не скачивается никогда: это локальный файл
    пользователя, путь задаётся переменной окружения OFFLINE_TRANSLATOR_GGUF.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dictionary_manager import model_cache_dir
from model_registry import _marian_hf_cache_has_model

ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
APP_NAME = "OfflineTranslator"
ENTRY = "main.py"

#: Обязательные runtime-зависимости (без них бандль не будет работать).
REQUIRED_DEPS = (
    "customtkinter",
    "transformers",
    "torch",
    "sentencepiece",
    "sacremoses",
    "pystray",
    "PIL",
)
#: Опциональные зависимости: встраиваются, если установлены.
OPTIONAL_DEPS = (
    "llama_cpp",  # GGUF-бэкенд (llama-cpp-python)
    "plyer",      # Windows fallback для системных уведомлений
)


def fail(message: str) -> "None":
    print(f"BUILD FAILED: {message}", file=sys.stderr)
    sys.exit(1)


def check_dependencies() -> list[str]:
    """Проверяет наличие зависимостей в текущем окружении сборки."""
    missing = [
        name for name in REQUIRED_DEPS if importlib.util.find_spec(name) is None
    ]
    if missing:
        fail(
            "missing required dependencies: " + ", ".join(missing)
            + "\n  Run: pip install -r requirements.txt pyinstaller"
        )
    present_optional = [
        name for name in OPTIONAL_DEPS if importlib.util.find_spec(name) is not None
    ]
    skipped_optional = [
        name for name in OPTIONAL_DEPS if name not in present_optional
    ]
    if skipped_optional:
        print(
            "NOTE: optional dependencies not installed, not bundled: "
            + ", ".join(skipped_optional)
        )
    return present_optional


def clean() -> None:
    """Удаляет артефакты предыдущих сборок."""
    for path in (BUILD_DIR, DIST_DIR):
        if path.exists():
            print(f"Removing {path.name}/ ...")
            shutil.rmtree(path, ignore_errors=True)
    for spec in ROOT.glob("*.spec"):
            print(f"Removing {spec.name} ...")
            spec.unlink()


def prepare_bundle_cache(cache_dir: Path) -> Path:
    """Собирает минимальный HF-кэш для встраивания в PyInstaller.

    Полный кэш Hugging Face содержит общие ``blobs``, несколько старых
    snapshot-ов и служебные lock-файлы. При копировании в PyInstaller это
    раздувает архив и на Windows может превысить лимит GitHub Release.
    Для запуска Marian достаточно одного snapshot каждой модели в формате
    ``models--.../snapshots/<revision>`` и ``refs/main``. Файлы snapshot-а
    копируются по содержимому, поэтому симлинки HF не требуют отдельного
    каталога blobs.
    """
    bundle_dir = BUILD_DIR / "bundle_cache"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir, ignore_errors=True)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    required_models = (
        "Helsinki-NLP/opus-mt-en-ru",
        "Helsinki-NLP/opus-mt-ru-en",
    )
    for repo_id in required_models:
        repo_name = "models--" + repo_id.replace("/", "--")
        source_repo = cache_dir / repo_name
        refs_main = source_repo / "refs" / "main"
        revision = refs_main.read_text(encoding="utf-8").strip() \
            if refs_main.is_file() else ""
        source_snapshot = source_repo / "snapshots" / revision
        if not revision or not source_snapshot.is_dir():
            snapshots = sorted(
                p for p in (source_repo / "snapshots").iterdir()
                if p.is_dir()) if (source_repo / "snapshots").is_dir() else []
            if not snapshots:
                fail(f"no snapshot found for release model: {repo_id}")
            source_snapshot = snapshots[-1]
            revision = source_snapshot.name

        target_repo = bundle_dir / repo_name
        target_snapshot = target_repo / "snapshots" / revision
        shutil.copytree(source_snapshot, target_snapshot,
                        symlinks=False, dirs_exist_ok=True)
        (target_repo / "refs").mkdir(parents=True, exist_ok=True)
        (target_repo / "refs" / "main").write_text(
            revision + "\n", encoding="utf-8")

    return bundle_dir


def build(optional_deps: list[str]) -> None:
    """Запускает PyInstaller (onedir, GUI) и проверяет результат."""
    is_windows = os.name == "nt"
    # Разделитель --add-data: ";" на Windows, ":" на POSIX.
    sep = ";" if is_windows else ":"
    cache_dir = Path(model_cache_dir())
    required_models = (
        "Helsinki-NLP/opus-mt-en-ru",
        "Helsinki-NLP/opus-mt-ru-en",
    )
    missing_models = [
        model for model in required_models
        if not _marian_hf_cache_has_model(str(cache_dir), model)
    ]
    if missing_models:
        fail(
            "offline bundle requires both Marian models in the local cache: "
            + ", ".join(missing_models)
            + "\n  Start the app once with internet access, then build again."
        )
    bundle_cache_dir = prepare_bundle_cache(cache_dir)
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onedir",      # каталог-бандль: без временного распаковывания, быстрый старт
        "--windowed",    # GUI-приложение — без консольного окна
        "--name",
        APP_NAME,
        # Словарь — read-only копия в корне бандля (_MEIPASS/dictionary.json).
        "--add-data",
        f"dictionary.json{sep}.",
        # Базовые Marian-модели обязательны для офлайн-первого запуска.
        "--add-data",
        f"{bundle_cache_dir}{sep}cache",
        # Данные/бинарники/сабмодули библиотек (нативные расширения, темы, .so).
        "--collect-all",
        "customtkinter",
        "--collect-all",
        "torch",
        "--collect-all",
        "transformers",
        "--collect-all",
        "pystray",
        "--hidden-import",
        "sentencepiece",
        "--hidden-import",
        "sacremoses",
        "--clean",
        "--noconfirm",
        "--log-level",
        "WARN",
    ]
    for dep in optional_deps:
        args += ["--collect-all", dep]
    args.append(ENTRY)

    print("Running PyInstaller ...")
    print("  " + " ".join(args))
    subprocess.run(args, cwd=ROOT, check=True)

    # PyInstaller 6+ (onedir): dist/<name>/<binary> + dist/<name>/_internal/.
    binary_name = f"{APP_NAME}.exe" if is_windows else APP_NAME
    exe = DIST_DIR / APP_NAME / binary_name
    if not exe.exists():
        fail(f"expected executable not found: {exe}")

    total = 0
    for root, _dirs, files in os.walk(DIST_DIR / APP_NAME):
        for name in files:
            file = Path(root) / name
            if file.is_file():
                total += file.stat().st_size
    size_mb = total / (1024 * 1024)

    print()
    print("BUILD OK!")
    print(f"  Bundle:      {DIST_DIR / APP_NAME}")
    print(f"  Executable:  {exe}")
    print(f"  Size:        {size_mb:.0f} MB")
    print("  Archiving (see .github/workflows/build.yml):")
    if is_windows:
        print(f"    Compress-Archive -Path {DIST_DIR / APP_NAME} "
              "-DestinationPath OfflineTranslator-Windows.zip")
    else:
        print(f"    tar -czf OfflineTranslator-*.tar.gz -C {DIST_DIR} {APP_NAME}")


def main() -> None:
    if not (ROOT / ENTRY).exists():
        fail(f"entry point {ENTRY} not found in project root: {ROOT}")
    print("========================================")
    print("Build OfflineTranslator (PyInstaller onedir)")
    print("========================================")
    clean()
    optional = check_dependencies()
    build(optional)


if __name__ == "__main__":
    main()
