# -*- coding: utf-8 -*-
"""Реестр моделей: backend-нейтральный слой «какие модели есть и какая выбрана».

Фундамент Этапа 8. Три уровня (лёгкие данные: без загрузки моделей,
без сети, без GUI, без inference):

    ModelDescriptor — описание модели: стабильный id, отображаемое имя,
        тип бэкенда, поддерживаемые направления, происхождение.
        Immutable-объект лёгких данных: получение дескриптора НЕ
        загружает модель в память.
    ModelRegistry — набор моделей: get(), list(), find_by_backend(),
        find_by_direction(). Уникальные id (дубликат — ValueError),
        неизвестный id — ModelNotFoundError (KeyError), детерминированный
        порядок: порядок регистрации == приоритет.
    ModelManager — «какую модель использовать и доступна ли она сейчас»:
        list_models(), get_model(), is_model_available(),
        get_available_models(), get_default_model(direction), resolve().
        Не занимается inference, скачиванием моделей, Tkinter,
        benchmark и словарём.

Зарегистрированные модели (default_registry()) отражают ФАКТИЧЕСКУЮ
архитектуру проекта:

- marian-en-ru, marian-ru-en — Marian (Helsinki-NLP opus-mt) — ДВЕ
  независимые модели, по одной на направление (так MarianBackend их
  реально загружает), поэтому — два direction-specific дескриптора
  одного семейства;
- hy-mt2-1.8b — GGUF tencent/Hy-MT2-1.8B (оба направления), локальный
  файл; путь — переменная окружения OFFLINE_TRANSLATOR_GGUF
  (существующий механизм Этапа 6). Скачивания модели нет.

Lazy loading: модуль НЕ импортирует torch/transformers/llama_cpp и даже
пакет backends — доступность проверяется лёгкими filesystem-проверками
(Marian — структура HF-кэша; GGUF — файл из env).

Подключение к пайплайну (Этап 9): OfflineTranslator(model_id=...) —
выбор разрешается через ModelManager (дескриптор + доступность, без
загрузки моделей); создание объекта бэкенда остаётся в фасаде
(translator.py), т.е. ModelManager не является factory. Поведение
OfflineTranslator() по умолчанию не меняется (Marian); GUI использует
реестр в отдельном (последующем) этапе; download-функций нет.
"""
import os
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple

from dictionary_manager import model_cache_dir

__all__ = [
    "ModelDescriptor",
    "ModelRegistry",
    "ModelManager",
    "ModelNotFoundError",
    "ModelUnavailableError",
    "SUPPORTED_DIRECTIONS",
    "GGUF_ENV_VAR",
    "default_registry",
]

#: Направления, которые проект поддерживает сейчас.
SUPPORTED_DIRECTIONS = ("en-ru", "ru-en")

#: Переменная окружения с путём к локальному GGUF-файлу (существующий
#: механизм Этапа 6; её же используют benchmark и smoke-тест).
GGUF_ENV_VAR = "OFFLINE_TRANSLATOR_GGUF"


class ModelNotFoundError(KeyError):
    """Неизвестный model id (ModelRegistry/ModelManager)."""


class ModelUnavailableError(RuntimeError):
    """Модель известна, но сейчас недоступна локально."""


def _validate_direction(direction: str) -> None:
    """Направление должно быть из поддерживаемого набора."""
    if direction not in SUPPORTED_DIRECTIONS:
        raise ValueError(
            "неподдерживаемое направление %r (допустимы: %s)"
            % (direction, ", ".join(SUPPORTED_DIRECTIONS)))

