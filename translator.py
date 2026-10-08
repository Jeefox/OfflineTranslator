# -*- coding: utf-8 -*-
"""OfflineTranslator — публичная точка входа проекта (compat-фасад).

Архитектура после выделения абстракции бэкендов:

    OfflineTranslator (этот модуль)
              |
              +-- TranslationService (translation_service.py) — общая логика,
              |     не зависящая от движка: словарь (dictionary.json),
              |     сентенс-пайплайн, token-aware chunking (общий алгоритм),
              |     сборка результата, translate_stream; без torch/transformers;
              |
              +-- ModelManager (model_registry.py) — Этап 9: выбор КОНКРЕТНОЙ
              |     модели по model_id: дескриптор + локальная доступность
              |     (без загрузки моделей, без сети); создание бэкенда
              |     остаётся в этом фасаде;
              |
              +-- MarianBackend (backends/marian.py) — Marian/Transformers-
                    специфика: загрузка моделей, CPU/CUDA, выбор модели EN↔RU,
                    лимит входных токенов, inference (model.generate);
              +-- LlamaCppBackend (backends/llama_cpp.py) — GGUF-специфика
                    (CPU-POC, Этап 6): tencent/Hy-MT2-1.8B через
                    llama-cpp-python, model-specific prompt в backends/prompts.py.

Выбор бэкенда/модели:
- legacy (Этап 6): OfflineTranslator(backend=..., gguf_path=...);
  по умолчанию 'marian' (поведение до Этапа 6);
- новый (Этап 9): OfflineTranslator(model_id=...) — конкретная модель по
  id реестра (model_registry): 'marian-en-ru', 'marian-ru-en',
  'hy-mt2-1.8b'. model_id и backend одновременно НЕ задаются.
Новый бэкенд добавляется в backends/ без касаний общего сервиса и GUI:
main.py создаёт OfflineTranslator() без параметров и не знает, какой
движок выполняется.
"""
import logging
import os
from local_models import local_path
from typing import Optional

# Логи: технические детали (model_id, направление) — сюда, а не в текст
# исключения: пользователь видит только понятное сообщение (Этап 14).
logger = logging.getLogger("offline_translate.translator")

# load_snapshot намеренно — модульное имя этого модуля:
# регрессионный контракт (tests/test_dictionary.py) monkeypatch'ит
# translator.load_snapshot, а сервис резолвит его через globals()
# в момент вызова (см. _load_snapshot()).
from dictionary_manager import default_dictionary_path, load_snapshot  # noqa: F401
from model_registry import (
    GGUF_ENV_VAR,
    ModelManager,
    ModelUnavailableError,
)
from translation_service import (
    TranslationService,
    split_sentence_to_chunks,
)
# MarianBackend/CacheManager/count_tokens реэкспортируются для совместимости API:
# до рефакторинга они жили в этом модуле (особенно импорт CacheManager —
# legacy-контракт).
from backends.marian import CacheManager, MarianBackend, count_tokens  # noqa: F401
# LlamaCppBackend реэкспортируется для совместимости API. Модуль импортируется
# безопасно без установленной llama-cpp-python (ленивый импорт внутри load()).
from backends.llama_cpp import LlamaCppBackend  # noqa: F401


def _load_snapshot(path: str):
    """Косвенный вызов глобального имени модуля `load_snapshot`:
    сервис вызывает его в момент translate, поэтому monkeypatch
    translator.load_snapshot работает (существующий контракт тестов)."""
    return globals()["load_snapshot"](path)


