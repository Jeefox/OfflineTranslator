# -*- coding: utf-8 -*-
"""Общий сервис перевода (не зависит от конкретного движка inference).

TranslationService объединяет:
- словарь терминов (dictionary.json: snapshot читается один раз в
  начале каждого translate(); lookup приоритетнее нейросети);
- сентенс-пайплайн (sentence_pipeline: логические юниты, StreamUnit,
  сборка финального перевода);
- translate_stream — инкрементальный попредложенический перевод.

Модуль не импортирует torch/transformers и не зависит от библиотек
конкретного движка: весь inference делегируется объекту, реализующему
контракт TranslationBackend (backends/base.py). Сейчас это MarianBackend
(backends/marian.py); в будущем сюда можно подключить LlamaCppBackend
(GGUF) без изменений общего сервиса и GUI.

Token-aware chunking (уровень B: предложение → чанки, укладывающиеся в
лимит входных токенов) — ОБЩАЯ алгоритмическая часть, движко-независимая:
split_sentence_to_chunks получает счётчик токенов (count_tokens) и лимит
(limit) параметрами — каждый backend подаёт свои (Marian — HF-токенизатор
и config модели; будущий llama.cpp — свой токенизатор и размер контекста).
Сам алгоритм разбивки (естественные границы → жадная сборка → слова →
символы) существует в одной копии.

Этап 13: инкрементальный кэш перевода (translation_cache.TranslationCache)
— in-memory кэш переводов логических юнитов, ключ (идентичность модели,
направление, точный текст юнита): повторный перевод изменённого текста
отправляет в backend только изменённые/новые юниты; порядок вывода,
абзацы и события стрима от кэша не зависят.
"""
import dataclasses
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
import re
from threading import Event, RLock
from typing import Callable, Optional

from dictionary_manager import default_dictionary_path, load_snapshot
from sentence_pipeline import StreamUnit, assemble_output, split_units
from translation_cache import TranslationCache
from protected_tokens import mask_tokens, restore_tokens
from translation_errors import (TranslationError, ModelLoadError, ModelUnavailableError,
                                BackendError, ProtectedTokenError)


class TranslationCancelled(Exception):
    """Cooperative cancellation; no partial unit is cached or published."""


class CancellationToken:
    def __init__(self, is_cancelled=None):
        self._event = Event()
        self._is_cancelled = is_cancelled

    def cancel(self):
        self._event.set()

    def check(self):
        if self._event.is_set() or (self._is_cancelled and self._is_cancelled()):
            raise TranslationCancelled()


def _check_cancelled(token):
    if token is not None:
        token.check()





# ---------------------------------------------------------------------- #
#  Token-aware chunking (движко-независимый алгоритм, общий для бэкендов) #
# ---------------------------------------------------------------------- #
def split_sentence_to_chunks(sentence: str,
                             count_tokens: Callable[[str], int],
                             limit: int) -> list:
    """Разбивает предложение на куски, укладывающиеся в лимит токенов.

    Короткое предложение возвращается как есть (один кусок → один
    inference). Сборка идёт по границам слов с сохранением исходных
    срезов; сверхдлинные слова разбиваются безопасным fallback.

    Args:
        sentence: логическое предложение (текст юнита);
        count_tokens: счётчик входных токенов движка (текст -> int);
        limit: безопасный лимит входных токенов движка.
    """
    if limit < 1:
        raise ValueError("Token limit must be positive")
    if not sentence:
        return []
    if count_tokens(sentence) <= limit:
        return [sentence]
    # Keep original slices: no whitespace is invented or discarded by chunking.
    parts = re.findall(r"\S+\s*|\s+", sentence)
    chunks = []
    current = ""
    for part in parts:
        if current and count_tokens(current + part) > limit:
            chunks.append(current)
            current = ""
        if count_tokens(part) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_word(part, count_tokens, limit))
        else:
            current += part
    if current:
        chunks.append(current)
    return chunks


def _split_long_word(word: str, count_tokens: Callable[[str], int],
                     limit: int) -> list:
    """Разбивает слишком длинную строку без пробелов по символам
    (последняя линия обороны от потери текста)."""
    pieces = []
    start = 0
    while start < len(word):
        end = len(word)
        while end > start + 1 and count_tokens(word[start:end]) > limit:
            end = start + (end - start) // 2
        if count_tokens(word[start:end]) > limit:
            raise ValueError("A single character exceeds the token limit")
        pieces.append(word[start:end])
        start = end
    return pieces


