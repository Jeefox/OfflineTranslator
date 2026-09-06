# -*- coding: utf-8 -*-
import json
import os
import re
import sys
import torch
from typing import Dict, Optional
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM


class OfflineTranslator:
    """Класс офлайн-переводчика с поддержкой двустороннего перевода EN↔RU."""
    
    def __init__(self):
        self.dictionary: Dict[str, str] = {}
        self.reverse_dictionary: Dict[str, str] = {}
        self.load_dictionary()
        
        # Модели для разных направлений перевода
        self.model_en_ru = None
        self.tokenizer_en_ru = None
        self.model_ru_en = None
        self.tokenizer_ru_en = None
        self.device = "cpu"
        
        self.load_models()
    
    def load_dictionary(self):
        """Загружает JSON словарь и создаёт обратный словарь."""
        dict_path = os.path.join(os.path.dirname(__file__), 'dictionary.json')
        if os.path.exists(dict_path):
            try:
                for encoding in ['utf-8-sig', 'utf-8', 'cp1251']:
                    try:
                        with open(dict_path, 'r', encoding=encoding) as f:
                            self.dictionary = json.load(f)
                            # Нормализуем ключи к нижнему регистру
                            self.dictionary = {k.lower(): v for k, v in self.dictionary.items()}
                            # Создаём обратный словарь RU→EN
                            self.reverse_dictionary = {v.lower(): k for k, v in self.dictionary.items()}
                            print(f"✓ Словарь загружен ({len(self.dictionary)} слов)")
                            return
                    except UnicodeDecodeError:
                        continue
            except Exception as e:
                print(f"⚠ Ошибка загрузки словаря: {e}")
        print("⚠ Словарь не загружен, используем только нейросеть")
    
    def save_dictionary(self, dictionary: Dict[str, str]) -> bool:
        """Сохраняет словарь в JSON файл.
        
        Args:
            dictionary: Словарь EN→RU для сохранения
            
        Returns:
            True если сохранение успешно, иначе False
        """
        dict_path = os.path.join(os.path.dirname(__file__), 'dictionary.json')
        try:
            with open(dict_path, 'w', encoding='utf-8') as f:
                json.dump(dictionary, f, ensure_ascii=False, indent=4)
            self.dictionary = {k.lower(): v for k, v in dictionary.items()}
            self.reverse_dictionary = {v.lower(): k for k, v in dictionary.items()}
            print(f"✓ Словарь сохранён ({len(self.dictionary)} слов)")
            return True
        except Exception as e:
            print(f"✗ Ошибка сохранения словаря: {e}")
            return False
    
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
        
        tokenizer = AutoTokenizer.from_pretrained(
            model_name,
            cache_dir="./cache",
        )
        
        model = AutoModelForSeq2SeqLM.from_pretrained(
            model_name,
            cache_dir="./cache",
        )
        
        if self.device == "cuda":
            model = model.to(self.device)
        
        model.eval()  # Режим инференса
        
        if direction == "en-ru":
            self.tokenizer_en_ru = tokenizer
            self.model_en_ru = model
        else:
            self.tokenizer_ru_en = tokenizer
            self.model_ru_en = model
        
        print(f"  ✓ Модель {direction} загружена")
        sys.stdout.flush()
    
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
        
        # Выбираем модель и словарь в зависимости от направления
        if direction == "en-ru":
            dictionary = self.dictionary
            model = self.model_en_ru
            tokenizer = self.tokenizer_en_ru
        else:  # ru-en
            dictionary = self.reverse_dictionary
            model = self.model_ru_en
            tokenizer = self.tokenizer_ru_en
        
        # Проверяем словарь (точное совпадение)
        lower_text = text.strip().lower()
        if lower_text in dictionary:
            return dictionary[lower_text]
        
        # Используем нейросеть
        try:
            # Разбиваем на предложения
            sentences = re.split(r'(?<=[.!?]) +', text)
            translated_sentences = []
            
            for sentence in sentences:
                if sentence.strip():
                    # Ограничиваем длину
                    if len(sentence) > 512:
                        sentence = sentence[:512]
                    
                    # Токенизация
                    inputs = tokenizer(
                        sentence,
                        return_tensors="pt",
                        padding=True,
                        truncation=True,
                        max_length=512
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
                    translation = tokenizer.decode(
                        outputs[0],
                        skip_special_tokens=True
                    )
                    translated_sentences.append(translation)
            
            return " ".join(translated_sentences)
            
        except Exception as e:
            return f"Ошибка перевода: {str(e)}"
