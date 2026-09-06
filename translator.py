# -*- coding: utf-8 -*-
import json
import os
import re
import sys
import torch
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

class OfflineTranslator:
    def __init__(self):
        self.dictionary = {}
        self.load_dictionary()
        self.model = None
        self.tokenizer = None
        self.device = "cpu"
        self.load_model()

    def load_dictionary(self):
        """Загружает JSON словарь"""
        dict_path = os.path.join(os.path.dirname(__file__), 'dictionary.json')
        if os.path.exists(dict_path):
            try:
                for encoding in ['utf-8-sig', 'utf-8', 'cp1251']:
                    try:
                        with open(dict_path, 'r', encoding=encoding) as f:
                            self.dictionary = json.load(f)
                            self.dictionary = {k.lower(): v for k, v in self.dictionary.items()}
                            print(f"✓ Словарь загружен ({len(self.dictionary)} слов)")
                            return
                    except UnicodeDecodeError:
                        continue
            except Exception as e:
                print(f"⚠ Ошибка загрузки словаря: {e}")
        print("⚠ Словарь не загружен, используем только нейросеть")

    def load_model(self):
        """Загружает нейросеть MarianMT напрямую, без pipeline"""
        print("Загрузка модели перевода...")
        print("(При первом запуске это займет 2-5 минут)")
        sys.stdout.flush()
        
        try:
            model_name = "Helsinki-NLP/opus-mt-en-ru"
            
            # Загружаем токенизатор
            print("  → Загрузка токенизатора...")
            sys.stdout.flush()
            self.tokenizer = AutoTokenizer.from_pretrained(
                model_name,
                cache_dir="./cache",
            )
            
            # Загружаем модель
            print("  → Загрузка модели...")
            sys.stdout.flush()
            self.model = AutoModelForSeq2SeqLM.from_pretrained(
                model_name,
                cache_dir="./cache",
            )
            
            # Проверяем, есть ли CUDA (видеокарта NVIDIA)
            if torch.cuda.is_available():
                self.device = "cuda"
                self.model = self.model.to(self.device)
                print(f"  ✓ Используется GPU: {torch.cuda.get_device_name(0)}")
            else:
                print("  ✓ Используется CPU")
            
            self.model.eval()  # Режим инференса
            
            print("✓ Модель готова к работе!")
            print("  Теперь можно работать офлайн")
            sys.stdout.flush()
            
        except Exception as e:
            print(f"✗ Ошибка загрузки модели: {e}")
            raise

    def translate(self, text):
        """Основная функция перевода"""
        if not text.strip():
            return ""

        # Проверяем словарь
        lower_text = text.strip().lower()
        if lower_text in self.dictionary:
            return self.dictionary[lower_text]

        # Используем нейросеть напрямую
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
                    inputs = self.tokenizer(
                        sentence,
                        return_tensors="pt",
                        padding=True,
                        truncation=True,
                        max_length=512
                    ).to(self.device)
                    
                    # Генерация перевода
                    with torch.no_grad():
                        outputs = self.model.generate(
                            **inputs,
                            max_length=512,
                            num_beams=4,
                            early_stopping=True
                        )
                    
                    # Декодирование
                    translation = self.tokenizer.decode(
                        outputs[0],
                        skip_special_tokens=True
                    )
                    translated_sentences.append(translation)
            
            return " ".join(translated_sentences)
            
        except Exception as e:
            return f"Ошибка перевода: {str(e)}"