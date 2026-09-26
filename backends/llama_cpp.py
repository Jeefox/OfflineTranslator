# -*- coding: utf-8 -*-
"""Движок перевода GGUF через llama.cpp (llama-cpp-python), CPU-POC.

POC-охват (Этап 6) — намеренно узкий:

- ОДНА фиксированная translation-oriented модель:
  tencent/Hy-MT2-1.8B (GGUF: tencent/Hy-MT2-1.8B-GGUF, напр.
  Hy-MT2-1.8B-Q4_K_M.gguf). Архитектура llama.cpp — «hunyuan-dense»,
  контекст модели 256K, 33 языка (включая en/ru).
- Путь к локальному GGUF-файлу передаётся ЯВНО (model_path) —
  автоматического скачивания модели НЕТ ни в бэкенде, ни в приложении.
- Только CPU (n_gpu_layers=0): никаких CUDA-зависимостей, POC сначала
  на CPU.
- Runtime: llama-cpp-python (in-process bindings llama.cpp) — см.
  отчёт Этапа 6 о выборе варианта интеграции.

llama_cpp импортируется ЛЕНИВО, внутри _default_llama_factory
(вызывается из load()): модуль backends.llama_cpp (и весь пакет
backends, и translator, и GUI) импортируются без установленной
llama-cpp-python. Отсутствие зависимости даёт понятную ошибку
RuntimeError из load(), а не ImportError при старте приложения.

Разделение ответственности (как для MarianBackend):

ЗДЕСЬ (model-specific inference):
- загрузка локальной GGUF-модели (llama_cpp.Llama, n_ctx);
- токенизация/счёт входных токенов (свой токенизатор GGUF);
- модельно-специфичный prompt (backends.prompts: chat template Hy-MT2
  + инструкция с ЯВНЫМ направлением) и sampling-параметры;
- inference одного чанка (create_completion) и стоп-токен EOS.

НЕ ЗДЕСЬ (общий сервис translation_service.TranslationService):
словарь (dictionary.json), разбивка текста на предложения, сборка
выхода, translate/translate_stream, обработка ошибок.
"""
import os
import sys
from typing import Callable, Optional

from backends.base import TranslationBackend
from backends import prompts
from translation_service import split_sentence_to_chunks

__all__ = ["LlamaCppBackend"]


def _default_llama_factory(model_path: str, n_ctx: int, verbose: bool):
    """Создаёт реальный llama_cpp.Llama (ленивый импорт: зависимость
    нужна только в момент load(), а не при импорте модуля)."""
    try:
        import llama_cpp
    except ImportError as e:
        raise RuntimeError(
            "llama-cpp-python не установлен: выполните "
            "`pip install llama-cpp-python` (зависимость нужна только "
            "для backend='llama_cpp'; Marian-путь без неё не меняется). "
            "Исходная ошибка: %s" % e
        ) from e
    return llama_cpp.Llama(
        model_path=model_path,
        n_ctx=n_ctx,
        n_gpu_layers=0,  # POC: только CPU (без CUDA-зависимостей)
        verbose=verbose,
    )


