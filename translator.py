# -*- coding: utf-8 -*-
import dataclasses
import os
import re
import shutil
import sys
import torch
from typing import Optional
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

from dictionary_manager import default_dictionary_path, load_snapshot
from sentence_pipeline import StreamUnit, assemble_output, split_units


class CacheManager:
    """Определяет постоянное кэш-директорию моделей (не зависит от CWD)
    и при необходимости восстанавливает кэш из бандла, вшитого в EXE."""

    @staticmethod
    def default_cache_dir() -> str:
        """Постоянный кэш в стандартной директории данных пользователя:
        - Windows: %LOCALAPPDATA%\\OfflineTranslator\\cache;
        - Linux/macOS: $XDG_CACHE_HOME/OfflineTranslator/cache
          (если переменная не задана — ~/.cache/OfflineTranslator/cache).
        Путь абсолютный и не зависит от текущей рабочей директории."""
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        else:
            base = (os.environ.get("XDG_CACHE_HOME")
                    or os.path.join(os.path.expanduser("~"), ".cache"))
        return os.path.join(base, "OfflineTranslator", "cache")

    @staticmethod
    def bundled_cache_dir() -> Optional[str]:
        """Кэш, вшитый в EXE через PyInstaller --onefile (sys._MEIPASS).
        Существует только у замороженного приложения. Это временная директория
        (удаляется после выхода), поэтому использовать её можно только
        как read-only источник для восстановления пользовательского кэша."""
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            return os.path.join(sys._MEIPASS, "cache")
        return None

    def __init__(self, cache_dir: Optional[str] = None):
        self.cache_dir = cache_dir or self.default_cache_dir()
        os.makedirs(self.cache_dir, exist_ok=True)
        self._seed_from_bundled()
        print(f"✓ Кэш моделей: {self.cache_dir}")

    def _seed_from_bundled(self):
        """Копирует в пользовательский кэш недостающие файлы моделей из бандла EXE.
        Уже скачанные файлы не перетираются; временная директория _MEIPASS
        используется только как источник для чтения."""
        bundled = self.bundled_cache_dir()
        if not bundled or not os.path.isdir(bundled):
            return
        try:
            for root, _dirs, files in os.walk(bundled):
                for name in files:
                    src = os.path.join(root, name)
                    dst = os.path.join(self.cache_dir, os.path.relpath(src, bundled))
                    if os.path.exists(dst):
                        continue
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
        except Exception as e:
            print(f"⚠ Не удалось восстановить модели из бандла EXE: {e}")


