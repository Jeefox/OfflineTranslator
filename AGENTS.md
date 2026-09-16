# Offline Translate — Project Rules

## Структура
- `gui_ctk.py` — класс `TranslatorGUI`, все виджеты в `_build()`
- `hotkey_agent.py` — фоновый агент (pynput + xclip + notify-send)
- `offline_translate/translator.py` — Argo Translate, инкрементальный кэш
- `settings.py` — JSON конфиг, валидация через pynput.HotKey.parse

## Ключевые факты (НЕ ЧИТАЙ ЭТИ ФАЙЛЫ, ЭТО УЖЕ ЗНАЕШЬ)
- CTkTextbox проксирует на tkinter.Text через `._textbox`
- Для bind на CTkTextbox: `add="+"`
- Стриминг: `translate_text_streaming(text, callback)`
- Кэш: `self._sent_cache` в gui_ctk.py
- PRIMARY selection: `xclip -selection primary -o`

## ЖЁСТКИЕ ПРАВИЛА
1. НЕ читай файлы если структура известна
2. НЕ запускай тесты без явного запроса "запусти тесты"
3. ПРИМЕНЯЙ изменения сразу через edit/write
4. После применения пиши ТОЛЬКО "ГОТОВО" + краткий summary (3 строки)
5. Максимум 1 файл за раз при правках
## Текущий статус проекта
- Все основные фичи реализованы: GUI, стриминг, кэш, подсветка, синхронный скролл
- Следующая задача: горячие клавиши (Ctrl+A, Ctrl+X) в gui_ctk.py
- Потом: System Tray, релиз на GitHub

## Формат ответа
- После применения изменений: ТОЛЬКО "ГОТОВО" + 2-3 строки что изменил
- НЕ пиши объяснения, НЕ запускай тесты, НЕ читай файлы
- Если нужно уточнение — задай ОДИН вопрос и жди ответа
