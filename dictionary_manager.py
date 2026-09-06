# -*- coding: utf-8 -*-
"""Модуль для управления словарём переводчика."""

import json
import os
from typing import Dict, List, Tuple, Optional


class DictionaryManager:
    """Менеджер для работы со словарём переводов."""
    
    def __init__(self, dictionary_path: str = "dictionary.json"):
        """Инициализирует менеджер словаря.
        
        Args:
            dictionary_path: Путь к файлу словаря JSON
        """
        self.dictionary_path = dictionary_path
        self.dictionary: Dict[str, str] = {}
        self.load()
    
    def load(self) -> bool:
        """Загружает словарь из файла.
        
        Returns:
            True если загрузка успешна, иначе False
        """
        if os.path.exists(self.dictionary_path):
            try:
                for encoding in ['utf-8-sig', 'utf-8', 'cp1251']:
                    try:
                        with open(self.dictionary_path, 'r', encoding=encoding) as f:
                            self.dictionary = json.load(f)
                            return True
                    except UnicodeDecodeError:
                        continue
            except Exception as e:
                print(f"Ошибка загрузки словаря: {e}")
        return False
    
    def save(self) -> bool:
        """Сохраняет словарь в файл.
        
        Returns:
            True если сохранение успешно, иначе False
        """
        try:
            with open(self.dictionary_path, 'w', encoding='utf-8') as f:
                json.dump(self.dictionary, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e:
            print(f"Ошибка сохранения словаря: {e}")
            return False
    
    def add_word(self, en_word: str, ru_word: str) -> bool:
        """Добавляет новую пару слов в словарь.
        
        Args:
            en_word: Английское слово
            ru_word: Русский перевод
            
        Returns:
            True если добавление успешно
        """
        en_word = en_word.strip().lower()
        ru_word = ru_word.strip()
        
        if not en_word or not ru_word:
            return False
        
        self.dictionary[en_word] = ru_word
        return self.save()
    
    def edit_word(self, old_en_word: str, new_en_word: str, new_ru_word: str) -> bool:
        """Редактирует существующую пару слов.
        
        Args:
            old_en_word: Старое английское слово
            new_en_word: Новое английское слово
            new_ru_word: Новый русский перевод
            
        Returns:
            True если редактирование успешно
        """
        old_en_word = old_en_word.strip().lower()
        new_en_word = new_en_word.strip().lower()
        new_ru_word = new_ru_word.strip()
        
        if old_en_word not in self.dictionary:
            return False
        
        # Удаляем старую запись
        del self.dictionary[old_en_word]
        
        # Добавляем новую
        if new_en_word and new_ru_word:
            self.dictionary[new_en_word] = new_ru_word
        
        return self.save()
    
    def delete_word(self, en_word: str) -> bool:
        """Удаляет слово из словаря.
        
        Args:
            en_word: Английское слово для удаления
            
        Returns:
            True если удаление успешно
        """
        en_word = en_word.strip().lower()
        
        if en_word in self.dictionary:
            del self.dictionary[en_word]
            return self.save()
        return False
    
    def search(self, query: str) -> List[Tuple[str, str]]:
        """Ищет слова по запросу.
        
        Args:
            query: Строка поиска
            
        Returns:
            Список пар (EN, RU) соответствующих запросу
        """
        query = query.lower().strip()
        results = []
        
        for en, ru in self.dictionary.items():
            if query in en.lower() or query in ru.lower():
                results.append((en, ru))
        
        return sorted(results, key=lambda x: x[0])
    
    def get_all_words(self) -> List[Tuple[str, str]]:
        """Возвращает все слова из словаря.
        
        Returns:
            Список пар (EN, RU)
        """
        return sorted([(en, ru) for en, ru in self.dictionary.items()], key=lambda x: x[0])
    
    def export_to_json(self, filepath: str) -> bool:
        """Экспортирует словарь в другой JSON файл.
        
        Args:
            filepath: Путь для экспорта
            
        Returns:
            True если экспорт успешён
        """
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(self.dictionary, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e:
            print(f"Ошибка экспорта: {e}")
            return False
    
    def import_from_json(self, filepath: str) -> Tuple[int, int]:
        """Импортирует слова из другого JSON файла.
        
        Args:
            filepath: Путь к файлу для импорта
            
        Returns:
            Кортеж (добавлено_слов, ошибок)
        """
        added = 0
        errors = 0
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                new_dict = json.load(f)
            
            for en, ru in new_dict.items():
                en_lower = en.lower().strip()
                ru_stripped = ru.strip()
                if en_lower and ru_stripped:
                    self.dictionary[en_lower] = ru_stripped
                    added += 1
                else:
                    errors += 1
            
            self.save()
            
        except Exception as e:
            print(f"Ошибка импорта: {e}")
            errors += 1
        
        return (added, errors)
    
    def get_count(self) -> int:
        """Возвращает количество слов в словаре.
        
        Returns:
            Количество слов
        """
        return len(self.dictionary)
