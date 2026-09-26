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
              +-- MarianBackend (backends/marian.py) — Marian/Transformers-
                    специфика: загрузка моделей, CPU/CUDA, выбор модели EN↔RU,
                    лимит входных токенов, inference (model.generate);
              +-- LlamaCppBackend (backends/llama_cpp.py) — GGUF-специфика
                    (CPU-POC, Этап 6): tencent/Hy-MT2-1.8B через
                    llama-cpp-python, model-specific prompt в backends/prompts.py.

Выбор бэкенда — параметр OfflineTranslator(backend=..., gguf_path=...);
по умолчанию 'marian' (поведение до Этапа 6). Новый бэкенд добавляется
в backends/ без касаний общего сервиса и GUI: main.py создаёт
OfflineTranslator() без параметров и не знает, какой движок выполняется.
"""
from typing import Optional

# load_snapshot намеренно — модульное имя этого модуля:
# регрессионный контракт (tests/test_dictionary.py) monkeypatch'ит
# translator.load_snapshot, а сервис резолвит его через globals()
# в момент вызова (см. _load_snapshot()).
from dictionary_manager import default_dictionary_path, load_snapshot  # noqa: F401
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

    Выбор бэкенда (Этап 6, POC): параметр backend — 'marian' (default,
    как до Этапа 6) или 'llama_cpp' (GGUF tencent/Hy-MT2-1.8B; путь к
    локальному GGUF-файлу — gguf_path, скачивания модели нет). GUI
    (main.py) создаёт OfflineTranslator() без параметров и НЕ знает,
    какой именно движок выполняется — веток вида `if backend == ...`
    в main.py нет.
    """

    def __init__(self, cache_dir: Optional[str] = None,
                 backend: str = "marian",
                 gguf_path: Optional[str] = None):
        if backend == "marian":
            # Marian-движок: кэш моделей (не зависит от CWD; EXE — из бандля),
            # CPU/CUDA, модели обоих направлений, лимит входных токенов.
            backend_obj = MarianBackend(cache_dir=cache_dir)
        elif backend == "llama_cpp":
            # GGUF-движок (CPU-POC): локальный GGUF-файл, путь явный;
            # POC-настройки по умолчанию (n_ctx и т.д. — в LlamaCppBackend).
            if not gguf_path:
                raise ValueError(
                    "OfflineTranslator(backend='llama_cpp'): нужен gguf_path "
                    "(путь к локальному GGUF-файлу tencent/Hy-MT2-1.8B)")
            backend_obj = LlamaCppBackend(gguf_path)
        else:
            raise ValueError(
                "OfflineTranslator: неизвестный backend %r "
                "(доступны: 'marian', 'llama_cpp')" % (backend,))

        # TranslationService.__init__ вызывает backend.load().
        super().__init__(backend=backend_obj, snapshot_loader=_load_snapshot)
        # Совместимость с прежним API: обычные атрибуты (как до рефакторинга).
        # Источник истины — backend.max_source_tokens / backend.device
        # (заполнены после load() внутри super().__init__).
        self.max_source_tokens = self.backend.max_source_tokens
        self.device = self.backend.device
        # Менеджер кэша — тот же объект, что и внутри бэкенда;
        # у бэкендов без кэша (llama_cpp) — None.
        self.cache_manager = getattr(self.backend, "cache_manager", None)

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