@dataclass(frozen=True)
class ModelDescriptor:
    """Лёгкое описание модели (описывает модель, но НЕ загружает её).

    Поля:
        id — стабильный уникальный идентификатор (например,
          "marian-en-ru");
        name — отображаемое имя (для будущего GUI);
        backend — тип/id бэкенда: "marian" или "llama_cpp" (те же
          идентификаторы, что у OfflineTranslator(backend=...));
        directions — кортеж поддерживаемых направлений ("en-ru", "ru-en");
        source — происхождение/расположение модели (HuggingFace-
          репозиторий или локальный файл). Machine-specific абсолютные
          пути запрещены;
        hf_model_id — для Marian: id репозитория на HuggingFace Hub
          (используется для проверки локального HF-кэша);
        gguf_model — для llama_cpp: ожидаемое имя модели в имени
          GGUF-файла (валидация файла из env).

    Frozen-объект данных: без изменяемого состояния и без ссылок на
    runtime-объекты (torch-модель, токенизатор, llama_cpp.Llama,
    TranslationService, GUI).
    """
    id: str
    name: str
    backend: str
    directions: Tuple[str, ...]
    source: str
    hf_model_id: Optional[str] = None
    gguf_model: Optional[str] = None

    def __post_init__(self):
        for field in ("id", "name", "backend", "source"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    "ModelDescriptor.%s: требуется непустая строка" % field)
        if (not isinstance(self.directions, tuple)
                or not self.directions):
            raise ValueError(
                "ModelDescriptor.directions: требуется непустой кортеж "
                "направлений")
        seen = set()
        for direction in self.directions:
            if direction not in SUPPORTED_DIRECTIONS:
                raise ValueError(
                    "ModelDescriptor.directions: неподдерживаемое "
                    "направление %r (допустимы: %s)"
                    % (direction, ", ".join(SUPPORTED_DIRECTIONS)))
            if direction in seen:
                raise ValueError(
                    "ModelDescriptor.directions: направление %r задано "
                    "двойным" % direction)
            seen.add(direction)

    def supports_direction(self, direction: str) -> bool:
        """Поддерживает ли модель заданное направление."""
        return direction in self.directions

class ModelRegistry:
    """Набор моделей: лёгкое хранилище с уникальными id.

    Порядок регистрации == порядок перечисления (детерминированность:
    одинаковая конфигурация -> одинаковый список) и == приоритет
    (ModelManager.get_default_model берёт первую поддерживающую
    направление модель). get() модель не загружает.
    """

    def __init__(self):
        self._models = {}

    def register(self, descriptor: "ModelDescriptor") -> None:
        """Добавляет модель. Дубликат id — ValueError (существующая
        модель НЕ заменяется)."""
        if not isinstance(descriptor, ModelDescriptor):
            raise TypeError(
                "register(): ожидается ModelDescriptor, получено %s"
                % type(descriptor).__name__)
        if descriptor.id in self._models:
            raise ValueError(
                "register(): model id %r уже зарегистрирован"
                % descriptor.id)
        self._models[descriptor.id] = descriptor

    def get(self, model_id: str) -> "ModelDescriptor":
        """Модель по id. Неизвестный id — ModelNotFoundError (KeyError)."""
        try:
            return self._models[model_id]
        except KeyError:
            raise ModelNotFoundError(
                "ModelRegistry: неизвестный model id %r (зарегистрированы: "
                "%s)" % (model_id, ", ".join(self._models) or "—")) from None

    def list(self) -> list:
        """Все модели в порядке регистрации (детерминированно)."""
        return list(self._models.values())

    def find_by_backend(self, backend: str) -> list:
        """Все модели заданного типа бэкенда (в порядке регистрации)."""
        return [d for d in self._models.values() if d.backend == backend]

    def find_by_direction(self, direction: str) -> list:
        """Все модели, поддерживающие заданное направление."""
        _validate_direction(direction)
        return [d for d in self._models.values()
                if direction in d.directions]

    def __contains__(self, model_id: object) -> bool:
        return model_id in self._models

    def __len__(self) -> int:
        return len(self._models)

    def __iter__(self) -> Iterator["ModelDescriptor"]:
        return iter(self._models.values())

