"""Командный интерфейс оффлайн-переводчика EN -> RU."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from offline_translate.translator import OfflineTranslator


def _read_input() -> str:
    data = sys.stdin.read()
    return data if data.strip() else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="offline-translate",
        description="Оффлайн-переводчик English -> Russian (Argo Translate).",
    )
    parser.add_argument(
        "text",
        nargs="*",
        help="Текст для перевода (сложение аргументов через пробел). "
        "Если не задан — читается из stdin.",
    )
    parser.add_argument(
        "-t", "--text-file",
        type=Path,
        help="Прочитать переводимый текст из файла.",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Установить модель en->ru из локального .argosmodel и выйти.",
    )
    parser.add_argument(
        "--term",
        metavar="EN=RU",
        action="append",
        default=[],
        help="Добавить термин в глоссарий: --term 'machine learning'=машинное обучение.",
    )
    parser.add_argument(
        "--time", action="store_true", help="Показать время перевода.",
    )
    args = parser.parse_args(argv)

    translator = OfflineTranslator()
    if args.install:
        translator.install_model()
        print("Модель en->ru установлена из локального .argosmodel. Работаем оффлайн.")
        return 0

    for pair in args.term:
        if "=" not in pair:
            print(f"Ошибка: --term требует формат EN=RU: {pair!r}", file=sys.stderr)
            return 2
        src, dst = pair.split("=", 1)
        translator.add_term(src.strip(), dst.strip())

    if args.text_file:
        text = args.text_file.read_text(encoding="utf-8").strip()
    elif args.text:
        text = " ".join(args.text)
    else:
        text = _read_input().strip()
    if not text:
        parser.print_help()
        return 2

    started = time.perf_counter()
    if len(text) <= 300 and "\n\n" not in text and text.count(". ") < 2:
        result = translator.translate(text).text
    else:
        result = translator.translate_text(text)
    elapsed = time.perf_counter() - started

    print(result)
    if args.time:
        print(f"\n[переведено за {elapsed:.2f} с]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
