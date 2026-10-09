#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Кроссплатформенная сборка OfflineTranslator в автономный бандль (PyInstaller).

Запуск:
    python build.py

Результат:
    dist/OfflineTranslator                (Linux/macOS, один файл)
    dist/OfflineTranslator.exe            (Windows, один файл)

Предварительно (см. .github/workflows/build.yml):
    pip install "torch==2.10.0" --index-url https://download.pytorch.org/whl/cpu
    pip install -r requirements-release.txt
  (Linux/Windows: CPU-индекс исключает CUDA-библиотеки; на macOS
   достаточно pip install -r requirements-release.txt.)

Что попадает в бандль:
  - код приложения (main.py + модули + backends/);
  - dictionary.json — read-only копия в корне бандля (_MEIPASS/dictionary.json);
    при первом запуске приложение само копирует её в user-директорию
    (dictionary_manager.default_dictionary_path), существующий словарь
    не перезаписывается;
  - зависимости: torch (CPU), transformers, sentencepiece, sacremoses,
    customtkinter, pystray и Pillow; plyer — если установлен.
    llama-cpp-python — только при OFFLINE_TRANSLATOR_BUILD_GGUF=1.

Чего НЕТ в бандле:
  - переводные модели: поставляются отдельным архивом с папкой models;
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
            + "\n  Run: pip install -r requirements-release.txt"
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


def prepare_icon() -> Path:
    """Копирует проверенный многоразмерный ICO в каталог сборки."""
    source = ROOT / "assets" / "offline_translator.ico"
    if not source.is_file():
        fail("icon is missing: run python -m scripts.generate_icons")
    path = BUILD_DIR / "offline_translator.ico"
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, path)
    return path


def build(optional_deps: list[str]) -> None:
    """Запускает PyInstaller (onefile, GUI) и проверяет результат."""
    is_windows = os.name == "nt"
    # Разделитель --add-data: ";" на Windows, ":" на POSIX.
    sep = ";" if is_windows else ":"
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",     # пользователь получает один запускаемый файл
        "--windowed",    # GUI-приложение — без консольного окна
        "--name",
        APP_NAME,
        # Словарь — read-only копия в корне бандля (_MEIPASS/dictionary.json).
        "--add-data",
        f"dictionary.json{sep}.",
        "--add-data",
        f"assets{sep}assets",
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
    if is_windows:
        args += ["--icon", str(prepare_icon())]
    include_gguf = os.environ.get("OFFLINE_TRANSLATOR_BUILD_GGUF") == "1"
    if include_gguf:
        if importlib.util.find_spec("llama_cpp") is None:
            fail("GGUF build requires requirements-gguf.txt")
        args += ["--collect-all", "llama_cpp"]
    else:
        args += ["--exclude-module", "llama_cpp"]
    for dep in optional_deps:
        if dep != "llama_cpp":
            args += ["--collect-all", dep]
    args.append(ENTRY)

    print("Running PyInstaller ...")
    print("  " + " ".join(args))
    subprocess.run(args, cwd=ROOT, check=True)

    # PyInstaller onefile: dist/<name>[.exe].
    binary_name = f"{APP_NAME}.exe" if is_windows else APP_NAME
    exe = DIST_DIR / binary_name
    if not exe.exists():
        fail(f"expected executable not found: {exe}")
    total = 0
    for root, _dirs, files in os.walk(DIST_DIR):
        for name in files:
            file = Path(root) / name
            if file.is_file():
                total += file.stat().st_size
    size_mb = total / (1024 * 1024)

    print()
    print("BUILD OK!")
    print(f"  Bundle:      {exe}")
    print(f"  Executable:  {exe}")
    print(f"  Size:        {size_mb:.0f} MB")
    print("  Archiving (see .github/workflows/build.yml):")
    if is_windows:
        print(f"    Compress-Archive -Path {exe} "
              "-DestinationPath OfflineTranslator-Windows.zip")
    else:
        print(f"    tar -czf OfflineTranslator-*.tar.gz -C {DIST_DIR} {binary_name}")


def main() -> None:
    if not (ROOT / ENTRY).exists():
        fail(f"entry point {ENTRY} not found in project root: {ROOT}")
    print("========================================")
    print("Build OfflineTranslator (PyInstaller onefile)")
    print("========================================")
    clean()
    optional = check_dependencies()
    build(optional)


if __name__ == "__main__":
    main()
