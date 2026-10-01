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
    customtkinter (включая темы), llama-cpp-python (libllama, опциональный
    GGUF-runtime) — если установлены.

Чего НЕТ в бандле (сознательно):
  - переводные модели. Marian (Helsinki-NLP opus-mt) скачивается из
    HuggingFace при первом запуске в постоянный user-кэш
    (~/.cache/OfflineTranslator/cache или %LOCALAPPDATA%\\OfflineTranslator\\cache)
    — в этот момент нужен интернет, далее работа полностью офлайн;
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
)
#: Опциональные зависимости: встраиваются, если установлены.
OPTIONAL_DEPS = (
    "llama_cpp",  # GGUF-бэкенд (llama-cpp-python)
)


def fail(message: str) -> "None":
    print(f"✗ ОШИБКА СБОРКИ: {message}", file=sys.stderr)
    sys.exit(1)


def check_dependencies() -> list[str]:
    """Проверяет наличие зависимостей в текущем окружении сборки."""
    missing = [
        name for name in REQUIRED_DEPS if importlib.util.find_spec(name) is None
    ]
    if missing:
        fail(
            "не установлены обязательные зависимости: " + ", ".join(missing)
            + "\n  Выполните: pip install -r requirements.txt pyinstaller"
        )
    present_optional = [
        name for name in OPTIONAL_DEPS if importlib.util.find_spec(name) is not None
    ]
    skipped_optional = [
        name for name in OPTIONAL_DEPS if name not in present_optional
    ]
    if skipped_optional:
        print(
            "ℹ Опциональные зависимости не установлены и не встраиваются: "
            + ", ".join(skipped_optional)
        )
    return present_optional


def clean() -> None:
    """Удаляет артефакты предыдущих сборок."""
    for path in (BUILD_DIR, DIST_DIR):
        if path.exists():
            print(f"Удаляю {path.name}/ ...")
            shutil.rmtree(path, ignore_errors=True)
    for spec in ROOT.glob("*.spec"):
        print(f"Удаляю {spec.name} ...")
        spec.unlink()


def build(optional_deps: list[str]) -> None:
    """Запускает PyInstaller (onedir, GUI) и проверяет результат."""
    is_windows = os.name == "nt"
    # Разделитель --add-data: ";" на Windows, ":" на POSIX.
    sep = ";" if is_windows else ":"
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
        # Данные/бинарники/сабмодули библиотек (нативные расширения, темы, .so).
        "--collect-all",
        "customtkinter",
        "--collect-all",
        "torch",
        "--collect-all",
        "transformers",
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

    print("Запускаю PyInstaller ...")
    print("  " + " ".join(args))
    subprocess.run(args, cwd=ROOT, check=True)

    # PyInstaller 6+ (onedir): dist/<name>/<binary> + dist/<name>/_internal/.
    binary_name = f"{APP_NAME}.exe" if is_windows else APP_NAME
    exe = DIST_DIR / APP_NAME / binary_name
    if not exe.exists():
        fail(f"ожидаемый исполняемый файл не найден: {exe}")

    total = 0
    for root, _dirs, files in os.walk(DIST_DIR / APP_NAME):
        for name in files:
            file = Path(root) / name
            if file.is_file():
                total += file.stat().st_size
    size_mb = total / (1024 * 1024)

    print()
    print("✓ СБОРКА УСПЕШНА!")
    print(f"  Бандль:      {DIST_DIR / APP_NAME}")
    print(f"  Исполняемый: {exe}")
    print(f"  Размер:      {size_mb:.0f} МБ")
    print("  Архивирование (см. .github/workflows/build.yml):")
    if is_windows:
        print(f"    Compress-Archive -Path {DIST_DIR / APP_NAME} "
              "-DestinationPath OfflineTranslator-Windows.zip")
    else:
        print(f"    tar -czf OfflineTranslator-*.tar.gz -C {DIST_DIR} {APP_NAME}")


def main() -> None:
    if not (ROOT / ENTRY).exists():
        fail(f"файл входа {ENTRY} не найден в корне проекта: {ROOT}")
    print("========================================")
    print("Сборка OfflineTranslator (PyInstaller onedir)")
    print("========================================")
    clean()
    optional = check_dependencies()
    build(optional)


if __name__ == "__main__":
    main()