def default_registry() -> ModelRegistry:
    """Стандартный реестр моделей, которые знает проект.

    Одинаковая конфигурация -> одинаковый список (детерминированно;
    порядок регистрации: Marian — первый, т.к. backend по умолчанию,
    затем GGUF). Модели не загружаются, тяжёлые зависимости не
    импортируются.
    """
    registry = ModelRegistry()
    # Marian — ДВЕ независимые HF-модели (по одной на направление) —
    # два direction-specific дескриптора (так работает MarianBackend).
    registry.register(ModelDescriptor(
        id="marian-en-ru",
        name="Marian EN → RU (Helsinki-NLP/opus-mt-en-ru)",
        backend="marian",
        directions=("en-ru",),
        source="HuggingFace: Helsinki-NLP/opus-mt-en-ru (локальный HF-кэш)",
        hf_model_id="Helsinki-NLP/opus-mt-en-ru",
    ))
    registry.register(ModelDescriptor(
        id="marian-ru-en",
        name="Marian RU → EN (Helsinki-NLP/opus-mt-ru-en)",
        backend="marian",
        directions=("ru-en",),
        source="HuggingFace: Helsinki-NLP/opus-mt-ru-en (локальный HF-кэш)",
        hf_model_id="Helsinki-NLP/opus-mt-ru-en",
    ))
    # GGUF — одна модель для обоих направлений, локальный файл
    # (путь — переменная окружения; скачивания нет).
    registry.register(ModelDescriptor(
        id="hy-mt2-1.8b",
        name="Hy-MT2 1.8B GGUF (tencent/Hy-MT2-1.8B-GGUF, напр. Q4_K_M)",
        backend="llama_cpp",
        directions=("en-ru", "ru-en"),
        source=("Локальный GGUF-файл: tencent/Hy-MT2-1.8B-GGUF "
                "(напр. Hy-MT2-1.8B-Q4_K_M.gguf, ~1.1 ГБ); путь — "
                "переменная окружения %s (скачивания нет)" % GGUF_ENV_VAR),
        gguf_model="Hy-MT2-1.8B",
    ))
    return registry


def _marian_hf_cache_has_model(cache_dir: str,
                               repo_id: Optional[str]) -> bool:
    """Лёгкая stdlib-проверка наличия модели в локальном HF-кэше.

    HuggingFace-кэш (v2-layout):
    <cache>/models--<org>--<name>/snapshots/<rev>/config.json (+ веса).
    Наличие config.json в любом snapshot — репозиторий скачан целиком
    (проверка по файловой структуре; без загрузки и без импорта
    transformers).
    """
    if not cache_dir or not repo_id or "/" not in repo_id:
        return False
    repo_dir = os.path.join(
        cache_dir, "models--" + repo_id.replace("/", "--"))
    snapshots = os.path.join(repo_dir, "snapshots")
    if not os.path.isdir(snapshots):
        return False
    for snapshot in os.listdir(snapshots):
        if os.path.isfile(os.path.join(snapshots, snapshot, "config.json")):
            return True
    return False


def _gguf_env_available(model_marker: Optional[str]) -> bool:
    """Лёгкая проверка наличия GGUF-модели: переменная окружения
    OFFLINE_TRANSLATOR_GGUF указывает на существующий .gguf-файл, и
    (если задан маркер) имя файла содержит ожидаемое имя модели.
    """
    path = os.environ.get(GGUF_ENV_VAR, "").strip()
    if not path:
        return False
    path = os.path.expanduser(path)
    if not os.path.isfile(path) or not path.lower().endswith(".gguf"):
        return False
    if (model_marker
            and model_marker.lower() not in os.path.basename(path).lower()):
        return False
    return True