class OfflineTranslator:
    """Класс офлайн-переводчика с поддержкой двустороннего перевода EN↔RU."""
    
    def __init__(self, cache_dir: Optional[str] = None):
        # Словарь: постоянного состояния в памяти нет. dictionary.json —
        # единственный источник истины (пишет DictionaryManager), актуальный
        # snapshot читается в начале каждого translate() (load_snapshot).
        self.dictionary_path = default_dictionary_path()
        self._dict_load_warned = False
        # Постоянный кэш моделей (не зависит от CWD); для EXE — с восстановлением из бандла
        self.cache_manager = CacheManager(cache_dir)
        
        # Модели для разных направлений перевода
        self.model_en_ru = None
        self.tokenizer_en_ru = None
        self.model_ru_en = None
        self.tokenizer_ru_en = None
        self.device = "cpu"
        # Безопасный лимит входных токенов (512 — лимит Marian, минус запас);
        # при загрузке моделей уточняется из их config
        self.max_source_tokens = 480
        
        self.load_models()
    
    def load_models(self):
        """Загружает нейросети MarianMT для обоих направлений перевода."""
        print("Загрузка моделей перевода...")
        print("(При первом запуске это займет 2-5 минут)")
        sys.stdout.flush()
        
        try:
            # Определяем устройство
            if torch.cuda.is_available():
                self.device = "cuda"
                print(f"✓ Используется GPU: {torch.cuda.get_device_name(0)}")
            else:
                print("✓ Используется CPU")
            
            # Загружаем модель EN→RU
            self._load_model_direction("en-ru", "Helsinki-NLP/opus-mt-en-ru")
            
            # Загружаем модель RU→EN
            self._load_model_direction("ru-en", "Helsinki-NLP/opus-mt-ru-en")
            
            print("✓ Обе модели готовы к работе!")
            print("  Теперь можно работать офлайн")
            sys.stdout.flush()
            
        except Exception as e:
            print(f"✗ Ошибка загрузки моделей: {e}")
            raise
    
    def _load_model_direction(self, direction: str, model_name: str):
        """Загружает конкретную модель для направления перевода.
        
        Args:
            direction: Направление перевода ('en-ru' или 'ru-en')
            model_name: Имя модели HuggingFace
        """
        print(f"  → Загрузка модели {direction}...")
        sys.stdout.flush()
        
        cache_dir = self.cache_manager.cache_dir
        
        tokenizer = AutoTokenizer.from_pretrained(
            model_name,
            cache_dir=cache_dir,
        )
        
        model = AutoModelForSeq2SeqLM.from_pretrained(
            model_name,
            cache_dir=cache_dir,
        )
        
        if self.device == "cuda":
            model = model.to(self.device)
        
        model.eval()  # Режим инференса
        # Реальный лимит входа берём из config модели, а не хардкодим
        self.max_source_tokens = self._max_source_tokens(model)
        
        if direction == "en-ru":
            self.tokenizer_en_ru = tokenizer
            self.model_en_ru = model
        else:
            self.tokenizer_ru_en = tokenizer
            self.model_ru_en = model
        
        print(f"  ✓ Модель {direction} загружена")
        sys.stdout.flush()
    
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
    
    def _max_source_tokens(self, model) -> int:
        """Безопасный лимит входных токенов: реальный лимит модели
        (config.max_position_embeddings, у Marian — 512) минус небольшой
        запас для стабильного inference."""
        cfg = getattr(model, "config", None)
        max_pos = getattr(cfg, "max_position_embeddings", None)
        if not (isinstance(max_pos, int) and max_pos > 64):
            max_pos = 512
        return max_pos - 32
    
    def _token_count(self, tokenizer, text: str) -> int:
        """Реальное количество входных токенов (не число символов)."""
        return len(tokenizer(text, add_special_tokens=True)["input_ids"])
    
    def _split_sentence_to_chunks(self, sentence: str, tokenizer) -> list:
        """Разбивает предложение на куски, укладывающиеся в лимит токенов.
        
        Короткое предложение возвращается как есть (один кусок → один inference).
        Сначала пробует естественные границы (, ; : — –), затем по словам;
        все части сохраняются, порядок не меняется.
        """
        limit = self.max_source_tokens
        if self._token_count(tokenizer, sentence) <= limit:
            return [sentence]
        # 1) естественные границы внутри слишком длинного «предложения»
        parts = [p for p in re.split(r'(?<=[,;:—–])\s*', sentence) if p.strip()]
        # 2) жадная сборка частей в куски по лимиту токенов
        chunks = []
        current = ""
        for part in parts:
            if not current:
                current = part
            elif self._token_count(tokenizer, current + " " + part) <= limit:
                current += " " + part
            else:
                chunks.append(current)
                current = part
        if current:
            chunks.append(current)
        # 3) кусок всё ещё длиннее лимита — разбиваем по словам
        result = []
        for chunk in chunks:
            if self._token_count(tokenizer, chunk) <= limit:
                result.append(chunk)
            else:
                result.extend(self._split_by_words(chunk, tokenizer, limit))
        return result
    
    def _split_by_words(self, text: str, tokenizer, limit: int) -> list:
        """Жадная сборка слов в куски по лимиту токенов (не режет слова)."""
        words = text.split()
        chunks = []
        current = []
        for word in words:
            if current and self._token_count(tokenizer, " ".join(current + [word])) > limit:
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
            if self._token_count(tokenizer, chunk) <= limit:
                result.append(chunk)
            else:
                result.extend(self._split_long_word(chunk, tokenizer, limit))
        return result
    
    def _split_long_word(self, word: str, tokenizer, limit: int) -> list:
        """Разбивает слишком длинную строку без пробелов по символам
        (последняя линия обороны от потери текста)."""
        pieces = []
        start = 0
        while start < len(word):
            end = len(word)
            while end > start + 1 and self._token_count(tokenizer, word[start:end]) > limit:
                end = start + (end - start) // 2
            if self._token_count(tokenizer, word[start:end]) > limit:
                end = start + 1
            pieces.append(word[start:end])
            start = end
        return pieces
    
    def _translate_chunk(self, chunk: str, model, tokenizer) -> str:
        """Перевод одного куска, заранее укладывающегося в лимит токенов.
        
        truncation=True здесь — только страховочная сеть (не срабатывает,
        т.к. куски ≤ self.max_source_tokens), а не механизм обработки
        длинного текста.
        """
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
    
    def _translate_paragraph(self, sentences: list, model, tokenizer) -> str:
        """Переводит абзац: каждое предложение разбивается на куски,
        каждый кусок переводится отдельно, порядок сохраняется."""
        translated_sentences = []
        for sentence in sentences:
            chunks = self._split_sentence_to_chunks(sentence, tokenizer)
            translated_sentences.append(
                " ".join(self._translate_chunk(chunk, model, tokenizer) for chunk in chunks)
            )
        return " ".join(translated_sentences)
    
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
        snapshot = load_snapshot(self.dictionary_path)
        if snapshot is None:
            if not self._dict_load_warned:
                print(f"⚠ Словарь не загружен ({self.dictionary_path}), "
                      f"используем только нейросетевой перевод")
                self._dict_load_warned = True
        else:
            self._dict_load_warned = False

        # Модель выбирается направлением; словарный lookup — двунаправленный
        # (snapshot содержит и en -> ru, и ru -> en)
        if direction == "en-ru":
            model = self.model_en_ru
            tokenizer = self.tokenizer_en_ru
        else:  # ru-en
            model = self.model_ru_en
            tokenizer = self.tokenizer_ru_en

        # Проверяем словарь (двунаправленное точное совпадение)
        if snapshot is not None:
            dict_result = snapshot.lookup(text)
            if dict_result is not None:
                return dict_result
        
        # Используем нейросеть
        try:
            # Абзацы → предложения → куски, укладывающиеся в лимит входных
            # токенов; каждый кусок переводится отдельно, порядок сохраняется
            translated_paragraphs = [
                self._translate_paragraph(sentences, model, tokenizer)
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
          укладывающиеся в лимит входных токенов (существующий
          _split_sentence_to_chunks — источник истины; sentence[:512]
          и len() <= 512 здесь не используются).

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
        snapshot = load_snapshot(self.dictionary_path)
        if snapshot is None:
            if not self._dict_load_warned:
                print(f"⚠ Словарь не загружен ({self.dictionary_path}), "
                      f"используем только нейросетевой перевод")
                self._dict_load_warned = True
        else:
            self._dict_load_warned = False

        if direction == "en-ru":
            model, tokenizer = self.model_en_ru, self.tokenizer_en_ru
        else:
            model, tokenizer = self.model_ru_en, self.tokenizer_ru_en

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
            # перевод предложения (порядок chunks сохраняется).
            chunks = self._split_sentence_to_chunks(u.text, tokenizer)
            translation = " ".join(
                self._translate_chunk(chunk, model, tokenizer) for chunk in chunks
            )
            done += 1
            if on_sentence is not None:
                on_sentence("done", done, total,
                            dataclasses.replace(unit,
                                                translation=translation))
            translations.append(translation)
        return assemble_output(units, translations)
