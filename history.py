# -*- coding: utf-8 -*-
"""Модуль для управления историей переводов."""

import sqlite3
import csv
import os
from datetime import datetime
from typing import List, Tuple, Optional


class HistoryManager:
    """Менеджер для работы с историей переводов."""
    
    def __init__(self, db_path: str = "history.db"):
        """Инициализирует менеджер истории.
        
        Args:
            db_path: Путь к файлу базы данных SQLite
        """
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """Инициализирует базу данных."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS translations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_text TEXT NOT NULL,
                translated_text TEXT NOT NULL,
                direction TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Индекс для быстрого поиска по дате
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_created_at 
            ON translations(created_at DESC)
        ''')
        
        conn.commit()
        conn.close()
    
    def add_translation(self, source_text: str, translated_text: str, direction: str) -> bool:
        """Добавляет перевод в историю.
        
        Args:
            source_text: Исходный текст
            translated_text: Переведённый текст
            direction: Направление перевода ('en-ru' или 'ru-en')
            
        Returns:
            True если добавление успешно
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Сначала удаляем старые записи, чтобы хранить только 50 последних
            cursor.execute('''
                DELETE FROM translations 
                WHERE id NOT IN (
                    SELECT id FROM translations 
                    ORDER BY created_at DESC 
                    LIMIT 49
                )
            ''')
            
            cursor.execute('''
                INSERT INTO translations (source_text, translated_text, direction)
                VALUES (?, ?, ?)
            ''', (source_text, translated_text, direction))
            
            conn.commit()
            conn.close()
            return True
            
        except Exception as e:
            print(f"Ошибка добавления в историю: {e}")
            return False
    
    def get_history(self, limit: int = 50) -> List[Tuple[int, str, str, str, str]]:
        """Получает историю переводов.
        
        Args:
            limit: Максимальное количество записей
            
        Returns:
            Список кортежей (id, source_text, translated_text, direction, created_at)
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT id, source_text, translated_text, direction, created_at
                FROM translations
                ORDER BY created_at DESC
                LIMIT ?
            ''', (limit,))
            
            results = cursor.fetchall()
            conn.close()
            return results
            
        except Exception as e:
            print(f"Ошибка получения истории: {e}")
            return []
    
    def delete_entry(self, entry_id: int) -> bool:
        """Удаляет запись из истории.
        
        Args:
            entry_id: ID записи для удаления
            
        Returns:
            True если удаление успешно
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM translations WHERE id = ?', (entry_id,))
            
            conn.commit()
            conn.close()
            return True
            
        except Exception as e:
            print(f"Ошибка удаления записи: {e}")
            return False
    
    def clear_history(self) -> bool:
        """Очищает всю историю.
        
        Returns:
            True если очистка успешна
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM translations')
            
            conn.commit()
            conn.close()
            return True
            
        except Exception as e:
            print(f"Ошибка очистки истории: {e}")
            return False
    
    def export_to_csv(self, filepath: str) -> bool:
        """Экспортирует историю в CSV файл.
        
        Args:
            filepath: Путь для экспорта
            
        Returns:
            True если экспорт успешён
        """
        try:
            history = self.get_history(limit=None)  # Получаем всю историю
            
            with open(filepath, 'w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['ID', 'Исходный текст', 'Перевод', 'Направление', 'Дата'])
                
                for entry in history:
                    writer.writerow(entry)
            
            return True
            
        except Exception as e:
            print(f"Ошибка экспорта истории: {e}")
            return False
    
    def get_count(self) -> int:
        """Возвращает количество записей в истории.
        
        Returns:
            Количество записей
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('SELECT COUNT(*) FROM translations')
            count = cursor.fetchone()[0]
            
            conn.close()
            return count
            
        except Exception as e:
            print(f"Ошибка подсчёта истории: {e}")
            return 0