class ModelManager:
    """«Какую модель использовать» и «доступна ли она сейчас».

    Уровень разрешения ВЫБОРА модели, а не inference («как переводить»):

    - list_models() / get_model(model_id) — дескрипторы (без загрузки);
    - is_model_available(model_id) / get_available_models() — наличие
      модели на этой машине СЕЙЧАС (Marian — локальный HF-кэш той же
      директории, что использует бэкенд; GGUF — файл из
      OFFLINE_TRANSLATOR_GGUF). Проверяется БЕЗ загрузки модели и без
      обращения к сети (download-функций в этом этапе нет);
    - get_default_model(direction) — первая модель реестра (порядок
      регистрации == приоритет), поддерживающая направление; None —
      подходящей модели нет. Доступность при этом НЕ требуется
      (GUI потом сам покажет, доступна ли модель);
    - resolve(model_id, direction, require_available) — итоговое
      решение: модель существует, поддерживает направление и (по
      умолчанию) доступна локально.

    Не здесь: Tkinter/GUI, перевод текста, словарь, benchmark,
    скачивание, progress bar, inference.
    """

    def __init__(self, registry: Optional[ModelRegistry] = None,
                 cache_dir: Optional[str] = None):
        self.registry = registry if registry is not None else default_registry()
        # Marian-доступность проверяем в том же кэше, что использует
        # бэкенд (единый источник истины: model_cache_dir). Каталог при
        # этом НЕ создаётся — менеджер ничего не загружает.
        self.cache_dir = cache_dir if cache_dir is not None else model_cache_dir()

    # ------------------------------------------------------------------ #
    #  Дескрипторы (без загрузки)                                         #
    # ------------------------------------------------------------------ #
    def list_models(self) -> list:
        """Все зарегистрированные модели (в порядке реестра)."""
        return self.registry.list()

    def get_model(self, model_id: str) -> ModelDescriptor:
        """Дескриптор по id. Неизвестный id — ModelNotFoundError."""
        return self.registry.get(model_id)

    def get_default_model(self, direction: str) -> Optional[ModelDescriptor]:
        """Модель по умолчанию для направления (первая зарегистрированная
        среди поддерживающих). None — поддерживающей модели нет."""
        _validate_direction(direction)
        for descriptor in self.registry.list():
            if direction in descriptor.directions:
                return descriptor
        return None

    # ------------------------------------------------------------------ #
    #  Доступность (без загрузки, без сети)                               #
    # ------------------------------------------------------------------ #
    def is_model_available(self, model_id: str) -> bool:
        """Доступна ли модель локально СЕЙЧАС.
        Неизвестный id — ModelNotFoundError."""
        return self._availability(self.get_model(model_id))

    def get_available_models(self) -> list:
        """Все доступные модели (в порядке реестра). Ничего не загружает."""
        return [d for d in self.registry.list() if self._availability(d)]

    def resolve(self, model_id: Optional[str] = None,
                direction: Optional[str] = None,
                require_available: bool = True) -> ModelDescriptor:
        """Разрешает выбор: какую модель использовать.

        - указан model_id — модель должна существовать и (если указано
          direction) поддерживать направление;
        - model_id не указан, указан direction — модель по умолчанию
          для направления (ModelNotFoundError — ни одна не подходит);
        - ни того, ни другого — ValueError.
        require_available=True (по умолчанию) — модель должна быть
        доступна локально (иначе ModelUnavailableError).
        """
        if model_id is None and direction is None:
            raise ValueError("resolve(): укажите model_id или direction")
        if direction is not None:
            _validate_direction(direction)
        if model_id is None:
            descriptor = self.get_default_model(direction)
            if descriptor is None:
                raise ModelNotFoundError(
                    "resolve(): ни одна модель не поддерживает направление "
                    "%r" % direction)
        else:
            descriptor = self.get_model(model_id)
        if direction is not None and direction not in descriptor.directions:
            raise ValueError(
                "resolve(): модель %r не поддерживает направление %r "
                "(поддерживает: %s)"
                % (model_id, direction, ", ".join(descriptor.directions)))
        if require_available and not self._availability(descriptor):
            raise ModelUnavailableError(
                "resolve(): модель %r сейчас недоступна локально (%s)"
                % (descriptor.id, descriptor.source))
        return descriptor

    # ------------------------------------------------------------------ #
    #  Внутреннее                                                         #
    # ------------------------------------------------------------------ #
    def _availability(self, descriptor: ModelDescriptor) -> bool:
        if descriptor.backend == "marian":
            return _marian_hf_cache_has_model(
                self.cache_dir, descriptor.hf_model_id)
        if descriptor.backend == "llama_cpp":
            return _gguf_env_available(descriptor.gguf_model)
        return False  # неизвестный тип бэкенда — модель недоступна