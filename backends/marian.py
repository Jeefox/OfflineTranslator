# -*- coding: utf-8 -*-
"""Движок перевода MarianMT (Helsinki-NLP/opus-mt) через Transformers.

Здесь живёт ВСЁ, что специфично для Marian/Transformers:
- загрузка токенизаторов и моделей для обоих направлений;
- выбор устройства CPU/CUDA;
- получение лимита входных токенов из config модели;
- inference (model.generate) для одного чанка;
- протокол счёта токенов HF-токенизатора (count_tokens).

Token-aware chunking как алгоритм (предложение → чанки, укладывающиеся
в лимит) — ОБЩАЯ движко-независимая часть:
translation_service.split_sentence_to_chunks. MarianBackend лишь подаёт
туда свой счётчик токенов (HF-токенизатор) и свой лимит
(config.max_position_embeddings минус запас).

Остальные уровни (словарь, пайплайн, GUI) не знают о Marian.
"""
import os
import shutil
import sys
from typing import Optional

import torch
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

from backends.base import TranslationBackend
from dictionary_manager import model_cache_dir
from local_models import local_path, has_transformers_model, cached_model_path, validate_transformers_model
from translation_service import split_sentence_to_chunks

__all__ = ["CacheManager", "MarianBackend", "count_tokens"]


class CacheManager:
    """Менеджер кэша моделей с поддержкой PyInstaller.

    Кэш — постоянная директория пользователя
    (dictionary_manager.model_cache_dir, единый источник истины,
    не зависит от CWD):
      Windows — %LOCALAPPDATA%\\OfflineTranslator\\cache,
      Linux/macOS — $XDG_CACHE_HOME/OfflineTranslator/cache (или ~/.cache/...).
    В режиме EXE: при старте восстанавливает кэш из бандля
    (_MEIPASS/cache — тот же layout, что использует build.bat), если его
    ещё нет или он пуст; дальше работает с обычным файлом на диске.
    Сам кэш не хранится в _MEIPASS: это временная директория PyInstaller,
    удаляемая после выхода (кэш должен переживать перезапуски).
    """

    def __init__(self, cache_dir: Optional[str] = None):
        # Если кэш не задан — постоянная пользовательская директория
        # (и в режиме разработки, и в EXE; не зависит от CWD — README).
        if cache_dir is None:
            cache_dir = model_cache_dir()

        self.cache_dir = cache_dir
        self.bundle_dir = None
        self.is_exe = getattr(sys, 'frozen', False)

        # Создаём папку кэша
        os.makedirs(self.cache_dir, exist_ok=True)

        print(f"✓ Кэш моделей: {self.cache_dir}")

        # Если EXE и есть бандль — восстанавливаем кэш
        if self.is_exe:
            self._restore_from_bundle()

    def _get_bundle_path(self) -> Optional[str]:
        """Путь к каталогу моделей, упакованному в EXE (или None).

        build.bat встраивает кэш моделей через
        `--add-data "cache;cache"`, т.е. в бандле модели лежат в
        `_MEIPASS/cache` (а не в "models" — ранняя несогласованность,
        из-за которой восстановление из бандля молча не работало).
        """
        if not self.is_exe:
            return None
        path = os.path.join(sys._MEIPASS, "cache")
        if os.path.isdir(path):
            return path
        return None

    def _restore_from_bundle(self):
        """Восстанавливает кэш из бандля, если его нет или он пуст."""
        bundle = self._get_bundle_path()
        if not bundle:
            return

        if os.path.isdir(self.cache_dir) and os.listdir(self.cache_dir):
            # Кэш уже есть — ничего не делаем
            return

        print("⏳ Восстанавливаю кэш моделей из приложения...")
        self._copytree(bundle, self.cache_dir)
        print("✓ Кэш восстановлен")

    def _copytree(self, src: str, dst: str):
        """Рекурсивное копирование каталога."""
        for root, _dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            target = os.path.join(dst, rel)
            os.makedirs(target, exist_ok=True)
            for f in files:
                s = os.path.join(root, f)
                d = os.path.join(target, f)
                shutil.copy2(s, d)


def count_tokens(tokenizer, text: str) -> int:
    """Реальное количество входных токенов для HuggingFace-токенизатора
    (а не символов)."""
    return len(tokenizer(text, add_special_tokens=True)["input_ids"])