class LlamaCppBackend(TranslationBackend):
    """GGUF-движок (llama.cpp) для фиксированной POC-модели
    tencent/Hy-MT2-1.8B. Реализует контракт TranslationBackend
    (backends/base.py): load(), max_source_tokens, split_sentence(),
    translate_chunk(). Движок не знает о GUI, словаре и потоках —
    только inference."""

    name = "llama_cpp"

    #: Направления POC (те же, что у общего сервиса и Marian).
    DIRECTIONS = ("en-ru", "ru-en")

    #: POC-модель (справочно: какой GGUF ожидается в model_path).
    POC_MODEL = (
        "tencent/Hy-MT2-1.8B — GGUF: tencent/Hy-MT2-1.8B-GGUF "
        "(напр. Hy-MT2-1.8B-Q4_K_M.gguf, ~1.1 ГБ); "
        "архитектура hunyuan-dense, контекст модели 256K"
    )

    # --- Настройки POC (явные и задокументированные) ------------------- #
    #: Runtime-контекст (n_ctx), токенов. Модель поддерживает 256K, но
    #: для CPU-POC 4096 — безопасное и быстрое значение: prompt
    #: (instruction + спец-токены), чанк и генерация помещаются с запасом.
    DEFAULT_CONTEXT_LENGTH = 4096
    #: Потолок генерации перевода одного чанка (max_tokens), токенов.
    DEFAULT_MAX_OUTPUT_TOKENS = 1024
    #: Резерв контекста под prompt: спец-токены шаблона
    #: (<bos><hy_User>...<hy_Assistant>, ~4 токена) + фиксированная часть
    #: инструкции (~25-30 токенов). Реальный prompt на типичных текстах
    #: ~35-45 токенов; 128 — безопасный резерв (см. max_source_tokens).
    PROMPT_RESERVE_TOKENS = 128
    #: Дополнительный служебный запас (токены) для стабильного inference.
    SAFETY_MARGIN_TOKENS = 32

    def __init__(self, model_path,
                 context_length: int = DEFAULT_CONTEXT_LENGTH,
                 max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
                 max_source_tokens: Optional[int] = None,
                 llama_class: Optional[Callable] = None,
                 verbose: bool = False):
        """
        Args:
            model_path: путь к ЛОКАЛЬНОМУ GGUF-файлу (скачивание
                модели не производится). Не существует — FileNotFoundError.
            context_length: runtime-контекст n_ctx (POC-настройка,
                допустимо 512..131072).
            max_output_tokens: потолок вывода на чанк (>= 16).
            max_source_tokens: ЯВНЫЙ лимит исходного текста (целое,
                >= 1). Если None — вычисляется автоматически:
                    context_length - max_output_tokens
                    - PROMPT_RESERVE_TOKENS - SAFETY_MARGIN_TOKENS
                (в контексте, помимо исходного текста, живут prompt
                и генерация — весь контекст для source text НЕ
                предназначен; это и есть безопасный лимит POC).
            llama_class: тестовый шов — фабрика
                (model_path, n_ctx, verbose) -> объект с интерфейсом
                llama_cpp.Llama (create_completion/tokenize).
            verbose: логирование llama.cpp (по умолчанию выключено).
        """
        if not model_path:
            raise ValueError(
                "LlamaCppBackend: model_path обязателен (путь к локальному "
                "GGUF-файлу); модель не скачивается автоматически. "
                "POC-модель: %s" % self.POC_MODEL)
        path = os.path.expanduser(str(model_path))
        if not os.path.isfile(path):
            raise FileNotFoundError(
                "LlamaCppBackend: GGUF-файл не найден: %r. "
                "Модель не скачивается автоматически — поместите файл "
                "локально. POC-модель: %s" % (path, self.POC_MODEL))
        if not (isinstance(context_length, int)
                and 512 <= context_length <= 131072):
            raise ValueError(
                "LlamaCppBackend: context_length должен быть целым в "
                "512..131072 (получено %r)" % (context_length,))
        if not (isinstance(max_output_tokens, int) and max_output_tokens >= 16):
            raise ValueError(
                "LlamaCppBackend: max_output_tokens должен быть >= 16 "
                "(получено %r)" % (max_output_tokens,))
        if (max_source_tokens is not None and not (
                isinstance(max_source_tokens, int) and max_source_tokens >= 1)):
            raise ValueError(
                "LlamaCppBackend: max_source_tokens должен быть целым >= 1 "
                "(получено %r)" % (max_source_tokens,))

        self.model_path = path
        self.context_length = context_length
        self.max_output_tokens = max_output_tokens
        self.device = "cpu"  # POC: только CPU
        self.verbose = verbose
        self._llama_factory = llama_class or _default_llama_factory
        self._llm = None
        self._max_source_tokens = (
            max_source_tokens if max_source_tokens is not None
            else self._auto_max_source_tokens())

    def _auto_max_source_tokens(self) -> int:
        """Безопасный лимит исходного текста: контекст за вычетом
        места под генерацию, prompt и служебного запаса."""
        return max(16, self.context_length - self.max_output_tokens
                   - self.PROMPT_RESERVE_TOKENS - self.SAFETY_MARGIN_TOKENS)

    # ------------------------------------------------------------------ #
    #  TranslationBackend                                                 #
    # ------------------------------------------------------------------ #
    @property
    def max_source_tokens(self) -> int:
        """Безопасный лимит входных токенов исходного текста
        (явная POC-настройка или вычисленный, см. __init__)."""
        return self._max_source_tokens

    def load(self) -> None:
        """Загружает локальную GGUF-модель (CPU, n_ctx = context_length).

        Вызывается один раз TranslationService.__init__ до первого
        translate(). Повторный вызов игнорируется (модель уже в памяти).
        """
        if self._llm is not None:
            return
        print("Загрузка GGUF-модели (llama.cpp, CPU, n_ctx=%d): %s"
              % (self.context_length, self.model_path))
        sys.stdout.flush()
        self._llm = self._llama_factory(
            self.model_path, self.context_length, self.verbose)
        print("✓ GGUF-модель загружена (llama.cpp)")
        sys.stdout.flush()

    def split_sentence(self, sentence: str, direction: str) -> list:
        """Разбивает предложение на чанки, укладывающиеся в лимит
        исходного текста (общий алгоритм split_sentence_to_chunks;
        счётчик — токенизатор GGUF, лимит — max_source_tokens)."""
        self._validate_direction(direction)
        return split_sentence_to_chunks(
            sentence, self._count_tokens, self.max_source_tokens)

    def translate_chunk(self, chunk: str, direction: str) -> str:
        """Переводит один чанк: модельно-специфичный prompt (Hy-MT2)
        + inference llama.cpp. Ошибки inference пробрасываются
        вызывающему (общий сервис их обрабатывает по контракту)."""
        self._validate_direction(direction)
        llm = self._require_llm()
        # Prompt собирается явно (chat template из prompts.py) и
        # токенизуется с special=True: спец-токены Hy-MT2 — единичные
        # токены (как при apply_chat_template в transformers).
        prompt_text = prompts.build_raw_prompt(chunk, direction)
        prompt_ids = llm.tokenize(
            prompt_text.encode("utf-8"), add_bos=False, special=True)
        resp = llm.create_completion(
            prompt=prompt_ids,
            max_tokens=self.max_output_tokens,
            stop=[prompts.EOS],
            **prompts.SAMPLING,
        )
        text = resp["choices"][0]["text"].strip()
        if not text:
            raise RuntimeError(
                "LlamaCppBackend: пустой результат inference "
                "(chunk: %r, direction: %s)" % (chunk, direction))
        return text

    # ------------------------------------------------------------------ #
    #  Внутреннее                                                         #
    # ------------------------------------------------------------------ #
    def _require_llm(self):
        if self._llm is None:
            raise RuntimeError(
                "LlamaCppBackend: модель не загружена — сначала load() "
                "(TranslationService вызывает его в __init__)")
        return self._llm

    def _count_tokens(self, text: str) -> int:
        """Количество входных токенов для токенизатора GGUF
        (llama.cpp). special=False: обычный текст, без спец-токенов."""
        llm = self._require_llm()
        return len(llm.tokenize(text.encode("utf-8"), add_bos=False))

    def _validate_direction(self, direction: str) -> None:
        """Направление явное ('en-ru'/'ru-en'); неизвестное — ValueError."""
        if direction not in self.DIRECTIONS:
            raise ValueError(
                "LlamaCppBackend: неподдерживаемое направление %r "
                "(ожидается: %s)"
                % (direction, ", ".join(self.DIRECTIONS)))
