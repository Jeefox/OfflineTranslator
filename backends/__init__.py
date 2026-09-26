# -*- coding: utf-8 -*-
"""Движки перевода (backends).

Backend отвечает за inference конкретного движка: загрузку моделей,
выбор модели по направлению, лимит входных токенов, перевод чанка.
Общий сервис (translation_service.TranslationService) не зависит от
конкретного бэкенда.

Сейчас:
- MarianBackend (MarianMT/Transformers) — базовый движок (default);
- LlamaCppBackend (llama.cpp/GGUF, CPU-POC) — тencent/Hy-MT2-1.8B.

LlamaCppBackend импортируется без установленной llama-cpp-python
(ленивый импорт внутри load()), поэтому пакет backends остаётся
импортируемым и без этой опциональной зависимости.
"""
from backends.base import TranslationBackend
from backends.llama_cpp import LlamaCppBackend
from backends.marian import MarianBackend

__all__ = ["TranslationBackend", "MarianBackend", "LlamaCppBackend"]