class MarianBackend(TranslationBackend):
    """Движок MarianMT (Helsinki-NLP opus-mt) через Transformers/torch.

    Занимается только inference-спецификой Marian: загрузка моделей,
    выбор CPU/CUDA, лимит входных токенов, перевод одного чанка.
    Dictionary.json, пайплайн и GUI — вне этого класса.
    """

    name = "marian"

    #: Направление -> имя модели на HuggingFace Hub.
    DIRECTIONS = {
        "en-ru": "Helsinki-NLP/opus-mt-en-ru",
        "ru-en": "Helsinki-NLP/opus-mt-ru-en",
    }

    def __init__(self, cache_dir: Optional[str] = None, directions=None):
        self.directions = tuple(directions or self.DIRECTIONS)
        # Постоянный кэш моделей и отдельные локальные папки.
        self.cache_manager = CacheManager(cache_dir)
        self.device = "cpu"
        # direction -> (model, tokenizer); заполняется в load()
        self._models = {}
        # Безопасный лимит входных токенов (512 — лимит Marian, минус запас);
        # уточняется из config модели в load()
        self._max_source_tokens = 480

    # ------------------------------------------------------------------ #
    #  Загрузка                                                           #
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        """Загружает нейросети MarianMT для обоих направлений перевода."""
        print("Загрузка моделей перевода...")
        print("(Это займет 2-5 минут при первом запуске)")
        sys.stdout.flush()

        try:
            # Определяем устройство
            if torch.cuda.is_available():
                self.device = "cuda"
                print(f"✓ Используем GPU: {torch.cuda.get_device_name(0)}")
            else:
                print("✓ Используем CPU")

            for direction in self.directions:
                self._load_model_direction(direction, self.DIRECTIONS[direction])

            print("✓ Выбранные модели готовы к работе!")
            print("  Теперь можно работать офлайн")
            sys.stdout.flush()

        except Exception as e:
            print(f"✗ Ошибка загрузки моделей: {e}")
            raise

    def _load_model_direction(self, direction: str, model_name: str):
        """Загружает конкретную модель для направления перевода.

        Args:
            direction: Направление перевода ('en-ru' или 'ru-en')
            model_name: Имя модели в HuggingFace
        """
        print(f"  → Загружаю модель {direction}...")
        sys.stdout.flush()

        cache_dir = self.cache_manager.cache_dir
        source = local_path(direction) or cached_model_path(cache_dir, model_name)
        if source:
            validation = validate_transformers_model(source)
            if not validation.available:
                raise FileNotFoundError("Неполная локальная модель: " + validation.reason)
        source = source or model_name
        offline = (bool(source != model_name) or getattr(sys, "frozen", False)
                   or os.environ.get("OFFLINE_TRANSLATOR_OFFLINE", "").lower() in ("1", "true", "yes"))

        tokenizer = AutoTokenizer.from_pretrained(
            source,
            cache_dir=cache_dir,
            local_files_only=offline,
            trust_remote_code=False,
        )

        model = AutoModelForSeq2SeqLM.from_pretrained(
            source,
            cache_dir=cache_dir,
            local_files_only=offline,
            use_safetensors=True,
            trust_remote_code=False,
        )

        if self.device == "cuda":
            model = model.to(self.device)

        model.eval()  # Режим инференса
        # Реальный лимит входных токенов берём из config модели, а не хардкод
        self._max_source_tokens = self._max_source_tokens_from_config(model)

        self._models[direction] = (model, tokenizer)

        print(f"  ✓ Модель {direction} загружена")
        sys.stdout.flush()

    # ------------------------------------------------------------------ #
    #  TranslationBackend                                                 #
    # ------------------------------------------------------------------ #
    @property
    def max_source_tokens(self) -> int:
        """Безопасный лимит входных токенов (из config модели, минус запас)."""
        return self._max_source_tokens

    def _model_for(self, direction: str):
        """(model, tokenizer) для направления; неизвестное — ValueError."""
        try:
            return self._models[direction]
        except KeyError:
            raise ValueError(
                f"MarianBackend: неподдерживаемое направление {direction!r} "
                f"(ожидается: {', '.join(self.DIRECTIONS)})"
            ) from None

    def split_sentence(self, sentence: str, direction: str) -> list:
        """Разбивает предложение на чанки, укладывающиеся в лимит входных
        токенов (общий алгоритм split_sentence_to_chunks; счётчик и лимит —
        Marian-специфичные: HF-токенизатор направления и config модели)."""
        _model, tokenizer = self._model_for(direction)
        return split_sentence_to_chunks(
            sentence,
            lambda text: count_tokens(tokenizer, text),
            self.max_source_tokens,
        )

    def translate_chunk(self, chunk: str, direction: str) -> str:
        """Переводит один чанк, заранее укладывающийся в лимит токенов.

        truncation=True — только страховка (при корректных чанках не
        срабатывает), а не механизм длинного текста.
        """
        model, tokenizer = self._model_for(direction)
        inputs = tokenizer(
            chunk,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=self.max_source_tokens
        ).to(self.device)

        # Генерация перевода
        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_length=512,
                num_beams=4,
                early_stopping=True
            )

        # Декодирование
        return tokenizer.decode(outputs[0], skip_special_tokens=True)

    # ------------------------------------------------------------------ #
    #  Внутреннее                                                         #
    # ------------------------------------------------------------------ #
    def _max_source_tokens_from_config(self, model) -> int:
        """Безопасный лимит входных токенов: реальный лимит модели
        (config.max_position_embeddings, для Marian — 512) минус небольшой
        запас для стабильного inference."""
        cfg = getattr(model, "config", None)
        max_pos = getattr(cfg, "max_position_embeddings", None)
        if not (isinstance(max_pos, int) and max_pos > 64):
            max_pos = 512
        return max_pos - 32
