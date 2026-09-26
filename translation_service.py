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
"""
import dataclasses
import re
from typing import Callable, Optional

from dictionary_manager import default_dictionary_path, load_snapshot
from sentence_pipeline import StreamUnit, assemble_output, split_units


# ---------------------------------------------------------------------- #
#  Token-aware chunking (движко-независимый алгоритм, общий для бэкендов) #
# ---------------------------------------------------------------------- #
def split_sentence_to_chunks(sentence: str,
                             count_tokens: Callable[[str], int],
                             limit: int) -> list:
    """Разбивает предложение на куски, укладывающиеся в лимит токенов.

    Короткое предложение возвращается как есть (один кусок → один
    inference). Сначала пробует естественные границы (, ; : — –),
    затем по словам; все части сохраняются, порядок не меняется.

    Args:
        sentence: логическое предложение (текст юнита);
        count_tokens: счётчик входных токенов движка (текст -> int);
        limit: безопасный лимит входных токенов движка.
    """
    if count_tokens(sentence) <= limit:
        return [sentence]
    # 1) естественные границы внутри слишком длинного «предложения»
    parts = [p for p in re.split(r'(?<=[,;:—–])\s*', sentence) if p.strip()]
    # 2) жадная сборка частей в куски по лимиту токенов
    chunks = []
    current = ""
    for part in parts:
        if not current:
            current = part
        elif count_tokens(current + " " + part) <= limit:
            current += " " + part
        else:
            chunks.append(current)
            current = part
    if current:
        chunks.append(current)
    # 3) кусок всё ещё длиннее лимита — разбиваем по словам
    result = []
    for chunk in chunks:
        if count_tokens(chunk) <= limit:
            result.append(chunk)
        else:
            result.extend(_split_by_words(chunk, count_tokens, limit))
    return result


def _split_by_words(text: str, count_tokens: Callable[[str], int],
                    limit: int) -> list:
    """Жадная сборка слов в куски по лимиту токенов (не режет слова)."""
    words = text.split()
    chunks = []
    current = []
    for word in words:
        if current and count_tokens(" ".join(current + [word])) > limit:
            chunks.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        chunks.append(" ".join(current))
    # 4) крайний случай: отдельное «слово» (URL, техническая строка)
    #    длиннее лимита — разбиваем по символам, чтобы ничего не потерять
    result = []
    for chunk in chunks:
        if count_tokens(chunk) <= limit:
            result.append(chunk)
        else:
            result.extend(_split_long_word(chunk, count_tokens, limit))
    return result


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
            end = start + 1
        pieces.append(word[start:end])
        start = end
    return pieces


# ---------------------------------------------------------------------- #
#  Общий сервис перевода                                                  #
# ---------------------------------------------------------------------- #
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
                 snapshot_loader: Callable[[str], object] = load_snapshot):
        # Движок: постоянного состояния в памяти нет — только ресурсы
        # inference (модели/токенизаторы), которые он загружает сам.
        self.backend = backend
        # Словарь: постоянного состояния в памяти нет. dictionary.json —
        # единственный источник истины (пишет DictionaryManager), актуальный
        # snapshot читается в начале каждого translate() (load_snapshot).
        self.dictionary_path = dictionary_path or default_dictionary_path()
        self._snapshot_loader = snapshot_loader
        self._dict_load_warned = False

        backend.load()

    # ------------------------------------------------------------------ #
    #  Разбиение текста (общее; от движка не зависит)                     #
    # ------------------------------------------------------------------ #
    def _split_text(self, text: str) -> list:
        """Разбивает текст на абзацы и предложения (список списков).

        Абзац — блок между пустыми строками; предложение заканчивается
        по . ! ? … и последующим пробелам/переводам строк.
        Порядок сохраняется; теряются только пробелы (восстанавливаются
        при сборке результата), слова и знаки не теряются.

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

    def _translate_paragraph(self, sentences: list, direction: str) -> str:
        """Переводит абзац: каждое предложение разбивается на куски,
        каждый кусок переводится отдельно, порядок сохраняется.
        Inference — у бэкенда (split_sentence/translate_chunk)."""
        translated_sentences = []
        for sentence in sentences:
            chunks = self.backend.split_sentence(sentence, direction)
            translated_sentences.append(
                " ".join(self.backend.translate_chunk(chunk, direction)
                         for chunk in chunks)
            )
        return " ".join(translated_sentences)


    # ------------------------------------------------------------------ #
    #  Публичный API                                                      #
    # ------------------------------------------------------------------ #
    def translate(self, text: str, direction: str = "en-ru") -> str:
        """Основная функция перевода.

        Args:
            text: Текст для перевода
            direction: Направление перевода ('en-ru' или 'ru-en')

        Returns:
            Переведённый текст
        """
        if not text.strip():
            return ""

        # Snapshot словаря читается ОДИН раз в начале перевода и используется
        # во время всего translate() (включая все куски): изменение словаря
        # в процессе текущего перевода действует только с следующего вызова.
        snapshot = self._snapshot_loader(self.dictionary_path)
        if snapshot is None:
            if not self._dict_load_warned:
                print(f"⚠ Словарь не загружен ({self.dictionary_path}), "
                      f"используем только нейросетевой перевод")
                self._dict_load_warned = True
        else:
            self._dict_load_warned = False

        # Проверяем словарь (двунаправленное точное совпадение)
        if snapshot is not None:
            dict_result = snapshot.lookup(text)
            if dict_result is not None:
                return dict_result

        # Используем движок (inference — у бэкенда)
        try:
            # Абзацы → предложения → куски, укладывающиеся в лимит входных
            # токенов; каждый кусок переводится отдельно, порядок сохраняется
            translated_paragraphs = [
                self._translate_paragraph(sentences, direction)
                for sentences in self._split_text(text)
            ]
            return "\n\n".join(translated_paragraphs)
        except Exception as e:
            return f"Ошибка перевода: {str(e)}"

    def translate_stream(self, text: str, direction: str = "en-ru",
                         on_sentence=None) -> str:
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

        Возвращает полный перевод — тот же текст, что и translate()
        для того же текста (источник истины для финального результата).
        """
        if not text.strip():
            return ""

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
                if on_sentence is not None:
                    unit = StreamUnit(src=text, src_start=0, src_end=len(text),
                                      new_paragraph=False,
                                      translation=dict_result)
                    on_sentence("start", 0, 1, unit)
                    on_sentence("done", 1, 1, unit)
                return dict_result

        units = split_units(text)
        total = len(units)
        translations = []
        done = 0
        for u in units:
            if on_sentence is not None:
                unit = StreamUnit(src=u.text, src_start=u.start,
                                  src_end=u.end, new_paragraph=u.new_paragraph)
                on_sentence("start", done, total, unit)
            # Одно логическое предложение → N технических chunks → один
            # перевод предложения (порядок chunks сохраняется;
            # inference — у бэкенда).
            chunks = self.backend.split_sentence(u.text, direction)
            translation = " ".join(
                self.backend.translate_chunk(chunk, direction) for chunk in chunks
            )
            done += 1
            if on_sentence is not None:
                on_sentence("done", done, total,
                            dataclasses.replace(unit,
                                                translation=translation))
            translations.append(translation)
        return assemble_output(units, translations)