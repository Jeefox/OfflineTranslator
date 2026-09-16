# Offline Translate (EN → RU)

Оффлайн-переводчик с английского на русский на **лёгкой предобученной
нейросети** [Argo Translate](https://github.com/argosopentech/argos-translate).
Работает без сети: перевод слов, фраз, предложений и кусков технического текста.

## Как это устроено

- **Нейросеть** — предобученная модель `translate-en_ru-1_9.argosmodel`
  (в корне проекта), ставится один раз локально и дальше не требует интернета.
- **Глоссарий** — файл `glossary.txt` с точными переводами терминов
  (`термин=перевод`). Покрывает то, что у нейросети бывает неточно.
- **Длинные тексты** — переводятся по абзацам/предложениям (последовательно,
  т.к. движок CTranslate2 не потокобезопасен), с **инкрементальным кэшем**:
  уже переведённые предложения переиспользуются, поэтому правка одного
  предложения не заставляет переводить весь текст заново.

## Установка

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Переводчик сам подставит модель при первом запуске из файла
`translate-en_ru-1_9.argosmodel` (должен лежать в корне проекта).
Можно поставить явно:

```bash
.venv\Scripts\python -m offline_translate.cli --install
```

## Использование

### Графический интерфейс (tkinter)

```bash
.venv\Scripts\python gui.py
```

Два поля: сверху — английский текст, снизу — русский перевод (только чтение).
Перевод идёт в фоновом потоке, окно не блокируется.

### Консоль

```bash
# слово / фраза / предложение
.venv\Scripts\python -m offline_translate.cli "machine learning"
# -> машинное обучение

.venv\Scripts\python -m offline_translate.cli "What is the latency of the request?"
# -> Какова задержка запроса?
```

Длинный текст удобнее из файла (или из stdin):

```bash
.venv\Scripts\python -m offline_translate.cli -t docs/readme.md --time
.venv\Scripts\python -m offline_translate.cli < docs/chunk.txt
```

Полезные флаги:

| Флаг | Назначение |
|------|-----------|
| `-t FILE` / `--text-file` | прочитать переводимый текст из файла |
| `--term EN=RU` | добавить термин в глоссарий на лету (можно несколько) |
| `--time` | показать время перевода (в stderr) |
| `--install` | только установить модель из `.argosmodel` и выйти |

Пример с глоссарием на лету:

```bash
.venv\Scripts\python -m offline_translate.cli \
  --term "page fault=сбой страницы" \
  "If a page fault occurs, the scheduler preempts the task."
```

### Python

```python
from offline_translate import OfflineTranslator

t = OfflineTranslator()

t.translate("neural network").text        # 'нейронная сеть'
t.translate("What is the latency?").text  # 'Какова задержка?'

text = "The kernel allocates virtual memory pages for each process."
print(t.translate_text(text))
```

API:

| Метод | Что делает |
|-------|-----------|
| `translate(text)` | слово / фраза / короткое предложение → `Translation` |
| `translate_sentence(s)` | одно предложение целиком |
| `translate_text(text)` | длинный текст (по абзацам/предложениям) |
| `translate_incremental(text, cache)` | инкрементальный перевод с кэшем по предложениям → `(перевод, новый_кэш)` |
| `translate_incremental_streaming(text, cache, callback)` | то же + промежуточные результаты (`callback(so_far, done, total)`) |
| `add_term(en, ru)` / `remove_term(en)` / `save_glossary()` | управление глоссарием |

`Translation` — датакласс: `.text` (перевод) и `.source` (`"nn"` или `"glossary"`).

## Глоссарий

`glossary.txt` — строки вида `термин=перевод`, строки `#` — комментарии.
Для одиночного входа (слово/фраза) термин из глоссария берётся целиком,
для длинного текста точных замен не применяется (важен контекст), но
термины полезны для отдельных слов/фраз и `--term`.

## Зависимости

- `argostranslate` (тянет `ctranslate2` под капотом) — сам перевод;
- `customtkinter` — графический интерфейс (`gui_ctk.py`);
- `pynput` — глобальный хоткей `Ctrl+Alt+T` (`hotkey_agent.py`);
- `notify-send` (Linux) — системные уведомления; `plyer` — fallback
  (Windows/macOS, либо нет `notify-send`);
- `pyperclip` — буфер обмена (CLIPBOARD); на Linux выделение мышью
  читается из PRIMARY-селекции напрямую через `xclip`/`xsel`.

Сеть нужна **однократно** при установке пакета `pip install -r requirements.txt`;
перевод после установки модели полностью оффлайн.

### Установка зависимостей ОС

| ОС | Что нужно |
|----|-----------|
| **Linux (X11)** | `sudo apt install xclip` (pyperclip читает/пишет буфер через xclip, xsel или wl-clipboard) |
| **Linux (Wayland)** | `sudo apt install wl-clipboard` |
| **Windows** | ничего дополнительно не нужно (встроенный PowerShell) |
| **macOS** | ничего дополнительно не нужно (встроенные pbcopy/pbpaste) |

Если утилита буфера обмена не найдена, агент не крашится: выводит
уведомление «Ошибка: не удалось прочитать буфер (установите xclip)» и
логирует предупреждение в `logging` (логгер `hotkey_agent`).

## Глобальный хоткей

В любой программе выделяете текст и нажимаете **Ctrl+Alt+T**
(настраивается в `config.json`, см. «Настройки») — перевод приходит
системным уведомлением. Работает в фоновых потоках, GUI не блокирует;
перевод дольше 3 с сначала показывает «Перевод...», затем результат.

Как читается текст:
- **Linux/X11** — выделение мышью читается напрямую из
  **PRIMARY**-селекции (`xclip`/`xsel`), буфер CLIPBOARD (Ctrl+C) —
  fallback. Ваш буфер не перезаписывается.
- **Windows/macOS** — CLIPBOARD (`pyperclip`).

## Настройки

Настройки хранятся в `config.json`, который создаётся автоматически при
первом запуске:

- **Linux/macOS** — `~/.config/offline_translate/config.json`
- **Windows** — `%APPDATA%\offline_translate\config.json`

Путь можно переопределить переменной окружения
**`OFFLINE_TRANSLATE_CONFIG`** (абсолютный путь к файлу или каталогу).

| Ключ | Тип | По умолчанию | Назначение |
|------|-----|--------------|------------|
| `hotkey` | str | `ctrl+alt+t` | Глобальный хоткей (формат pynput; модификаторы `ctrl`, `alt`, `shift`, `cmd`) |
| `source_lang` | str | `en` | Исходный язык |
| `target_lang` | str | `ru` | Язык перевода |
| `theme` | str | `dark` | Тема GUI (`dark` / `light`) |
| `slow_after_sec` | int | `3` | Через сколько секунд показывать «Перевод...» |
| `debounce_sec` | float | `1.5` | Пауза автоперевода после ввода в GUI |
| `notify_timeout` | int | `5` | Сколько секунд живёт уведомление |
| `max_text_length` | int | `5000` | Максимальная длина текста из буфера |
| `filter_cyrillic` | bool | `true` | Не переводить, если в буфере кириллица |
| `model_path` | str\|null | `null` | Путь к файлу `.argosmodel`; `null`/пусто — автоопределение (PyInstaller `_MEIPASS` / рядом с проектом). Применяется после перезапуска |

Изменить можно в GUI: кнопка **⚙️ Настройки** в окне приложения.
Тема применяется мгновенно, смена хоткея перезапускает слушателя агента.
Невалидные значения (хоткей, который не парсит pynput; числа ≤ 0;
неизвестные варианты языка/темы) отбрасываются с warning'ом в лог и
заменяются дефолтными.

## Структура

```
OfflineTranslate/
├── offline_translate/
│   ├── __init__.py
│   ├── translator.py   # OfflineTranslator: модель + глоссарий + перевод
│   └── cli.py          # командный интерфейс
├── translate-en_ru-1_9.argosmodel   # предобученная модель (локальная)
├── glossary.txt
├── wordlist.txt        # словарь для автокомплита в GUI
├── gui.py              # графический интерфейс (tkinter)
├── gui_ctk.py          # графический интерфейс (CustomTkinter)
├── hotkey_agent.py     # глобальный хоткей -> перевод буфера
├── settings.py         # Settings: config.json (путь/валидация/дефолты)
├── requirements.txt
├── run_tests.py        # самопроверка: слова/фразы/предложения/текст
└── README.md
```

## Сборка бинарников

Проект собирается в единый исполняемый файл через PyInstaller.
Модель распространяется отдельно — положите файл `*.argosmodel`
(например, `translate-en_ru-1_9.argosmodel`) в ту же папку, что и
бинарник: программа найдёт её автоматически.

### Требования

- Python 3.12+
- `pip install -r requirements.txt pyinstaller`

### Сборка

```bash
python build.py
```

Результат появится в `dist/`: `dist/OfflineTranslate.exe` (Windows)
или `dist/OfflineTranslate` (Linux). После сборки скопируйте
бинарник вместе с файлом модели в одну папку:

```
dist/
├── OfflineTranslate.exe      # собранный бинарник
└── translate-en_ru-1_9.argosmodel   # модель, кладётся рядом вручную
```

`build.spec` описывает сборку: входная точка `gui_ctk.py`,
встраиваются `glossary.txt` и `wordlist.txt`, модель не включается.

## Лицензия

Argo Translate — MIT. Модель `en→ru` — обучена Argo Open Technologies,
разрешено некоммерческое и коммерческое использование согласно условиям
[Argospm](https://github.com/argosopentech/argospm-index).
