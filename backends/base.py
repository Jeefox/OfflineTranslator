# -*- coding: utf-8 -*-
"""Абстракция движка перевода (TranslationBackend).

Backend отвечает за inference конкретного движка
(Marian/Transformers сейчас, llama.cpp/GGUF в будущем):
загрузку моделей, выбор модели по направлению, лимит входных токенов,
разбиение предложения на inference-чанки и перевод одного чанка.

Backend НЕ занимается Tkinter, очередями GUI, debounce, словарём
(dictionary.json) и подсветкой предложений — это уровень общего сервиса
(translation_service.TranslationService) и GUI (main.py).

Интерфейс намеренно минимальный: общему сервису достаточно
(1) разбить логическое предложение на куски, укладывающиеся в лимит
входных токенов движка, и (2) перевести один такой кусок.
"""
from abc import ABC, abstractmethod


class TranslationBackend(ABC):
    """Один конкретный движок перевода (Marian, llama.cpp, ...)."""

    #: Имя движка (для логирования/диагностики).
    name = "base"

    def load(self) -> None:
        """Загружает ресурсы движка (модели, токенизаторы).

        Вызывается один раз TranslationService.__init__ до первого
        translate(). Движкам, которым ничего загружать не нужно,
        достаточно реализацией по умолчанию.
        """

    @property
    @abstractmethod
    def max_source_tokens(self) -> int:
        """Безопасный лимит входных токенов (лимит движка минус запас).

        Чанк, передаваемый translate_chunk(), гарантированно не длиннее
        этого лимита в токенах движка (см. split_sentence).
        """

    @abstractmethod
    def split_sentence(self, sentence: str, direction: str) -> list:
        """Разбивает логическое предложение на технические чанки,
        укладывающиеся в лимит входных токенов.

        direction — 'en-ru' или 'ru-en' (направление выбирает модель).
        Чанки сохраняют порядок, текст не теряется; короткое предложение
        возвращается как список из одного элемента.
        """

    @abstractmethod
    def translate_chunk(self, chunk: str, direction: str) -> str:
        """Переводит один чанк, заранее укладывающийся в лимит токенов.

        Ошибки inference пробрасываются вызывающему: общий сервис
        обрабатывает их (translate() оборачивает в
        «Ошибка перевода: ...», translate_stream() передаёт дальше —
        в GUI-потоке main.py это перехватывается как translation_error).
        """