class OfflineTranslator(TranslationService):
    """Класс офлайн-переводчика с поддержкой двунаправленного перевода EN↔RU.

    Тонкий фасад над TranslationService + выбранным бэкендом. Публичный API
    сохранён (используется main.py и тестами):
        translate(text, direction) -> str
        translate_stream(text, direction, on_sentence) -> str
        max_source_tokens, device, dictionary_path, cache_manager
    Исторические приватные методы (_split_text, _split_sentence_to_chunks,
    _token_count) сохранены как регрессионный контракт тестов и реализованы
    поверх общих функций.

    Выбор модели:
    - model_id (Этап 9) — новый способ выбрать КОНКРЕТную модель по id
      реестра (model_registry.default_registry()):
          "marian-en-ru" — Marian EN→RU (Helsinki-NLP/opus-mt-en-ru);
          "marian-ru-en" — Marian RU→EN (Helsinki-NLP/opus-mt-ru-en);
          "hy-mt2-1.8b"  — GGUF tencent/Hy-MT2-1.8B (оба направления;
                           путь — gguf_path либо переменная окружения
                           OFFLINE_TRANSLATOR_GGUF — механизм Этапа 6).
      Выбор разрешается через ModelManager: дескриптор + локальная
      доступность (без загрузки моделей, без сети). Неизвестный id —
      ModelNotFoundError (со списком известных id); модель отсутствует
      локально — ModelUnavailableError (СКАЧИВАНИЯ НЕ ПРОИЗВОДИТСЯ).
      Выбранная модель ограничивает направления: translate()/
      translate_stream() в неподдерживаемом направлении — явная ошибка
      (молчаливого перевода не в том направлении нет).
    - backend (legacy, Этап 6) — низкоуровневый выбор бэкенда без
      конкретной модели: 'marian' (default — поведение до Этапа 6) или
      'llama_cpp' (нужен gguf_path). model_id и backend одновременно
      задавать нельзя (ValueError).

    GUI (main.py) создаёт OfflineTranslator() без параметров и НЕ знает,
    какая именно модель/движок выполняется — веток вида
    `if backend == ...` в main.py нет.
    """

    def __init__(self, cache_dir: Optional[str] = None,
                 backend: Optional[str] = None,
                 gguf_path: Optional[str] = None,
                 model_id: Optional[str] = None, auto_load: bool = True):
        if model_id is not None:
            # --- Этап 9: выбор конкретной модели через реестр ---
            if backend is not None:
                raise ValueError(
                    "OfflineTranslator: model_id=%r и backend=%r нельзя "
                    "задавать вместе: model_id — новый способ выбрать "
                    "конкретную модель (бэкенд определяется по её "
                    "дескриптору), backend=... — legacy-способ выбрать "
                    "бэкенд. Укажите только одно из двух."
                    % (model_id, backend))
            backend_obj, directions = self._resolve_model(
                model_id, gguf_path, cache_dir)
        else:
            # --- Legacy (Этап 6): поведение без изменений ---
            if backend is None:
                backend = "marian"
            if backend == "marian":
                # Marian-движок: кэш моделей (не зависит от CWD; EXE —
                # из бандля), CPU/CUDA, модели обоих направлений,
                # лимит входных токенов.
                backend_obj = MarianBackend(cache_dir=cache_dir)
                directions = None  # без ограничений — legacy-поведение
            elif backend == "llama_cpp":
                # GGUF-движок (CPU-POC): локальный GGUF-файл, путь явный;
                # POC-настройки по умолчанию (n_ctx и т.д. — в LlamaCppBackend).
                if not gguf_path:
                    raise ValueError(
                        "OfflineTranslator(backend='llama_cpp'): нужен "
                        "gguf_path (путь к локальному GGUF-файлу "
                        "tencent/Hy-MT2-1.8B)")
                backend_obj = LlamaCppBackend(gguf_path)
                directions = None
            else:
                raise ValueError(
                    "OfflineTranslator: неизвестный backend %r "
                    "(доступны: 'marian', 'llama_cpp')" % (backend,))

        # TranslationService.__init__ загружает backend, если auto_load=True.
        super().__init__(backend=backend_obj, snapshot_loader=_load_snapshot, auto_load=auto_load)
        # Совместимость с прежним API: обычные атрибуты (как до рефакторинга).
        # Источник истины — backend.max_source_tokens / backend.device
        # (заполнены после load() внутри super().__init__).
        self.max_source_tokens = self.backend.max_source_tokens
        self.device = self.backend.device
        # Менеджер кэша — тот же объект, что и внутри бэкенда;
        # у бэкендов без кэша (llama_cpp) — None.
        self.cache_manager = getattr(self.backend, "cache_manager", None)
        # Этап 9: выбранная модель (None — legacy-выбор backend=...).
        self.model_id = model_id
        sources = []
        paths = ([getattr(self.backend, "model_path")] if hasattr(self.backend, "model_path")
                 else [local_path(direction) for direction in directions or ("en-ru", "ru-en")])
        from pathlib import Path
        for path in paths:
            if path:
                root = Path(path)
                files = [root] if root.is_file() else sorted(p for p in root.glob("*") if p.is_file())
                sources.extend((str(p.resolve()), p.stat().st_size, p.stat().st_mtime_ns)
                               for p in files)
        self._model_identity = (model_id, tuple(sources)) if sources else model_id
        # Направления выбранной модели (descriptor.directions) или None —
        # без ограничений (legacy: направление валидирует сам бэкенд).
        self._model_directions = directions

    # ------------------------------------------------------------------ #
    #  Этап 13: идентичность модели для ключа инкрементального кэша       #
    # ------------------------------------------------------------------ #
    @property
    def model_identity(self):
        """Идентичность выбранной модели для ключа кэша перевода (Этап 13):
        model_id из реестра (None — legacy-выбор backend=...). Включается
        в ключ кэша вместе с направлением и текстом юнита, поэтому
        результат одной модели не используется для другой."""
        return self._model_identity

    # ------------------------------------------------------------------ #
    #  Этап 9: разрешение model_id (ModelManager; без загрузки моделей)   #
    # ------------------------------------------------------------------ #
    def _resolve_model(self, model_id: str, gguf_path: Optional[str],
                       cache_dir: Optional[str]):
        """model_id -> (backend_obj, directions).

        Выбор разрешается через ModelManager (model_registry): дескриптор
        + локальная доступность (лёгкие stdlib-проверки, без загрузки
        моделей и без сети). Создание объекта бэкенда остаётся здесь,
        в фасаде: ModelManager не является factory.

        Ошибки:
          - неизвестный model_id — ModelNotFoundError (реестр; список
            известных id);
          - для GGUF отсутствующий локальный файл — ModelUnavailableError;
            Marian допускается без предварительного кэша: он загружается
            из встроенного бандля, пользовательского кэша или HuggingFace;
          - gguf_path для не-GGUF-модели — ValueError.
        """
        manager = ModelManager(cache_dir=cache_dir)
        descriptor = manager.get_model(model_id)
        if gguf_path is not None and descriptor.backend != "llama_cpp":
            raise ValueError(
                "OfflineTranslator(model_id=%r): gguf_path имеет смысл "
                "только для GGUF-моделей (backend='llama_cpp'), у этой "
                "модели backend=%r."
                % (model_id, descriptor.backend))
        if descriptor.backend == "marian":
            # Не блокируем Marian из-за пустого кэша: backend должен иметь
            # возможность скачать модель при первом запуске, если интернет
            # доступен. В офлайн-бандле CacheManager предварительно
            # восстановит вложенный кэш из _MEIPASS/cache.
            return MarianBackend(cache_dir=cache_dir, directions=descriptor.directions), descriptor.directions
        if descriptor.backend == "llama_cpp":
            if gguf_path is not None:
                # Явный путь — существующий контракт LlamaCppBackend:
                # файл должен существовать (имя модели не проверяется).
                path = os.path.expanduser(str(gguf_path))
                if not os.path.isfile(path):
                    raise ModelUnavailableError(
                        "Модель %r (%s) сейчас недоступна локально: "
                        "GGUF-файл не найден: %r. Модель не скачивается "
                        "автоматически."
                        % (model_id, descriptor.name, gguf_path))
            else:
                # Существующий механизм Этапа 6: путь — из env
                # OFFLINE_TRANSLATOR_GGUF (реестр проверяет файл и имя
                # модели в имени файла).
                if not manager.is_model_available(model_id):
                    raise ModelUnavailableError(
                        "Модель %r (%s) сейчас недоступна локально: файл "
                        "GGUF не найден. Передайте gguf_path=... (путь к "
                        ".gguf-файлу) или задайте переменную окружения "
                        "%s. Модель не скачивается автоматически."
                        % (model_id, descriptor.name, GGUF_ENV_VAR))
                path = local_path("gguf")
            return LlamaCppBackend(path), descriptor.directions
        raise ValueError(
            "OfflineTranslator(model_id=%r): неизвестный backend %r в "
            "дескрипторе модели."
            % (model_id, descriptor.backend))

    # ------------------------------------------------------------------ #
    #  Направление: проверка для модели, выбранной через model_id         #
    # ------------------------------------------------------------------ #
    def _check_direction(self, direction: str) -> None:
        """Модель, выбранная через model_id, поддерживает только свои
        направления (descriptor.directions): неподдерживаемое направление —
        явная ValueError (молчаливого перевода не в том направлении нет).
        Legacy-выбор (_model_directions is None) — без ограничений:
        направление валидирует сам бэкенд, как раньше.

        Текст исключения — понятный пользователю (попадает в статус GUI,
        Этап 14): без внутренних идентификаторов. Технические детали
        (model_id, направление) — в лог.
        """
        allowed = self._model_directions
        if allowed is not None and direction not in allowed:
            logger.warning(
                "Модель %r не поддерживает направление %r "
                "(модель поддерживает: %s)",
                self.model_id, direction, ", ".join(allowed))
            raise ValueError(
                "Модель не поддерживает выбранное направление. "
                "Выберите подходящую модель в «Настройках».")

    def load(self):
        super().load()
        self.max_source_tokens = self.backend.max_source_tokens
        self.device = self.backend.device

    def translate(self, text: str, direction: str = "en-ru") -> str:
        """Перевод (контракт TranslationService.translate сохранён:
        возвращает str, ошибки — исключения). Перед вызовом
        сервиса проверяется направление выбранной модели (Этап 9)."""
        self._check_direction(direction)
        return super().translate(text, direction)

    def translate_stream(self, text: str, direction: str = "en-ru",
                         on_sentence=None) -> str:
        """Инкрементальный перевод (контракт TranslationService.
        translate_stream сохранён: исключения пробрасываются). Направление
        выбранной модели проверяется до начала обработки (Этап 9)."""
        self._check_direction(direction)
        return super().translate_stream(text, direction, on_sentence)

    # ------------------------------------------------------------------ #
    #  Исторические приватные методы (контракт регрессионных тестов)      #
    # ------------------------------------------------------------------ #
    def _token_count(self, tokenizer, text: str) -> int:
        """Реальное количество входных токенов (а не символов)
        для HuggingFace-токенизатора (протокол Marian)."""
        return count_tokens(tokenizer, text)

    def _split_sentence_to_chunks(self, sentence: str, tokenizer) -> list:
        """Разбивает предложение на куски, укладывающиеся в лимит токенов
        (прежний API OfflineTranslator; поверх общего алгоритма
        translation_service.split_sentence_to_chunks)."""
        return split_sentence_to_chunks(
            sentence,
            lambda text: self._token_count(tokenizer, text),
            self.max_source_tokens,
        )