# ---------------------------------------------------------------------- #
#  Общий сервис перевода                                                  #
# ---------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class TranslationChunk:
    text: str
    separator_before: str = ""
    separator_after: str = ""

    @classmethod
    def from_source(cls, source):
        start = len(source) - len(source.lstrip())
        end = len(source.rstrip())
        if end <= start:
            return cls("", source, "")
        return cls(source[start:end], source[:start], source[end:])


class TranslationService:
    """Общий сервис перевода поверх движко-независимого TranslationBackend.

    Публичный API (его сохраняет и facade translator.OfflineTranslator):
        translate(text, direction) -> str
        translate_stream(text, direction, on_sentence) -> str

    Args:
        backend: движок перевода, реализующий TranslationBackend
            (MarianBackend сейчас; LlamaCppBackend в будущем).
            Сервис вызывает backend.load() в __init__ и дальше только
            backend.split_sentence / backend.translate_chunk.
        dictionary_path: путь к dictionary.json (по умолчанию —
            default_dictionary_path());
        snapshot_loader: функция path -> snapshot (по умолчанию
            dictionary_manager.load_snapshot; инъектируется в тестах).
    """

    def __init__(self, backend, dictionary_path: Optional[str] = None,
                 snapshot_loader: Callable[[str], object] = load_snapshot,
                 auto_load: bool = True):
        # Движок: постоянного состояния в памяти нет — только ресурсы
        # inference (модели/токенизаторы), которые он загружает сам.
        self.backend = backend
        # Словарь: постоянного состояния в памяти нет. dictionary.json —
        # единственный источник истины (пишет DictionaryManager), актуальный
        # snapshot читается в начале каждого translate() (load_snapshot).
        self.dictionary_path = dictionary_path or default_dictionary_path()
        self._snapshot_loader = snapshot_loader
        self._dict_load_warned = False
        # Этап 13: инкрементальный кэш перевода (in-memory; ключ —
        # (идентичность модели, направление, текст юнита)): повторный
        # перевод изменённого текста не переводит неизменённые юниты.
        self.translation_cache = TranslationCache()

        self._load_lock = RLock()
        self._flight_lock = RLock()
        self._inference_lock = RLock()
        self._flights = {}
        self._loaded = False
        if auto_load:
            self.load()

    def load(self):
        with self._load_lock:
            if not self._loaded:
                try:
                    self.backend.load()
                except ModelLoadError:
                    raise
                except (FileNotFoundError, ImportError) as exc:
                    raise ModelUnavailableError(str(exc)) from exc
                except Exception as exc:
                    raise ModelLoadError(str(exc)) from exc
                self._loaded = True

    # ------------------------------------------------------------------ #
    #  Этап 13: идентичность модели для ключа кэша                        #
    # ------------------------------------------------------------------ #
    @property
    def model_identity(self):
        """Идентичность выбранной модели для ключа инкрементального кэша
        (Этап 13).

        Базовый сервис не знает, какая модель выбрана, — возвращает None.
        OfflineTranslator сужает её до выбранного model_id (реестр,
        Этап 9). Ключ (model_id, направление, текст юнита) гарантирует,
        что результат одной модели/направления не используется для другой.
        """
        return None

    # ------------------------------------------------------------------ #
    #  Разбиение текста (общее; от движка не зависит)                     #
    # ------------------------------------------------------------------ #
    def _split_text(self, text: str) -> list:
        """Разбивает текст на абзацы и предложения (список списков).

        Абзац — блок между пустыми строками; предложение заканчивается
        по . ! ? … и последующим пробелам/переводам строк.
        Этот исторический helper возвращает только содержимое юнитов.
        Публичный API собирает результат по исходным разделителям.

        Правила разбивки — sentence_pipeline.split_units: единый источник
        логических юнитов (те же юниты использует инкрементальный
        translate_stream для попредложенического перевода).
        """
        units = split_units(text)
        paragraphs = []
        current = []
        for u in units:
            if u.new_paragraph and current:
                paragraphs.append(current)
                current = []
            current.append(u.text)
        if current:
            paragraphs.append(current)
        return paragraphs

    def _translate_unit(self, sentence: str, direction: str, cancellation_token=None) -> str:
        key = self.translation_cache.make_key(self.model_identity, direction, sentence)
        while True:
            _check_cancelled(cancellation_token)
            with self._flight_lock:
                cached = self.translation_cache.get(key)
                if cached is not None:
                    return cached
                future = self._flights.get(key)
                owner = future is None
                if owner:
                    future = self._flights[key] = Future()
            if not owner:
                while not future.done():
                    _check_cancelled(cancellation_token)
                    try:
                        future.exception(timeout=0.05)
                    except FutureTimeoutError:
                        pass
                _check_cancelled(cancellation_token)
                try:
                    return future.result()
                except TranslationCancelled:
                    # A cancelled producer must not cancel an independent consumer.
                    _check_cancelled(cancellation_token)
                    continue
            acquired = False
            try:
                while not acquired:
                    _check_cancelled(cancellation_token)
                    acquired = self._inference_lock.acquire(timeout=0.05)
                result = self._translate_unit_once(sentence, direction, cancellation_token)
            except BaseException as exc:
                with self._flight_lock:
                    self._flights.pop(key, None)
                    future.set_exception(exc)
                raise
            else:
                with self._flight_lock:
                    self._flights.pop(key, None)
                    future.set_result(result)
                return result
            finally:
                if acquired:
                    self._inference_lock.release()

    def _translate_unit_once(self, sentence: str, direction: str, cancellation_token=None) -> str:
        """Перевод одного логического юнита (предложения) — единственная
        точка обращения к бэкенду для translate() и translate_stream()
        (Этап 13).

        Порядок: in-memory кэш (ключ: идентичность модели, направление,
        точный текст юнита) → backend по промаху (split_sentence →
        translate_chunk на каждый чанк), после чего результат
        сохраняется в кэше. Дубликаты юнитов разделяют одну запись
        (второе вхождение — даже в том же переводе — уже попадание).
        Словарь здесь не участвует: точный lookup всего текста
        выполняется в translate/translate_stream до разбивки на юниты
        (dictionary.json приоритетнее нейросети).
        """
        _check_cancelled(cancellation_token)
        key = self.translation_cache.make_key(
            self.model_identity, direction, sentence)
        cached = self.translation_cache.get(key)
        if cached is not None:
            return cached
        def infer(source):
            if not source.strip():
                return source
            try:
                sources = self.backend.split_sentence(source, direction)
                if "".join(sources) != source:
                    raise ValueError("Backend chunking must preserve exact source slices")
                chunks = [TranslationChunk.from_source(chunk) for chunk in sources]
                results = []
                for chunk in chunks:
                    _check_cancelled(cancellation_token)
                    translated = (self.backend.translate_chunk(chunk.text, direction).strip()
                                  if chunk.text else "")
                    _check_cancelled(cancellation_token)
                    results.append(chunk.separator_before + translated + chunk.separator_after)
                return "".join(results)
            except TranslationCancelled:
                raise
            except Exception as exc:
                raise BackendError(str(exc)) from exc

        masked, mapping = mask_tokens(sentence)
        translated = infer(masked)
        try:
            translation = restore_tokens(translated, mapping)
        except ValueError as exc:
            raise ProtectedTokenError(str(exc)) from exc
        _check_cancelled(cancellation_token)
        self.translation_cache.put(key, translation)
        return translation

    def _translate_paragraph(self, sentences: list, direction: str) -> str:
        """Переводит абзац: каждое предложение переводится через
        _translate_unit (Этап 13: сначала кэш, по промаху — backend),
        порядок сохраняется."""
        translated_sentences = []
        for sentence in sentences:
            translated_sentences.append(
                self._translate_unit(sentence, direction))
        return " ".join(translated_sentences)


    # ------------------------------------------------------------------ #
    #  Публичный API                                                      #
    # ------------------------------------------------------------------ #
    def translate(self, text: str, direction: str = "en-ru", *, cancellation_token=None) -> str:
        """Основная функция перевода.

        Args:
            text: Текст для перевода
            direction: Направление перевода ('en-ru' или 'ru-en')

        Returns:
            Переведённый текст

        Этап 13: перевод каждого юнита сначала берётся из in-memory
        кэша (ключ: идентичность модели, направление, текст юнита) —
        в backend уходят только отсутствующие юниты; склейка абзацев
        и разделители от кэша не зависят.
        """
        return self.translate_stream(text, direction, cancellation_token=cancellation_token)

    def translate_stream(self, text: str, direction: str = "en-ru",
                         on_sentence=None, *, cancellation_token=None) -> str:
        """Инкрементальный попредложенический перевод (тот же пайплайн,
        что и translate(), но с промежуточными результатами).

        Два уровня, как в translate():
        - уровень A: текст → логические предложения (split_units);
        - уровень B: каждое предложение → технические chunks,
          укладывающиеся в лимит входных токенов (backend.split_sentence
          поверх общего алгоритма split_sentence_to_chunks — источник
          истины; sentence[:512] и len() <= 512 здесь не используются).

        Чанки (chunks) одного логического предложения сначала переводятся все,
        затем склеиваются в ОДИН перевод предложения. Частичный/обрезанный
        перевод пользователю не показывается.

        on_sentence(phase, done, total, unit) вызывается в потоке вызывающего
        (worker-потоке GUI) для каждого предложения:
          phase "start" — началась обработка предложения (done — количество
              уже завершённых, unit.translation == "");
          phase "done"  — предложение переведено (unit.translation заполнен).
        Вызывающий обязан внутри колбэка только проверять актуальность
        операции и класть событие в очередь GUI (без Tk-операций).
        Исключение из колбэка прерывает перевод (ранний выход устаревшего
        worker'а).

        Этап 13: перевод юнита сначала берётся из in-memory кэша
        (ключ: идентичность модели, направление, текст юнита). Попадание
        — backend не вызывается, юнит выдаётся сразу; промах — обычный
        путь через backend. Последовательность start/done, порядок юнитов,
        разделители абзацев и финальный текст от кэша не зависят.

        Возвращает полный перевод — тот же текст, что и translate()
        для того же текста (источник истины для финального результата).
        """
        _check_cancelled(cancellation_token)
        if not text.strip():
            return ""
        self.load()
        _check_cancelled(cancellation_token)

        # Snapshot словаря читается один раз (как в translate()).
        snapshot = self._snapshot_loader(self.dictionary_path)
        if snapshot is None:
            if not self._dict_load_warned:
                print(f"⚠ Словарь не загружен ({self.dictionary_path}), "
                      f"используем только нейросетевой перевод")
                self._dict_load_warned = True
        else:
            self._dict_load_warned = False

        # Точное совпадение всего текста со словарём (как в translate()):
        # текст обрабатывается как единый логический юнит.
        if snapshot is not None:
            dict_result = snapshot.lookup(text)
            if dict_result is not None:
                start = len(text) - len(text.lstrip())
                end = len(text.rstrip())
                before, after = text[:start], text[end:]
                if on_sentence is not None:
                    unit = StreamUnit(src=text[start:end], src_start=start, src_end=end,
                                      new_paragraph=False, translation=dict_result,
                                      separator_before=before, separator_after=after)
                    on_sentence("start", 0, 1, unit)
                    _check_cancelled(cancellation_token)
                    on_sentence("done", 1, 1, unit)
                _check_cancelled(cancellation_token)
                return before + dict_result + after

        units = split_units(text)
        total = len(units)
        translations = []
        done = 0
        for u in units:
            _check_cancelled(cancellation_token)
            if on_sentence is not None:
                unit = StreamUnit(src=u.text, src_start=u.start,
                                  src_end=u.end, new_paragraph=u.new_paragraph,
                                  separator_before=u.separator_before,
                                  separator_after=u.separator_after)
                on_sentence("start", done, total, unit)
            # Одно логическое предложение → один перевод предложения
            # (Этап 13): сначала in-memory кэш — при попадании backend
            # НЕ вызывается и юнит выдаётся сразу; по промаху — обычный
            # путь через backend (N технических chunks → склейка),
            # результат сохраняется в кэше.
            translation = self._translate_unit(u.text, direction, cancellation_token)
            _check_cancelled(cancellation_token)
            done += 1
            if on_sentence is not None:
                on_sentence("done", done, total,
                            dataclasses.replace(unit,
                                                translation=translation))
            translations.append(translation)
        return assemble_output(units, translations)
