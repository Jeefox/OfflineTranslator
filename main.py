# -*- coding: utf-8 -*-

import bisect
import customtkinter as ctk
import logging
import platform
import re
import tkinter as tk
from tkinter import messagebox
from translator import OfflineTranslator
from settings import Settings, normalize_hotkey, validate_value
from hotkey_agent import HotkeyAgent, PYNPUT_AVAILABLE
from notifications import show_notification
from system_tray import SystemTray
from sentence_pipeline import line_offsets, off_to_tk, tk_to_off
from model_registry import (
    ModelManager,
    ModelNotFoundError,
    SUPPORTED_DIRECTIONS,
)
import queue
import threading

# Логи: технические детали (причины ошибок, сырые исключения) пишутся
# сюда, а НЕ в пользовательский статус (Этап 14).
logger = logging.getLogger("offline_translate.gui")

# Tk-события вида ``<Control-c>`` используют keysym, который зависит от
# текущей раскладки. Для системных сочетаний переводчика используем
# физический keycode клавиши. Наборы соответствуют X11, Win32 и macOS Tk;
# поэтому Ctrl+C/V/A/L работает одинаково в EN, RU и других раскладках.
_PHYSICAL_KEYCODES = {
    "Linux": {
        "a": 38, "b": 56, "c": 54, "d": 40, "e": 26, "f": 41,
        "g": 42, "h": 43, "i": 31, "j": 44, "k": 45, "l": 46,
        "m": 58, "n": 57, "o": 32, "p": 33, "q": 24, "r": 27,
        "s": 39, "t": 28, "u": 30, "v": 55, "w": 25, "x": 53,
        "y": 29, "z": 52,
        "comma": 59, "return": 36, "escape": 9,
    },
    "Windows": {
        "a": 65, "b": 66, "c": 67, "d": 68, "e": 69, "f": 70,
        "g": 71, "h": 72, "i": 73, "j": 74, "k": 75, "l": 76,
        "m": 77, "n": 78, "o": 79, "p": 80, "q": 81, "r": 82,
        "s": 83, "t": 84, "u": 85, "v": 86, "w": 87, "x": 88,
        "y": 89, "z": 90,
        "comma": 188, "return": 13, "escape": 27,
    },
    "Darwin": {
        "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3,
        "g": 5, "h": 4, "i": 34, "j": 38, "k": 40, "l": 37,
        "m": 46, "n": 45, "o": 31, "p": 35, "q": 12, "r": 15,
        "s": 1, "t": 17, "u": 32, "v": 9, "w": 13, "x": 7,
        "y": 16, "z": 6,
        "comma": 43, "return": 36, "escape": 53,
    },
}
_CONTROL_MASK = 0x0004


def _physical_key_name(event):
    """Имя системной клавиши по физическому Tk keycode."""
    codes = _PHYSICAL_KEYCODES.get(platform.system(), {})
    for name, code in codes.items():
        if event.keycode == code:
            return name
    return None

# Настройка внешнего вида
ctk.set_appearance_mode("Dark")  # Темная тема (или "Light", "System")
ctk.set_default_color_theme("blue")

# =====================================================================
# Тёмная палитра (Catppuccin Mocha, как в старой версии) —
# единое место для всех цветов интерфейса.
# =====================================================================
BG_COLOR = "#1e1e2e"        # фон окна
FIELD_BG = "#2a2a3c"        # текстовые поля
PANEL_BG = "#313244"       # панели, вторичные кнопки
PANEL_HOVER = "#45475a"    # hover вторичных элементов
ACCENT = "#89b4fa"         # основное действие, подпись «Перевод»
ACCENT_HOVER = "#74a8fc"
TEXT_COLOR = "#cdd6f4"      # основной текст
MUTED_COLOR = "#a6adc8"     # приглушённый текст (подзаголовок, статус)
SUCCESS_COLOR = "#a6e3a1"   # статус: готово / скопировано
ERROR_COLOR = "#f38ba8"     # статус: ошибки
PENDING_COLOR = "#fab387"   # статус: инициализация / идёт перевод

# Стандартный текст «готового» статуса.
READY_TEXT = "Готов к переводу! (Работает офлайн)"

# Цветовые темы (Catppuccin). "dark" — текущая палитра по умолчанию (Mocha),
# "light" — та же палитра в светлой гамме (Latte). Тема выбирается в «Настройках»
# и применяется без перезапуска.
# "hl" — мягкая подсветка текущего предложения в полях (тот же приём, что в
# старой версии gui_ctk.py, но цвет — из текущей палитры; без кислотных тонов).
PALETTES = {
    "dark": {
        "bg": BG_COLOR, "field": FIELD_BG, "panel": PANEL_BG,
        "panel_hover": PANEL_HOVER, "scrollbar": PANEL_HOVER,
        "scrollbar_hover": "#585b70",
        "accent": ACCENT, "accent_hover": ACCENT_HOVER, "accent_text": BG_COLOR,
        "text": TEXT_COLOR, "muted": MUTED_COLOR,
        "success": SUCCESS_COLOR, "error": ERROR_COLOR, "pending": PENDING_COLOR,
        "hl": "#3b4768",
    },
    "light": {
        # Контрастная светлая палитра: тёмный текст на светлых полях и
        # различимые состояния кнопок, статуса и подсветки.
        "bg": "#f5f7fb", "field": "#ffffff", "panel": "#e3e8f0",
        "panel_hover": "#d4dce8", "scrollbar": "#b6c2d2",
        "scrollbar_hover": "#9eacbf",
        "accent": "#1d4ed8", "accent_hover": "#1e40af", "accent_text": "#ffffff",
        "text": "#1f2937", "muted": "#4b5563",
        "success": "#166534", "error": "#b91c1c", "pending": "#b45309",
        "hl": "#dbeafe",
    },
}


class _StaleTranslation(Exception):
    """Поколение перевода устарело — worker завершается как можно раньше
    и не публикует результатов (проверка делается на каждом предложении)."""

class TranslatorApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Офлайн Переводчик (Английский ↔ Русский)")
        self.geometry("900x600")
        self.resizable(True, True)
        # Минимальный размер окна: меньше него сетка «два поля бок о бок»
        # не остаётся читаемой (дублирует minsize у строк/колоннок в setup_ui).
        self.minsize(700, 450)

        # Настройки: загружаются при старте, хранятся в settings.json
        # (механизм перенесён из старой версии, см. settings.py). Тема и
        # стартовое направление применяются здесь; параметры автоперевода
        # и глобальный хоткей используются «на лету».
        self.settings = Settings()
        self._pal = dict(PALETTES.get(self.settings.get("theme"), PALETTES["dark"]))
        ctk.set_appearance_mode(self.settings.get("theme"))
        self.configure(fg_color=self._pal["bg"])

        # Направление перевода ("en-ru" или "ru-en") — стартовое из настроек.
        self.direction = f"{self.settings.get('source_lang')}-{self.settings.get('target_lang')}"
        # Выбор модели (Этап 10): ModelManager — backend-нейтральный реестр
        # (дескрипторы + лёгкие filesystem-проверки; GUI не знает ни
        # конкретные inference-бэкенды, ни их тяжёлые библиотеки).
        # Сохранённый
        # model_id (settings.json) разрешается против направления:
        # неизвестная/несовместимая модель — дефолт направления (исправленное
        # значение сохраняется). Marian допускается без пользовательского
        # кэша: его загрузит backend из бандля или сети.
        self.model_manager = ModelManager()
        self._active_model_id, persist_id, self._model_note = \
            self.model_manager.resolve_runtime(
                self.settings.get("model_id"), self.direction)
        self.model_id = persist_id if persist_id is not None \
            else self.settings.get("model_id")
        if persist_id is not None:
            self.settings.set("model_id", persist_id)
            self.settings.save()
        # Счётчик поколений операций перевода: результат применяется к UI
        # только если поколение совпадает (защита от «устаревшего» результата
        # после очистки полей / смены направления / swap).
        self._translation_generation = 0
        # Потокобезопасная очередь «фоновый поток -> главный поток Tkinter»:
        # все изменения GUI происходят только в главном потоке.
        self._gui_queue = queue.Queue()

        # Инициализация переводчика в отдельном потоке, чтобы окно не зависло при загрузке
        self.translator = None
        # Модель, которой реально загружен текущий self.translator
        # (None — ещё не загружен; может отличаться от self.model_id
        # при runtime-фолбэке на доступный дефолт, Этап 10).
        self._translator_model_id = None
        # Идёт (пере)загрузка модели: True в _start_model_load,
        # False в init_done/init_error (Этап 14) — надёжное состояние
        # «идёт загрузка» (в отличие от мимолётного translator = None,
        # который фоновый поток может уже успеть заменить).
        self._model_loading = False

        # Состояние автоперевода / single-flight:
        # _translation_busy — сейчас идёт перевод;
        # _pending_text — самое свежий запрошенный текст, пока идёт перевод
        #   (коалесинг: новое требование перекрывает старое; после завершения
        #   текущего перевода переводится именно последнее запрошенное);
        # _auto_translate_after — debounce-таймер автоперевода;
        # _translating_status_after — таймер показа статуса «Перевод...».
        self._translation_busy = False
        self._pending_text = None
        self._active_text = None  # текст запущенного сейчас перевода (single-flight)
        self._auto_translate_after = None
        self._translating_status_after = None
        # Инкрементальный попредложенический перевод (Этап 4; семантика
        # подсветки Этапа 5; hover-сопоставление Этапа 10).
        # _hl_src_tag/_hl_dst_tag — теги подсветки ТЕКУЩЕЙ пары
        # «предложение k ↔ перевод k» в полях (мягкий фон текущей палитры).
        # В любой момент подсвечена ровно ОДНА пара (активный unit):
        # _hl_unit — индекс подсвеченного юнита (None — ничего не
        # подсвечено). _stream_unit — последний ЗАВЕРШЁННЫЙ стрим-юнит
        # (для восстановления подсветки после hover, если перевод ещё
        # идёт; автопоказ _show_current_pair тоже следует за ним).
        # _unit_map — mapping пайплайна
        # «юнит k ↔ перевод k»: (src_start, src_end, dst_start, dst_end)
        # по завершённым юнитам — строится ВРЕМЯ перевода (смещения src —
        # из StreamUnit, dst — из фактической вставки в поле вывода) и
        # используется hover'ом; сопоставление по ТЕКСТУ не делается.
        # Одно логическое предложение = одна единица синхронизации UI:
        # сколько бы технических chunks ни было внутри предложения,
        # пара продвигается ровно один раз на предложение (на "done").
        self._hl_src_tag = "hl_src"
        self._hl_dst_tag = "hl_dst"
        self._hl_unit = None
        self._stream_unit = None
        self._unit_map = []
        # Начало src/dst-диапазона каждого юнита (для bisect в hover):
        # сортированы по построению — юниты добавляются по порядку.
        self._src_starts = []
        self._dst_starts = []
        # Кэш начал строк для hover (id(внутреннего Text) -> (len, offs)).
        self._hover_cache = {}
        # Синхронная прокрутка полей (Этап 5; исправление Этапа 10):
        # _syncing_scroll — защита от рекурсии (пока код программно
        # выставляет вид партнёра через yview_moveto, callback партнёра
        # не синхронизирует обратно); _sync_last — последняя yview-фракция
        # каждого поля (инкрементальная синхронизация по дельте доли
        # документа — фикс «прокрутка вверх не синхронизируется»);
        # _auto_follow — автопоказ текущей пары при переводе (выключается,
        # когда пользователь прокручивает вручную; включается заново при
        # следующем запуске перевода).
        self._syncing_scroll = False
        self._sync_last = {}
        self._auto_follow = True
        # Текущий статус (вид, текст) — переотрисовывается при смене темы.
        self._status = ("busy", "Инициализация нейросети...")
        # Окно настроек и агент глобального хоткея.
        self._settings_dialog = None
        self._hotkey_agent = None
        self._agent_error = None
        self._tray = SystemTray(self, self._show_from_tray, self._quit_app)
        self._notification_after_translation = False
        self._pending_agent_text = None

        self.setup_ui()
        self._bind_hotkeys()
        self._start_hotkey_agent()
        if not self._tray.start():
            logger.warning("Системный трей недоступен")

        # Начинаем опрос очереди в главном потоке.
        self._process_gui_queue()

        # Запускаем загрузку выбранной модели в фоне (Этап 10): все
        # виджеты уже созданы, и сам поток не обращается к GUI (только к
        # self.translator и очереди).
        self._start_model_load()

        # При закрытии окна остановим глобальный агент хоткея.
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

    def init_translator(self):
        """Загружает выбранную модель в фоновом потоке (Этап 10).

        Тот же механизм, что и при старте: _start_model_load."""
        self._start_model_load()

    def _start_model_load(self):
        """(Пере)загружает переводчик в фоновом потоке (Этап 10).

        Пока идёт загрузка self.translator = None — это же поведение, что
        при старте: кнопка «Перевести» отключена (в т.ч. при (пере)загрузке
        в середине сессии — Этап 14), _start_translation_internal ждёт
        («Ожидание загрузки моделей...»), автоперевод после init_done
        отработает отложенно. Поток не обращается к виджетам (только к
        self.translator и очереди) — threading-модель старта сохранена.
        """
        # Счётчик загрузок: устаревший worker (после новой смены
        # направления/модели) не должен переопределять свежий результат.
        self._load_seq = getattr(self, "_load_seq", 0) + 1
        self.translator = None
        self._model_loading = True
        # Пока модель не загружена, ручной перевод невозможен — кнопка
        # отключена до init_done (включается в init_done; при init_error
        # остаётся отключённой). Без этого клик во время загрузки давал
        # ложное «Ожидание загрузки моделей...» (Этап 14).
        self.translate_btn.configure(state="disabled")
        self._set_status("busy", "Инициализация модели...")
        threading.Thread(target=self._init_translator_worker,
                         args=(self._load_seq,), daemon=True).start()

    def _init_translator_worker(self, load_seq: int):
        """Фоновый поток: OfflineTranslator(model_id=...).

        GUI не знает бэкенды: конкретная модель передаётся через
        backend-нейтральный model_id (реестр, Этап 8/9); фасад сам
        собирает движок. Ошибка — в очередь (clear error, без traceback).
        Устаревшая загрузка (load_seq != self._load_seq — за время
        инициализации сменили направление/модель) не применяется.
        """
        try:
            translator = OfflineTranslator(model_id=self._active_model_id)
        except Exception as e:
            self._gui_queue.put(("init_error", str(e)))
            return
        if load_seq != self._load_seq:
            # Во время загрузки запустили новую — этот результат не нужен.
            return
        self.translator = translator
        self._translator_model_id = self._active_model_id
        self._gui_queue.put(("init_done",))

    def setup_ui(self):
        # Главный контейнер
        main_frame = ctk.CTkFrame(self, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=20, pady=10)

        # Заголовок (строка 0): название приложения + справа кнопка
        # «Настройки» и приглушённое пояснение.
        self.header_title = ctk.CTkLabel(main_frame, text="Офлайн Переводчик",
                                         font=("Arial", 20, "bold"),
                                         text_color=self._pal["text"])
        self.header_title.grid(row=0, column=0, padx=10, pady=(6, 0), sticky="w")

        header_right = ctk.CTkFrame(main_frame, fg_color="transparent")
        header_right.grid(row=0, column=1, padx=10, pady=(6, 0), sticky="e")
        self.settings_btn = ctk.CTkButton(header_right, text="Настройки",
                                          command=self._open_settings,
                                          width=110, height=30, font=("Arial", 13),
                                          corner_radius=8,
                                          fg_color=self._pal["panel"],
                                          hover_color=self._pal["panel_hover"],
                                          text_color=self._pal["text"])
        self.settings_btn.pack(side="left", padx=(0, 10))
        self.header_subtitle = ctk.CTkLabel(header_right, text="Английский ↔ Русский · работает офлайн",
                                            font=("Arial", 12),
                                            text_color=self._pal["muted"])
        self.header_subtitle.pack(side="left")

        # Responsive-сетка: колонки 0/1 и строка 2 (текстовые поля) имеют
        # weight=1 — занимают всё свободное пространство и растягиваются
        # вместе с окном (в т.ч. при максимизации). Остальные строки
        # (подписи, направление, кнопки, статус) — weight=0: фиксированной
        # высоты, поэтому кнопки и панели не растягиваются. minsize страхует
        # от схлопывания полей при малом размере окна.
        main_frame.grid_columnconfigure(0, weight=1, minsize=220)
        main_frame.grid_columnconfigure(1, weight=1, minsize=220)
        main_frame.grid_rowconfigure(2, weight=1, minsize=150)

        # Поле ввода (Английский)
        self.input_label = ctk.CTkLabel(main_frame, text="Исходный текст (Английский)",
                                        font=("Arial", 13, "bold"),
                                        text_color=self._pal["text"])
        self.input_label.grid(row=1, column=0, padx=10, pady=(10, 4), sticky="w")
        
        # Без фиксированных width/height: размер задаёт grid
        # (sticky="nsew" + weight) — поле тянется по ширине и высоте с окном.
        # wrap="word" (Этап 12): визуальный перенос длинных строк по ширине
        # виджета — задача самого Text; в ЛОГИЧЕСКИЙ текст переносы не
        # вставляются (единственные физические \n — границы абзацев "\n\n"),
        # поэтому смещения _unit_map и hover не зависят от размера окна.
        self.input_text = ctk.CTkTextbox(main_frame, font=("Arial", 14),
                                         wrap="word",
                                         undo=True,
                                         fg_color=self._pal["field"], text_color=self._pal["text"],
                                         corner_radius=10,
                                         scrollbar_button_color=self._pal["scrollbar"],
                                         scrollbar_button_hover_color=self._pal["scrollbar_hover"])
        self.input_text.grid(row=2, column=0, padx=10, pady=(0, 10), sticky="nsew")
        # Автоперевод: <<Modified>> срабатывает только от пользовательских
        # правок — программные insert/delete из кода (swap/очистка/агент)
        # событие НЕ порождают, поэтому нет рекурсии и «лишних» переводов.
        self.input_text.bind("<<Modified>>", self._on_input_modified)

        # Поле вывода (Русский)
        self.output_label = ctk.CTkLabel(main_frame, text="Перевод (Русский)",
                                         font=("Arial", 13, "bold"),
                                         text_color=self._pal["accent"])
        self.output_label.grid(row=1, column=1, padx=10, pady=(10, 4), sticky="w")
        
        # wrap="word" — как у поля ввода (Этап 12): перенос по ширине —
        # визуальный, текст и смещения не меняются.
        self.output_text = ctk.CTkTextbox(main_frame, font=("Arial", 14),
                                          wrap="word",
                                          undo=True,
                                          fg_color=self._pal["field"], text_color=self._pal["text"],
                                          corner_radius=10,
                                          scrollbar_button_color=self._pal["scrollbar"],
                                          scrollbar_button_hover_color=self._pal["scrollbar_hover"])
        self.output_text.grid(row=2, column=1, padx=10, pady=(0, 10), sticky="nsew")

        # Теги подсветки предложений (Этап 4): фон — из текущей палитры,
        # при смене темы перенастраиваются (_set_theme).
        self._setup_highlight_tags()

        # Синхронная прокрутка исходного и выводного полей (Этап 5):
        # относительная позиция документа + защита от рекурсии.
        self._setup_scroll_sync()

        # Компактный блок направления (строка 3, слева): подпись + меню +
        # «Сменить местами» — направление видно и понятно без пояснений.
        self.direction_frame = ctk.CTkFrame(main_frame, fg_color=self._pal["panel"], corner_radius=12)
        self.direction_frame.grid(row=3, column=0, columnspan=2, pady=(10, 0), sticky="w")

        self.direction_caption = ctk.CTkLabel(self.direction_frame, text="Направление:",
                                         font=("Arial", 13), text_color=self._pal["text"])
        self.direction_caption.pack(side="left", padx=(14, 6), pady=8)

        self.direction_var = ctk.StringVar(value="Английский → Русский")
        self.direction_menu = ctk.CTkOptionMenu(
            self.direction_frame,
            variable=self.direction_var,
            values=["Английский → Русский", "Русский → Английский"],
            command=self.change_direction,
            width=130, height=34,
            font=("Arial", 13), corner_radius=8,
            fg_color=self._pal["field"], button_color=self._pal["panel_hover"],
            button_hover_color=self._pal["scrollbar_hover"], text_color=self._pal["text"],
            dropdown_fg_color=self._pal["panel"], dropdown_hover_color=self._pal["panel_hover"],
            dropdown_text_color=self._pal["text"],
        )
        self.direction_menu.pack(side="left", padx=(0, 8), pady=8)

        self.swap_btn = ctk.CTkButton(self.direction_frame, text="Сменить местами",
                                 command=self.swap_fields, width=140, height=34,
                                 font=("Arial", 13), corner_radius=8,
                                 fg_color=self._pal["panel_hover"], hover_color=self._pal["scrollbar_hover"],
                                 text_color=self._pal["text"])
        self.swap_btn.pack(side="left", padx=(0, 14), pady=8)

        # Кнопки управления (строка 4, правая часть): «Перевести» — основное
        # действие (акцентный цвет, жирный шрифт), «Копировать» и «Очистить»
        # — вторичные (нейтральная подложка).
        btn_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        btn_frame.grid(row=4, column=0, columnspan=2, pady=(10, 10), sticky="ew")

        self.translate_btn = ctk.CTkButton(btn_frame, text="Перевести",
                                           command=self.start_translation,
                                           state="disabled",
                                           width=160, height=40,
                                           font=("Arial", 15, "bold"),
                                           corner_radius=10,
                                           fg_color=self._pal["accent"], hover_color=self._pal["accent_hover"],
                                           text_color=self._pal["accent_text"])
        self.translate_btn.pack(side="right")

        self.copy_btn = ctk.CTkButton(btn_frame, text="Копировать перевод",
                                 command=self.copy_translation,
                                 width=160, height=40, font=("Arial", 14),
                                 corner_radius=10,
                                 fg_color=self._pal["panel"], hover_color=self._pal["panel_hover"],
                                 text_color=self._pal["text"])
        self.copy_btn.pack(side="right", padx=10)

        self.clear_btn = ctk.CTkButton(btn_frame, text="Очистить",
                                  command=self.clear_fields,
                                  width=110, height=40, font=("Arial", 14),
                                  corner_radius=10,
                                  fg_color=self._pal["panel"], hover_color=self._pal["panel_hover"],
                                  text_color=self._pal["muted"])
        self.clear_btn.pack(side="right", padx=10)

        # Строка статуса (строка 5, внизу, weight=0): растягивается только
        # по ширине. Создаётся здесь (master=main_frame) — виджет Tkinter
        # нельзя перенести в другой master.
        self.status_label = ctk.CTkLabel(main_frame, text="Инициализация нейросети...",
                                         font=("Arial", 12), text_color=self._pal["pending"])
        self.status_label.grid(row=5, column=0, columnspan=2, padx=10, pady=(0, 2), sticky="w")

        # Стартовое направление (из настроек): подписи полей и меню.
        self._apply_direction(self.direction)

    def start_translation(self, _event=None):
        """Ручной перевод: кнопка «Перевести», Ctrl+Enter, глобальный агент.

        Ожидающий debounce автоперевода отменяется (переводим сейчас);
        если перевод уже идёт — запрос уходит в коалесинг
        (_start_translation_internal), параллельных потоков перевода нет.
        """
        self._start_translation_internal("manual")

    def translate_thread(self, text, direction, generation):
        """Попредложенический перевод в фоновом потоке (ОДИН worker на
        generation — параллельных циклов перевода нет).

        Один общий pipeline для ручного/авто/агентского перевода (разница
        только в том, кто вызывает _start_translation_internal):
            for предложение: проверить generation → перевести чанки
            предложения → отправить событие в очередь → следующее.

        Tk-операции из этого потока НЕ выполняются — только проверка
        поколения и put в _gui_queue (применяет главный поток). Если
        generation устарела (очистка/смена направления/swap/изменение
        текста пользователем), worker завершается как можно раньше и не
        публикует устаревшие результаты.
        """
        stream = getattr(self.translator, "translate_stream", None)
        if stream is None:
            # Фолбэк: у переводчика нет инкрементального интерфейса —
            # переводим весь текст целиком (старый путь, без стрима).
            try:
                result = self.translator.translate(text, direction)
            except Exception as e:
                self._gui_queue.put(("translation_error", generation, str(e)))
                return
            self._gui_queue.put(("translation_done", generation, result))
            return

        def on_sentence(phase, done, total, unit):
            # Проверка поколения на каждом предложении: устаревший worker
            # прерывается (ранний выход) и ничего не публикует. Финальная
            # защита от устаревших событий — в _handle_message (главный
            # поток).
            if generation != self._translation_generation:
                raise _StaleTranslation()
            self._gui_queue.put(
                ("stream_sentence", generation, phase, done, total, unit))

        try:
            result = stream(text, direction, on_sentence)
        except _StaleTranslation:
            # Поколение устарело: сообщаем главному потоку, чтобы он
            # сбросил состояние и запустил коалесированный запрос.
            self._gui_queue.put(("stream_stale", generation))
            return
        except Exception as e:
            # Даже при неожиданной ошибке уведомляем главный поток:
            # он включит кнопку и покажет ошибку в статусе.
            self._gui_queue.put(("translation_error", generation, str(e)))
            return
        self._gui_queue.put(("translation_done", generation, result))

    def _process_gui_queue(self):
        """Опрос очереди в главном потоке: применяет сообщения фоновых потоков
        к виджетам (Tkinter позволяет менять GUI только из главного потока)."""
        try:
            while True:
                self._handle_message(self._gui_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._process_gui_queue)

    def _handle_message(self, message):
        """Обрабатывает одно сообщение от фонового потока (только главный поток)."""
        kind = message[0]

        if kind == "init_done":
            # Модели загружены: перевод доступен.
            self._model_loading = False
            self.translate_btn.configure(state="normal")
            # Если во время загрузки текста уже ввели — запланируем автоперевод.
            if self.settings.get("autotranslate"):
                self._schedule_autotranslate()
            # Пользователь всегда видит, какой моделью РЕАЛЬНО работает
            # приложение — при любом пути сюда: старт, смена модели,
            # фолбэк, смена направления (Этап 14).
            self._update_model_display()
            if self._pending_agent_text is not None:
                self._pending_agent_text = None
                self._start_translation_internal("agent")
            # Ready-статус: готовность + фактическая модель + заметка о
            # фолбэке, если была (_model_note потребляется
            # _ready_status_text). Недоступный глобальный хоткей —
            # предупреждение в том же статусе: сырая причина (traceback
            # импорта) не попадает в пользовательский статус — только в
            # лог и в окно «Настройки» (Этап 14).
            if self._agent_error:
                logger.warning("Глобальный хоткей недоступен: %s",
                               self._agent_error)
                status = ("%s Глобальный хоткей недоступен."
                          % self._ready_status_text())
            else:
                status = self._ready_status_text()
            self._set_status("ready", status)
        elif kind == "init_error":
            # Загрузка не удалась: понятная пользователю ошибка (сырое
            # исключение — в лог, в статус не попадает), кнопка остаётся
            # отключённой — включается только в init_done (Этап 14).
            self._model_loading = False
            logger.error("Ошибка загрузки модели: %s", message[1])
            self._set_status(
                "error",
                "Не удалось загрузить модель. Проверьте настройки модели "
                "и доступность необходимых файлов.")
        elif kind == "translation_done":
            generation, result = message[1], message[2]
            # Результат применяем только если операция ещё актуальна;
            # иначе пользователь уже изменил состояние (очистка/направление/swap)
            # и поля не трогаем.
            if generation == self._translation_generation:
                # Финальный результат — из translator (источник истины):
                # перекрывает вывод, накопленный стримом по предложениям,
                # поэтому итоговый текст всегда корректен. Выставление вида
                # под защитой синхронизации: сброс документа вывода до верха
                # (delete/insert) иначе через callback перетянул бы вид
                # исходного поля наверх.
                self._syncing_scroll = True
                try:
                    self.output_text.delete("1.0", "end")
                    self.output_text.insert("1.0", result)
                finally:
                    self._syncing_scroll = False
                # Замена текста стёрла теги подсветки — переставляем
                # активный юнит на новый текст (dst-диапазоны из
                # self._unit_map точны: результат совпадает с накопленным
                # стримом; _add_highlight_range на всякий случай зажимает
                # смещения в длину. Если пользователь вмешался, поколение
                # уже устарело и мы сюда не пришли).
                if self._hl_unit is not None:
                    self._highlight_unit(self._hl_unit)
                    # Оба поля подвести к последней паре (вид вывода после
                    # замены сбрасывается наверх; исходное остаётся на месте).
                    self._show_current_pair()
                # Стрим завершён. События done(N) и translation_done
                # приходят в одном батче очереди — подсветка последней
                # пары снималась бы «на лету» и так и не отрисовалась
                # (особенно заметно для текста из одного предложения).
                # Снимается гарантированно через 0.7 c, если до этого не
                # наступила новая операция (смена поколения —
                # коалесированный запрос, очистка, swap и т.п.).
                def _deferred_clear(gen=generation):
                    if gen == self._translation_generation:
                        self._clear_highlight()
                self.after(700, _deferred_clear)
            self._translation_busy = False
            self._active_text = None
            self.translate_btn.configure(state="normal")
            self._set_status("ready")
            if (generation == self._translation_generation
                    and self._notification_after_translation):
                self._notification_after_translation = False
                shown = show_notification(
                    "Перевод готов", result,
                    self.settings.get("notification_duration_sec", 5))
                if not shown:
                    logger.warning("Не удалось показать уведомление")
                    if self.state() == "withdrawn":
                        self._show_from_tray()
            # После завершения: коалесированный запрос (если текст не изменился).
            self._continue_pending_translation()
        elif kind == "translation_error":
            generation, error = message[1], message[2]
            self._translation_busy = False
            self._active_text = None
            self._notification_after_translation = False
            self.translate_btn.configure(state="normal")
            if generation == self._translation_generation:
                # Операция актуальна: подсветка снимается, уже переведённые
                # предложения остаются в поле (они верны), ошибка — в статусе
                # (traceback пользователю не показываем).
                self._clear_highlight()
                self._set_status("error", f"Ошибка перевода: {error}")
            else:
                # Операция устарела — просто восстанавливаем обычный статус.
                self._set_status("ready")
            self._continue_pending_translation()
        elif kind == "stream_sentence":
            # Событие попредложенического стрима от worker'а (фаза "start"
            # или "done"). Устаревшие события (после очистки/swap/смены
            # направления/изменения текста пользователем) отбрасываем:
            # старые результаты не возвращаются в поля.
            generation = message[1]
            if generation != self._translation_generation:
                return
            self._apply_stream_sentence(message[2], message[3], message[4],
                                        message[5])
        elif kind == "stream_stale":
            # Устаревший worker сообщил о раннем завершении: сбрасываем
            # состояние (если оно всё ещё этого поколения) и запускаем
            # коалесированный запрос, если пользователь уже изменил текст.
            generation = message[1]
            if generation == self._translation_generation:
                return
            self._translation_busy = False
            self._active_text = None
            self.translate_btn.configure(state="normal")
            self._clear_highlight()
            if self._pending_text is not None \
                    or self._auto_translate_after is not None:
                self._set_status("busy", "Ожидание перевода…")
            else:
                self._set_status("ready")
            self._continue_pending_translation()
        elif kind == "agent_request":
            # Глобальный агент хоткея (поток pynput) передал перехваченный текст.
            self._handle_agent_request(message[1])

    def copy_translation(self):
        text = self.output_text.get("1.0", "end-1c")
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._set_status("ready", "Скопировано в буфер обмена!")

    def clear_fields(self):
        # Ожидающий автоперевод отменяется (требование: «очистка отменяет
        # ожидающий автоперевод»). Программное удаление не порождает
        # <<Modified>> — самого действия очистки новый перевод не запускает.
        self._cancel_auto()
        self._pending_text = None
        self._notification_after_translation = False
        self.input_text.delete("1.0", "end")
        self.output_text.delete("1.0", "end")
        # Поля пусты — накопленное сопоставление юнитов недействительно.
        self._reset_sentence_mapping()
        # Запущенный перевод (если есть) больше не соответствует состоянию UI — помечаем его устаревшим.
        self._translation_generation += 1
        self._set_status("ready")

    def change_direction(self, value: str):
        """Сменяет направление перевода (меню) и обновляет подписи полей.

        Смена модели под новое направление (если нужна) — в _set_direction
        (Этап 10)."""
        # Старые подписи оставляем принимаемыми для совместимости с уже
        # открытыми окнами/автоматизированными сценариями после обновления.
        self._set_direction(
            "ru-en" if value in ("Русский → Английский", "RU → EN")
            else "en-ru")

    def swap_fields(self):
        """Меняет содержимое полей и направление перевода местами."""
        input_text = self.input_text.get("1.0", "end-1c")
        output_text = self.output_text.get("1.0", "end-1c")

        # Ожидающий автоперевод отменяем ДО изменения полей: после swap
        # запланируем ровно один новый (программный insert не порождает
        # <<Modified>>, поэтому рекурсивных/двойных событий нет).
        self._cancel_auto()
        self._pending_text = None
        self._notification_after_translation = False

        self.input_text.delete("1.0", "end")
        self.output_text.delete("1.0", "end")
        if output_text:
            self.input_text.insert("1.0", output_text)
        if input_text:
            self.output_text.insert("1.0", input_text)
        # Содержимое полей изменилось — накопленное сопоставление юнитов
        # (и подсветка) недействительно.
        self._reset_sentence_mapping()

        # Содержимое полей изменилось — запущенный перевод (если есть) устарел.
        self._translation_generation += 1

        # Смена направления — через model-aware путь (Этап 14): если текущая
        # модель не поддерживает новое направление, автоматически выбирается
        # совместимая (то же поведение, что у меню направления, _set_direction).
        # _apply_direction() один лишь меняет подписи — directional-модель
        # оставалась несовместимой с новым направлением, и перевод выдавал
        # ошибку.
        self._set_direction("ru-en" if self.direction == "en-ru" else "en-ru")

        # Прежний перевод оказался в исходном поле — один debounce-
        # автоперевод по новому направлению (если автоперевод включён).
        self._schedule_autotranslate()

    # ------------------------------------------------------------------ #
    #  Статус, тема, направление                                          #
    # ------------------------------------------------------------------ #
    def _set_status(self, kind: str, text: str = None):
        """Ставит статус с цветом текущей темы.

        kind: "ready" (зелёный), "busy" (оранжевый), "error" (красный).
        Текст сохраняется (self._status) и переотрисовывается при смене темы.
        """
        if text is None:
            text = READY_TEXT
        color = {"ready": self._pal["success"],
                 "busy": self._pal["pending"],
                 "error": self._pal["error"]}.get(kind, self._pal["pending"])
        self._status = (kind, text)
        self.status_label.configure(text=text, text_color=color)

    # ------------------------------------------------------------------ #
    #  Модель: отображение и применение (Этап 10)                        #
    # ------------------------------------------------------------------ #
    def _model_short_name(self) -> str:
        """Короткое отображаемое имя текущей (запущенной) модели:
        display name без происхождения в скобках."""
        descriptor = self.model_manager.get_model(self._active_model_id)
        return descriptor.name.split(" (")[0]

    def _ready_status_text(self) -> str:
        """Статус «готово» с именем модели (Этап 10): пользователь видит,
        какой моделью работает приложение. note (фолбэк/смена) — впереди."""
        try:
            text = "Готов к переводу! (Работает офлайн, модель: %s)" % \
                self._model_short_name()
        except ModelNotFoundError:
            text = READY_TEXT
        if self._model_note:
            note, self._model_note = self._model_note, None
            text = "%s. %s" % (note, text)
        return text

    def _update_model_display(self):
        """Подпись под заголовком: направление + текущая модель (Этап 10)."""
        dir_label = "Русский → Английский" if self.direction == "ru-en" else "Английский → Русский"
        try:
            model = self._model_short_name()
        except ModelNotFoundError:
            model = "—"
        self.header_subtitle.configure(
            text="%s · %s · работает офлайн" % (dir_label, model))

    def _set_direction(self, direction: str):
        """Программная смена направления (агент хоткея, настройки, меню).

        Этап 10 — направление учитывает модель: если текущая модель не
        поддерживает новое направление — автоматически выбирается дефолт
        направления (сохраняется в настройки) и переводчик (пере)загружается
        в фоне. Приложение никогда не «молча» остаётся на несовместимой
        модели: выбранная через model_id модель в неподдерживаемом
        направлении выдаёт явную ошибку, поэтому смена обязательна.
        """
        self._cancel_auto()
        self._pending_text = None
        self._notification_after_translation = False
        self._translation_generation += 1
        self._reset_sentence_mapping()
        self._apply_direction(direction)
        run_id, persist_id, note = self.model_manager.resolve_runtime(
            self.model_id, direction)
        if persist_id is not None:
            # Сохранённая модель не подходит под новое направление.
            old_name = None
            try:
                old_name = self.model_manager.get_model(self.model_id).name
            except ModelNotFoundError:
                pass
            new_name = self.model_manager.get_model(persist_id).name
            self.model_id = persist_id
            self.settings.set("model_id", persist_id)
            self.settings.save()
            note = ("Модель %s не поддерживает направление %s — использую %s"
                    % (old_name or "—",
                       "Русский → Английский" if direction == "ru-en" else "Английский → Русский",
                       new_name))
        self._active_model_id = run_id
        self._model_note = note
        self._update_model_display()
        if run_id != self._translator_model_id:
            self._start_model_load()

    def _set_model(self, model_id: str):
        """Явная смена модели пользователем (диалог «Настройки», Этап 10).

        Минимальный безопасный механизм: та же фоновая (пере)загрузка, что
        при старте (_start_model_load); идущий перевод помечается устаревшим
        (generation). Выбор сохраняется в настройки (model_id) — переживает
        перезапуск. Недоступная модель (напр. отсутствует локальный файл
        модели) не падает: resolve_runtime подставляет доступный дефолт
        с понятным
        сообщением в статусе, приложение остаётся работоспособным."""
        if model_id == self._active_model_id and \
                model_id == self.model_id:
            return
        self._cancel_auto()
        self._pending_text = None
        self._translation_generation += 1
        self._reset_sentence_mapping()
        run_id, persist_id, note = self.model_manager.resolve_runtime(
            model_id, self.direction)
        self.model_id = persist_id if persist_id is not None else model_id
        self.settings.set("model_id", self.model_id)
        self.settings.save()
        self._active_model_id = run_id
        self._model_note = note
        self._update_model_display()
        if run_id != self._translator_model_id:
            self._start_model_load()

    # ------------------------------------------------------------------ #
    #  Подсветка пары «предложение ↔ его перевод» (Этап 4, Этап 10)     #
    #  Модель Этапа 10: ОДНА активная пара (unit), hover в обоих       #
    #  направлениях, сопоставление по смещениям пайплайна (по тексту  #
    #  — нет).                                                         #
    # ------------------------------------------------------------------ #
    def _setup_highlight_tags(self):
        """Настраивает теги подсветки в обоих полях (мягкий фон палитры).

        Таги ставим на внутренний tkinter.Text (_textbox): это штатный
        механизм подсветки диапазонов, который не влияет на редактирование,
        копирование, Ctrl+A/Ctrl+C/Ctrl+X (меняет только фон диапазона).
        """
        for box, tag in ((self.input_text._textbox, self._hl_src_tag),
                         (self.output_text._textbox, self._hl_dst_tag)):
            box.tag_configure(tag, background=self._pal["hl"])

    def _setup_hover_sync(self):
        """Hover/клик в любом поле подсвечивает пару в обоих полях (Этап 10).

        Биндим на внутренний Text (_textbox): именно он получает события
        мыши. Обработчики не возвращают "break" — клики, курсор и
        выделение работают штатно, подсветка — «побочный» эффект того же
        события. Все действия — в главном потоке (Tk-события приходят
        сюда).
        """
        for box, pane in ((self.input_text, "src"),
                          (self.output_text, "dst")):
            tb = box._textbox
            tb.bind("<Motion>", lambda e, p=pane: self._on_hover_move(p, e),
                    add="+")
            tb.bind("<Button-1>", lambda e, p=pane: self._on_hover_move(p, e),
                    add="+")
            tb.bind("<Leave>", lambda e, p=pane: self._on_hover_leave(p, e),
                    add="+")

    def _highlight_unit(self, unit_idx):
        """Подсвечивает пару юнита unit_idx в обоих полях (активный unit).

        Диапазоны — из self._unit_map (смещения пайплайна). Вызывать
        только из главного потока. unit_idx вне диапазона — как
        _clear_pair(). Старая подсветка снимается до постановки новой.
        """
        in_tb = self.input_text._textbox
        out_tb = self.output_text._textbox
        in_tb.tag_remove(self._hl_src_tag, "1.0", "end")
        out_tb.tag_remove(self._hl_dst_tag, "1.0", "end")
        if unit_idx is None or not (0 <= unit_idx < len(self._unit_map)):
            self._hl_unit = None
            return
        src_start, src_end, dst_start, dst_end = self._unit_map[unit_idx]
        self._hl_unit = unit_idx
        self._add_highlight_range(in_tb, self._hl_src_tag, src_start, src_end)
        self._add_highlight_range(out_tb, self._hl_dst_tag, dst_start, dst_end)

    def _add_highlight_range(self, text_widget, tag, start, end):
        """Добавляет диапазон подсветки, зажимая смещения в длину текста:
        устаревший диапазон (пользователь поправил поле во время перевода)
        не должен порождать TclError."""
        if start is None or end is None:
            return
        try:
            text = text_widget.get("1.0", "end-1c")
            s = max(0, min(int(start), len(text)))
            e = max(0, min(int(end), len(text)))
            if e > s:
                text_widget.tag_add(tag, off_to_tk(text, s), off_to_tk(text, e))
        except tk.TclError:
            pass

    def _clear_pair(self):
        """Снимает подсветку активного юнита из обоих полей (само состояние
        _hl_unit сбрасывается). Вызывать только из главного потока."""
        for box, tag in ((self.input_text._textbox, self._hl_src_tag),
                         (self.output_text._textbox, self._hl_dst_tag)):
            try:
                box.tag_remove(tag, "1.0", "end")
            except tk.TclError:
                pass
        self._hl_unit = None

    def _clear_highlight(self):
        """Снимает подсветку пары (alias для _clear_pair; имя сохранено
        для существующих мест вызова). Mapping юнитов при этом НЕ
        сбрасывается: после завершения перевода hover продолжает
        сопоставлять предложения по накопленным смещениям."""
        self._clear_pair()

    def _reset_sentence_mapping(self):
        """Полный сброс сопоставления «предложение ↔ перевод» и подсветки.

        Вызывается при операциях, делающих накопленные смещения
        недействительными: очистка полей, swap, смена направления, смена
        модели, изменение исходного текста, начало нового стрима.
        """
        self._unit_map = []
        self._src_starts = []
        self._dst_starts = []
        self._hl_unit = None
        self._stream_unit = None
        self._clear_pair()

    # -- hover-механика (Этап 10) --------------------------------------- #
    def _hover_cache_line_offsets(self, tb):
        """Начала строк текста поля с кэшем: пересчёт только при изменении
        длины текста (для long text <Motion> не должен сканировать весь
        текст на каждое движение мыши)."""
        text = tb.get("1.0", "end-1c")
        cached = self._hover_cache.get(id(tb))
        if cached is None or cached[0] != len(text):
            cached = (len(text), line_offsets(text))
            self._hover_cache[id(tb)] = cached
        return cached[1]

    def _find_unit_at(self, pane: str, off: int):
        """Индекс юнита, чей диапазон (pane "src"/"dst") содержит
        символьное смещение off; вне предложений — None.

        Только смещения пайплайна (bisect по отсортированным началам) —
        сопоставление по тексту намеренно не используется.
        """
        starts = self._src_starts if pane == "src" else self._dst_starts
        if not starts:
            return None
        i = bisect.bisect_right(starts, off) - 1
        if i < 0:
            return None
        entry = self._unit_map[i]
        lo, hi = (entry[0], entry[1]) if pane == "src" else (entry[2], entry[3])
        if lo <= off < hi:
            return i
        return None

    def _on_hover_move(self, pane: str, event):
        """Мышь двигается/клик в поле: подсвечиваем пару под курсором
        (в обоих полях — и своё, и поле-партнёр)."""
        tb = event.widget
        try:
            # str(): index() может вернуть Tcl_Obj (зависит от версии
            # tkinter), а не str.
            idx = str(tb.index("@%d,%d" % (event.x, event.y)))
            line_offs = self._hover_cache_line_offsets(tb)
            line_s, col_s = idx.split(".", 1)
            off = line_offs[int(line_s) - 1] + int(col_s)
        except (tk.TclError, ValueError, IndexError):
            return
        unit = self._find_unit_at(pane, off)
        if unit is None:
            # Мышь в «зазоре» между предложениями (разделители не входят
            # ни в один юнит) — пары под курсором нет.
            self._hover_restore()
        elif unit != self._hl_unit:
            self._highlight_unit(unit)

    def _on_hover_leave(self, pane: str, event):
        """Мышь ушла из поля: пара под курсором больше нет."""
        self._hover_restore()

    def _hover_restore(self):
        """Состояние после «мыши вне предложений»: во время перевода —
        назад на последний завершённый стрим-юнит (подсветка стрима
        восстанавливается), иначе — подсветка снимается."""
        if self._translation_busy and self._stream_unit is not None:
            self._highlight_unit(self._stream_unit)
        else:
            self._clear_pair()

    # ------------------------------------------------------------------ #
    #  Синхронная прокрутка исходного/выводного полей (Этап 5)            #
    # ------------------------------------------------------------------ #
    def _setup_scroll_sync(self):
        """Синхронная прокрутка полей ввода/вывода (Этап 5).

        Любое изменение видимого окна ОДНОГО поля (mouse wheel, клавиши
        Page Up/Down, перетаскивание, скроллбар, программное see()/yview_*)
        выставляет в ДРУГОМ поле ту же ОТНОСИТЕЛЬНУЮ позицию документа
        (yview-фракцию): 0.42 -> ~0.42, верх -> верх, низ -> низ. Количество
        строк/высота содержимого полей может отличаться — поэтому
        синхронизация по позиции, а не по номеру строки.

        Реализовано обёрткой над штатным yscrollcommand: Tk вызывает его при
        ЛЮБОМ изменении видимого окна поля (это покрывает wheel, клавиши,
        перетаскивание скроллбара и программную прокрутку одним механизмом),
        и обёртка продолжает обновлять собственный скроллбар поля
        (оригинальный _y_scrollbar.set).

        Защита от бесконечного callback loop: пока код программно выставляет
        вид партнёра, _syncing_scroll=True и callback партнёра не запускает
        обратную синхронизацию.
        """
        in_box, out_box = self.input_text, self.output_text
        in_box._textbox.configure(
            yscrollcommand=self._make_sync_view_command(in_box,
                                                        out_box._textbox))
        out_box._textbox.configure(
            yscrollcommand=self._make_sync_view_command(out_box,
                                                        in_box._textbox))
        # «Пользователь прокрутил вручную» -> _auto_follow=False: во время
        # перевода перестаем подсовывать текущую пару в видимую область
        # (поля при этом остаются синхронизированы друг с другом).
        for box in (in_box, out_box):
            tb = box._textbox
            # <B1-Motion> — перетаскивание: Text сам прокручивает вид,
            # когда курсор уводит выделение за край видимой области.
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>",
                        "<B1-Motion>", "<Prior>", "<Next>"):
                tb.bind(seq, self._on_user_scroll, add="+")
            # Скроллбар CTkTextbox — canvas: клик по треку и перетаскивание
            # бегунка.
            sb_canvas = box._y_scrollbar._canvas
            for seq in ("<Button-1>", "<B1-Motion>"):
                sb_canvas.bind(seq, self._on_user_scroll, add="+")
        # Hover/клик в любом поле подсвечивает пару в обоих полях (Этап 10).
        self._setup_hover_sync()

    def _make_sync_view_command(self, box, partner_tb):
        """yscrollcommand одного поля: обновить собственный скроллбар и
        (если не идёт программная синхронизация) синхронизировать вид
        поля-партнёра."""
        def on_view(first, last):
            try:
                box._y_scrollbar.set(first, last)  # бегунок этого поля
            except tk.TclError:
                pass
            self._sync_partner_view(partner_tb, first, last)
        return on_view

    def _sync_partner_view(self, partner_tb, first, last):
        """Выставляет в поле-партнёре ту же относительную позицию
        документа, что и в исходном поле.

        Всегда через yview_moveto() (фикс Этапа 10): see() вызывает
        yscrollcommand АСИНХРОННО — после перерисовки, когда защита
        _syncing_scroll уже снята; это порождало обратную синхронизацию
        (feedback loop) и «просадку» синхронизации при прокрутке вверх.
        yview_moveto() вызывает yscrollcommand партнёра СИНХРОННО, пока
        защита активна — обратная синхронизация гарантированно
        заблокирована, без зависимости от таймингов callback'ов.
        Верх — доля 0.0; низ — доля, близкая к 1 (yview_moveto зажимает
        значение в максимально прокручиваемое — ровно низ документа).
        """
        if self._syncing_scroll:
            return
        try:
            f = float(first)
            l = float(last)
        except (TypeError, ValueError):
            return
        if f < 0.0 or l < f:
            return
        if f <= 5e-4:
            target = 0.0            # поле вверху -> партнёр вверху
        elif l >= 1.0 - 5e-4:
            # Поле внизу: yview last — доля ДОКУМЕНТА ВЫШЕ НИЗА окна
            # (last >= first; f+l >= 1 в середине — НОРМА, проверять
            # нельзя), поэтому низ определяется по last == 1.
            target = 1.0 - 5e-4     # поле внизу -> партнёр внизу
        else:
            # Середина документа: та же доля (относительная позиция).
            target = f
        self._syncing_scroll = True
        try:
            partner_tb.yview_moveto(target)
        except tk.TclError:
            pass
        finally:
            self._syncing_scroll = False

    def _on_user_scroll(self, _event=None):
        """Пользователь прокрутил вручную (wheel/клавиши/скроллбар):
        не отбирать у него управление — автопозиционирование пары отключено
        до следующего запуска перевода."""
        self._auto_follow = False

    def _show_current_pair(self):
        """Автопоказ: подводит текущую стрим-пару в видимую область обоих
        полей (только во время перевода, если пользователь не прокрутил сам).

        Подводим СТРИМ-юнит (прогресс перевода), а не любой активный
        _hl_unit: hover — инициатива пользователя, автопоказу не следует
        уводить вид от обработываемого предложения.
        """
        if not self._auto_follow or self._stream_unit is None:
            return
        src_start, _src_end, dst_start, _dst_end = \
            self._unit_map[self._stream_unit]
        in_tb = self.input_text._textbox
        out_tb = self.output_text._textbox
        in_text = in_tb.get("1.0", "end-1c")
        out_text = out_tb.get("1.0", "end-1c")
        # Оба вида выставляем под общей защитой: их yscrollcommand'ы
        # сработают, но обратную синхронизацию не запустят.
        self._syncing_scroll = True
        try:
            in_tb.see(off_to_tk(in_text, src_start))
            out_tb.see(off_to_tk(out_text, dst_start))
        except tk.TclError:
            pass
        finally:
            self._syncing_scroll = False

    def _apply_stream_sentence(self, phase: str, done: int, total: int, unit):
        """Применяет событие стрима к полям и подсветке (только главный
        поток, через очередь).

        Семантика подсветки (Этап 5): ОДНО логическое предложение = ОДНА
        единица синхронизации UI. В любой момент подсвечена ровно одна
        ЦЕЛАЯ пара «предложение k ↔ перевод k» — последняя завершённая;
        сколько бы технических chunks (уровень B) ни было внутри
        предложения, переход пары происходит ровно один раз — на событие
        "done" этого юнита. «Крестовая» пара (предложение N, перевод N-1)
        не показывается никогда:

          start(N) — идёт обработка N-го юнита; остаётся подсвеченной
                     пара (N-1, N-1) — последняя завершённая единица.
          done(N)  — перевод N-го готов: дописываем его в поле вывода,
                     пара атомарно продвигается на (N, N) и подводится в
                     видимую область (если пользователь не прокрутил).
        """
        if phase == "start":
            # Обработка следующего юнита началась: предыдущая завершённая
            # пара остаётся текущей (согласованная единица (k, k)).
            if total > 1:
                # Показываем номер ТЕКУЩЕГО юнита (done+1), а не количество
                # уже завершённых: иначе первый юнит отображал бы «0/N»
                # (Этап 14).
                self._set_status("busy", f"Перевод… ({done + 1}/{total})")
            return

        # phase == "done": дописываем перевод юнита в поле вывода.
        out_tb = self.output_text._textbox
        existing = out_tb.get("1.0", "end-1c")
        if existing:
            sep = "\n\n" if unit.new_paragraph else " "
            out_tb.insert("end-1c", sep + unit.translation)
            dst_start = len(existing) + len(sep)
        else:
            out_tb.insert("end-1c", unit.translation)
            dst_start = 0
        dst_end = dst_start + len(unit.translation)
        # Пара продвигается как единое целое: (предложение N, перевод N).
        # src-смещения — из StreamUnit (пайплайн), dst-смещения — из
        # фактически вставленного текста: соответствие original N <->
        # translated N точное и используется и для подсветки, и для
        # hover-сопоставления (Этап 10) — сопоставление по тексту нигде
        # не применяется.
        self._unit_map.append((unit.src_start, unit.src_end,
                               dst_start, dst_end))
        self._src_starts.append(unit.src_start)
        self._dst_starts.append(dst_start)
        self._stream_unit = len(self._unit_map) - 1
        self._highlight_unit(self._stream_unit)
        self._show_current_pair()
        if total > 1:
            self._set_status("busy", f"Переведено {done}/{total} предложений…")

    def _apply_direction(self, direction: str):
        """Устанавливает направление и обновляет меню с подписями полей."""
        self.direction = direction
        if direction == "ru-en":
            self.direction_var.set("Русский → Английский")
            self.input_label.configure(text="Исходный текст (Русский)")
            self.output_label.configure(text="Перевод (Английский)")
        else:
            self.direction_var.set("Английский → Русский")
            self.input_label.configure(text="Исходный текст (Английский)")
            self.output_label.configure(text="Перевод (Русский)")

    def _set_theme(self, theme: str):
        """Применяет цветовую тему к основному окну (без перезапуска)."""
        pal = PALETTES.get(theme, PALETTES["dark"])
        self._pal = dict(pal)
        self.configure(fg_color=pal["bg"])
        # Фон подсветки предложений — из палитры темы.
        self._setup_highlight_tags()
        for box in (self.input_text, self.output_text):
            box.configure(fg_color=pal["field"],
                          text_color=pal["text"],
                          scrollbar_button_color=pal["scrollbar"],
                          scrollbar_button_hover_color=pal["scrollbar_hover"])
        self.header_title.configure(text_color=pal["text"])
        self.header_subtitle.configure(text_color=pal["muted"])
        self.input_label.configure(text_color=pal["text"])
        self.output_label.configure(text_color=pal["accent"])
        self.direction_frame.configure(fg_color=pal["panel"])
        self.direction_caption.configure(text_color=pal["text"])
        self.direction_menu.configure(fg_color=pal["field"],
                                      button_color=pal["panel_hover"],
                                      button_hover_color=pal["scrollbar_hover"],
                                      text_color=pal["text"],
                                      dropdown_fg_color=pal["panel"],
                                      dropdown_hover_color=pal["panel_hover"],
                                      dropdown_text_color=pal["text"])
        self.swap_btn.configure(fg_color=pal["panel_hover"],
                                hover_color=pal["scrollbar_hover"],
                                text_color=pal["text"])
        self.copy_btn.configure(fg_color=pal["panel"], hover_color=pal["panel_hover"],
                                text_color=pal["text"])
        self.clear_btn.configure(fg_color=pal["panel"], hover_color=pal["panel_hover"],
                                 text_color=pal["muted"])
        self.settings_btn.configure(fg_color=pal["panel"], hover_color=pal["panel_hover"],
                                    text_color=pal["text"])
        self.translate_btn.configure(fg_color=pal["accent"],
                                     hover_color=pal["accent_hover"],
                                     text_color=pal["accent_text"])
        # Текущий статус перекрашиваем в новую палитру.
        self._set_status(self._status[0], self._status[1])

    # ------------------------------------------------------------------ #
    #  Горячие клавиши окна (перенос из старой версии)                    #
    # ------------------------------------------------------------------ #
    def _bind_hotkeys(self):
        # Один обработчик получает физический keycode, а не keysym текущей
        # раскладки. Это важно, например, для Ctrl+V в русской раскладке.
        self.bind_all("<KeyPress>", self._on_physical_hotkey, add="+")
        # Clipboard-команды должны быть на widget bind-tag: class binding Tk
        # обрабатывает Ctrl+V раньше bind_all и иначе вставил бы текст дважды.
        for box in (self.input_text, self.output_text):
            box._textbox.bind("<KeyPress>", self._on_physical_hotkey,
                              add="+")
        # Escape на главном окне: закрыть окно настроек (если открыто).
        self.bind("<Escape>", self._on_hotkey_escape)

    def _on_physical_hotkey(self, event):
        """Обрабатывает системные сочетания по физическим keycode."""
        key = _physical_key_name(event)
        if key is None:
            return None
        ctrl = bool(event.state & _CONTROL_MASK)
        if not ctrl:
            if key == "escape":
                return self._on_hotkey_escape(event)
            return None
        if key == "return":
            return self._on_hotkey_translate(event)
        if key == "l":
            return self._on_hotkey_clear(event)
        if key == "comma":
            return self._on_hotkey_settings(event)
        widget = event.widget
        if key == "a":
            return self._select_all_physical(widget)
        if key == "c":
            return self._copy_physical(widget)
        if key == "v":
            return self._paste_physical(widget)
        if key == "x":
            return self._cut_physical(widget)
        if key == "z":
            return self._undo_physical(widget)
        return None

    @staticmethod
    def _text_widget(widget):
        return isinstance(widget, (tk.Text, tk.Entry))

    def _select_all_physical(self, widget):
        if not self._text_widget(widget):
            return None
        if isinstance(widget, tk.Text):
            widget.tag_add("sel", "1.0", "end")
        else:
            widget.selection_range(0, "end")
        return "break"

    def _selected_text(self, widget):
        try:
            if isinstance(widget, tk.Text):
                ranges = widget.tag_ranges("sel")
                return widget.get(*ranges) if ranges else ""
            if isinstance(widget, tk.Entry) and widget.selection_present():
                return widget.selection_get()
        except tk.TclError:
            return ""
        return ""

    def _copy_physical(self, widget):
        if not self._text_widget(widget):
            return None
        if widget is self.output_text._textbox:
            self.copy_translation()
        else:
            selected = self._selected_text(widget)
            if selected:
                self.clipboard_clear()
                self.clipboard_append(selected)
        return "break"

    def _paste_physical(self, widget):
        if not self._text_widget(widget):
            return None
        try:
            value = self.clipboard_get()
            if isinstance(widget, tk.Text):
                ranges = widget.tag_ranges("sel")
                if ranges:
                    widget.delete(*ranges)
                widget.insert("insert", value)
            else:
                if widget.selection_present():
                    widget.delete("sel.first", "sel.last")
                widget.insert("insert", value)
        except tk.TclError:
            pass
        return "break"

    def _cut_physical(self, widget):
        if not self._text_widget(widget):
            return None
        selected = self._selected_text(widget)
        if selected:
            self.clipboard_clear()
            self.clipboard_append(selected)
            try:
                widget.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
        return "break"

    def _undo_physical(self, widget):
        """Отменяет последнее изменение в активном текстовом поле."""
        if not self._text_widget(widget):
            return None
        try:
            widget.edit_undo()
        except tk.TclError:
            # Пустая история отмены — обычная ситуация, не ошибка для UI.
            pass
        return "break"

    def _on_hotkey_translate(self, _event):
        """Ctrl+Enter: немедленный перевод, без ожидания дебаунса."""
        self.start_translation()
        # "break": не вставляем перенос строки в поле ввода.
        return "break"

    def _on_hotkey_clear(self, _event):
        """Ctrl+L: очистка полей (тот же код, что кнопка «Очистить»)."""
        self.clear_fields()
        # "break": не вставляем букву 'l' в поле ввода.
        return "break"

    def _on_hotkey_settings(self, _event):
        """Ctrl+, — открыть окно настроек."""
        self._open_settings()
        return "break"

    def _on_hotkey_escape(self, _event):
        """Escape: закрыть окно настроек (если открыто)."""
        dialog = self._settings_dialog
        if dialog is not None and dialog.winfo_exists():
            dialog._on_close()
            return "break"
        return None

    def _on_select_all(self, event):
        """Ctrl+A: выделить всё в том поле, по которому нажата комбинация."""
        widget = event.widget
        if widget in (self.input_text._textbox, self.output_text._textbox):
            widget.tag_add("sel", "1.0", "end")
        return "break"

    def _on_hotkey_copy(self, event):
        """Ctrl+C в поле перевода — копирует весь перевод (как в старой версии)."""
        if event.widget is self.output_text._textbox:
            self.copy_translation()
            return "break"
        return None

    # ------------------------------------------------------------------ #
    #  Автоперевод (debounce) + single-flight перевод                     #
    # ------------------------------------------------------------------ #
    def _on_input_modified(self, _event=None):
        """<<Modified>> в исходном поле: debounce-автоперевод.

        Каждое новое изменение отменяет предыдущий таймер
        (_schedule_autotranslate всегда запускает таймер заново) —
        перевод «на каждую букву» не запускается.
        """
        try:
            # Сбрасываем modified-флаг, чтобы событие не повторялось
            # после программных изменений.
            self.input_text.edit_modified(False)
        except tk.TclError:
            pass
        if not self.input_text.get("1.0", "end-1c").strip():
            # Пустой исходный текст (пользователь удалил всё): текущий
            # перевод останавливаем (поколение устарело — старый worker
            # ничего не опубликует), вывод очищаем, подсветку снимаем,
            # ожидающий автоперевод и pending-запросы отменяем, статус
            # «Готово». Ничего не запускаем — независимо от настройки
            # autotranslate.
            self._cancel_auto()
            self._pending_text = None
            self._translation_generation += 1
            self.output_text.delete("1.0", "end")
            # Исходный текст пуст — накопленное сопоставление недействительно.
            self._reset_sentence_mapping()
            self._set_status("ready")
            return
        if self._translation_busy:
            # Пользователь изменил исходный текст: идущий перевод сделан
            # для старого текста — признаём его устаревшим (существующая
            # система _translation_generation, а не вторая конкурирующая):
            # worker сам завершится на следующем предложении и не публикует
            # результаты, а новый текст будет переведён через debounce /
            # коалесинг — тем же общим pipeline.
            # Сравниваем с _active_text: <<Modified>> от ПРОГРАММНЫХ
            # insert/delete доставляется асинхронно (после следующего
            # app.update()) — если текст совпадает с идущим переводом,
            # это не пользовательская правка и инвалидировать не нужно.
            current = self.input_text.get("1.0", "end-1c")
            if self._active_text is None or current != self._active_text:
                self._translation_generation += 1
                # Смещения в исходном тексте изменились — mapping сбрасывается.
                self._reset_sentence_mapping()
        self._schedule_autotranslate()

    def _schedule_autotranslate(self):
        """(Пере)запускает debounce-таймер автоперевода для текущего текста."""
        self._cancel_auto()
        if not self.settings.get("autotranslate") or self.translator is None:
            return
        text = self.input_text.get("1.0", "end-1c")
        if not text.strip():
            # Пустой исходный текст: текущий перевод останавливаем
            # (поколение становится устаревшим — старый worker ничего не
            # опубликует), вывод очищаем, подсветку снимаем, pending-
            # запросы отменяем, статус «Готово». Ничего не запускаем.
            self._pending_text = None
            self._translation_generation += 1
            self.output_text.delete("1.0", "end")
            # Исходный текст пуст — накопленное сопоставление недействительно.
            self._reset_sentence_mapping()
            self._set_status("ready")
            return
        limit = int(self.settings.get("max_text_length") or 0)
        if limit and len(text) > limit:
            self._set_status("error",
                             f"Текст слишком длинный: {len(text)} > {limit} символов (лимит в «Настройках»)")
            return
        self._set_status("busy", "Ожидание перевода…")
        debounce = float(self.settings.get("debounce_sec"))
        self._auto_translate_after = self.after(int(debounce * 1000),
                                                self._on_auto_translate_due)

    def _on_auto_translate_due(self):
        """Debounce истёк (пользователь перестал печатать) — переводим."""
        self._auto_translate_after = None
        self._start_translation_internal("auto")

    def _cancel_auto(self):
        """Отменяет ожидающий автоперевод и таймер статуса «Перевод...»."""
        for attr in ("_auto_translate_after", "_translating_status_after"):
            timer = getattr(self, attr, None)
            if timer is not None:
                try:
                    self.after_cancel(timer)
                except (tk.TclError, ValueError):
                    pass
                setattr(self, attr, None)

    def _show_translating_status(self):
        """Перевод (авто) длится дольше slow_after_sec — показываем «Перевод...»."""
        self._translating_status_after = None
        if self._translation_busy:
            self._set_status("busy", "Перевод...")

    def _start_translation_internal(self, source: str):
        """Единая точка запуска перевода (auto / manual / agent / rerun).

        - single-flight: пока идёт перевод, новый параллельный запуск НЕ
          происходит; новое требование сохраняется как последнее
          (_pending_text — коалесинг, новое перекрывает старое);
        - устаревшие результаты защищены _translation_generation.
        """
        self._cancel_auto()
        text = self.input_text.get("1.0", "end-1c")
        if not text.strip():
            return
        limit = int(self.settings.get("max_text_length") or 0)
        if limit and len(text) > limit:
            self._set_status("error",
                             f"Текст слишком длинный: {len(text)} > {limit} символов (лимит в «Настройках»)")
            return
        if self.translator is None:
            self._set_status("busy", "Ожидание загрузки моделей...")
            return
        if self._translation_busy:
            # Идёт перевод того же текста — повторять нет смысла
            # (двух одновременных переводов одного текста не бывает).
            if text == self._active_text:
                return
            # Идёт другой перевод: помним самый свежий текст и переведём
            # его сразу после завершения текущего (_continue_pending_translation).
            self._pending_text = text
            self._set_status("busy", "Ожидание перевода…")
            return
        self._translation_busy = True
        self._active_text = text
        # Новый запуск перевода: снова показываем текущую пару в видимой
        # области (если пользователь прокрутил вручную, _auto_follow был
        # отключён — до этого момента управление видом у него).
        self._auto_follow = True
        self.translate_btn.configure(state="disabled")
        if source == "auto":
            # Автоперевод: статус «Перевод...» показываем только если перевод
            # длится дольше slow_after_sec — для быстрых не мелькает.
            self._set_status("busy", "Ожидание перевода…")
            slow = float(self.settings.get("slow_after_sec"))
            if slow > 0:
                self._translating_status_after = self.after(int(slow * 1000),
                                                            self._show_translating_status)
        else:
            self._set_status("busy", "Перевод...")
        # Фиксируем направление до запуска потока (защита от гонки с self.direction)
        direction = self.direction
        # Инкрементальный вывод (Этап 4): если у переводчика есть
        # попредложенический интерфейс, очищаем поле вывода ДО стрима —
        # перевод будет появляться по предложениям (старое содержимое и
        # подсветка не должны смешиваться с новым стримом).
        if getattr(self.translator, "translate_stream", None) is not None:
            self.output_text.delete("1.0", "end")
            # Новый стрим — mapping строится заново (старые смещения
            # относятся к предыдущему переводу).
            self._reset_sentence_mapping()
        # Отмечаем начало новой операции: если до завершения потока пользователь
        # изменит состояние UI (очистка/направление/swap), результат не будет применён.
        self._translation_generation += 1
        generation = self._translation_generation
        # Перевод в отдельном потоке, чтобы GUI не фризил
        threading.Thread(target=self.translate_thread,
                         args=(text, direction, generation), daemon=True).start()

    def _continue_pending_translation(self):
        """После завершения перевода: если был коалесированный запрос и текст
        с тех пор не изменился — запускаем перевод последнего запрошенного."""
        pending = self._pending_text
        self._pending_text = None
        if pending is None or self.translator is None:
            return
        if self.input_text.get("1.0", "end-1c").strip() == pending.strip():
            self._start_translation_internal("auto")

    # ------------------------------------------------------------------ #
    #  Глобальный хоткей (агент) и окно настроек                          #
    # ------------------------------------------------------------------ #
    def _start_hotkey_agent(self) -> bool:
        """(Пере)запускает агента глобального хоткея. True — запущен.

        Агент опционален: без pynput/backend он просто не стартует,
        приложение работает как обычно (о причинах сообщает «Настройки»).
        """
        self._stop_hotkey_agent()
        self._hotkey_agent = HotkeyAgent(
            on_trigger=lambda text: self._gui_queue.put(("agent_request", text))
        )
        ok = self._hotkey_agent.start(self.settings.get("hotkey"))
        if not ok:
            self._agent_error = self._hotkey_agent.error
            # Приложение уже работает (агент перезапускается из настроек) —
            # предупреждение показываем и в статусе.
            if self.translator is not None:
                self._set_status("error", f"Глобальный хоткей: {self._agent_error}")
        else:
            self._agent_error = None
        return ok

    def _stop_hotkey_agent(self):
        if self._hotkey_agent is not None:
            self._hotkey_agent.stop()
            self._hotkey_agent = None

    def _handle_agent_request(self, text: str):
        """Нажат глобальный хоткей: перехваченный текст ставим в исходное
        поле, при необходимости переопределяем направление и переводим."""
        limit = int(self.settings.get("max_text_length") or 0)
        if limit and len(text) > limit:
            text = text[:limit].strip()
        if not text:
            return
        # «Фильтр кириллицы» (настройка из старой версии): направление
        # определяется автоматически по написанию перехваченного текста.
        if self.settings.get("filter_cyrillic"):
            has_cyrillic = bool(re.search(r"[\u0400-\u04ff]", text))
            if has_cyrillic and self.direction != "ru-en":
                self._set_direction("ru-en")
            elif not has_cyrillic and self.direction != "en-ru":
                self._set_direction("en-ru")
        self._cancel_auto()
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", text)
        # Переводим сразу (без debounce), а результат после завершения
        # показываем системным уведомлением — окно может оставаться в трее.
        self._notification_after_translation = True
        if self.translator is None:
            # Смена направления могла запустить загрузку другой модели.
            # Запрос будет выполнен после init_done.
            self._pending_agent_text = text
        else:
            self.start_translation()

    def _drop_topmost(self):
        try:
            self.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _open_settings(self):
        """Открывает окно «Настройки» (одно окно за раз)."""
        dialog = self._settings_dialog
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
            dialog.focus()
            return
        self._settings_dialog = SettingsDialog(self)

    def _on_window_close(self):
        if self._tray.active:
            self._cancel_auto()
            self.withdraw()
            return
        self._quit_app()

    def _show_from_tray(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _quit_app(self):
        self._stop_hotkey_agent()
        self._tray.stop()
        self.destroy()


# =====================================================================
# Окно «Настройки» (в стилистике основного окна)
# =====================================================================

# Языки/тема в интерфейсе — по-русски, в настройках хранятся внутренние коды.
# Текущая архитектура поддерживает только EN/RU (модели Helsinki-NLP opus-mt).
_LANG_LABELS = {"en": "Английский", "ru": "Русский"}
_LANG_FROM_LABEL = {
    **{v: k for k, v in _LANG_LABELS.items()},
    # Совместимость с ранее сохранёнными/созданными UI-сценариями.
    "Английский (EN)": "en",
    "Русский (RU)": "ru",
}
_THEME_LABELS = {"dark": "Тёмная", "light": "Светлая"}
_THEME_FROM_LABEL = {v: k for k, v in _THEME_LABELS.items()}

# Имена настроек для сообщений об ошибках валидации.
_SETTING_NAMES = {
    "hotkey": "хоткей",
    "source_lang": "исходный язык",
    "target_lang": "язык перевода",
    "theme": "тема",
    "autotranslate": "автоперевод",
    "slow_after_sec": "задержка статуса «Перевод...»",
    "notification_duration_sec": "время жизни уведомления",
    "debounce_sec": "задержка автоперевода",
    "max_text_length": "макс. длина текста",
    "filter_cyrillic": "фильтр кириллицы",
}

# Запись хоткея: tkinter keysym -> имя pynput для модификаторов.
_HK_MODIFIER_KEYS = {
    "Control_L": "ctrl", "Control_R": "ctrl",
    "Alt_L": "alt", "Alt_R": "alt",
    "Shift_L": "shift", "Shift_R": "shift",
    "Super_L": "cmd", "Super_R": "cmd",
}
_HK_MODIFIER_NAMES = {"ctrl", "alt", "shift", "cmd"}
_HK_IGNORE_KEYS = {
    "Caps_Lock", "Num_Lock", "Scroll_Lock", "Mode_switch",
    "ISO_Level3_Shift", "ISO_Left_Tab",
}


class SettingsDialog(ctk.CTkToplevel):
    """Окно «Настройки».

    Изменения применяются сразу после «Сохранить» (без перезапуска):
    тема, направление, параметры автоперевода, глобальный хоткей.
    Невалидные значения не сохраняются — о проблеме сообщается в окне.
    Escape закрывает окно (во время записи хоткея — отменяет запись).
    """

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Настройки")
        self.geometry("560x640")
        self.minsize(500, 480)
        self.configure(fg_color=app._pal["bg"])
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", self._on_escape)

        # Состояние записи хоткея
        self._recording = False
        self._hk_orig = ""
        self._hk_mods = []
        self._hk_pending = None
        # Защита от взаимного перезаписывания языковых меню
        self._syncing = False

        self._build_ui()

    # ------------------------------------------------------------------ #
    def _build_ui(self):
        pal = self.app._pal
        s = self.app.settings

        # Фиксированная нижняя панель (вне прокручиваемой области):
        # слева — ошибка валидации, справа — кнопки «Отмена» / «Сохранить».
        # Кнопки всегда внизу окна независимо от размера окна и количества
        # настроек; вертикальный scrollbar относится только к содержимому.
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(side="bottom", fill="x", padx=18, pady=(4, 14))
        bottom.grid_columnconfigure(0, weight=1)
        self.error_label = ctk.CTkLabel(bottom, text="", font=("Arial", 12),
                                        text_color=pal["error"], anchor="w",
                                        justify="left")
        self.error_label.grid(row=0, column=0, sticky="w")
        btns = ctk.CTkFrame(bottom, fg_color="transparent")
        btns.grid(row=0, column=1, sticky="e")
        self.cancel_btn = ctk.CTkButton(btns, text="Отмена", width=110,
                                        height=36, font=("Arial", 13),
                                        corner_radius=8, fg_color=pal["panel"],
                                        hover_color=pal["panel_hover"],
                                        text_color=pal["text"],
                                        command=self._on_close)
        self.cancel_btn.pack(side="left")
        self.save_btn = ctk.CTkButton(btns, text="Сохранить", width=140,
                                      height=36, font=("Arial", 13, "bold"),
                                      corner_radius=8, fg_color=pal["accent"],
                                      hover_color=pal["accent_hover"],
                                      text_color=pal["accent_text"],
                                      command=self._on_save)
        self.save_btn.pack(side="left", padx=(10, 0))

        # Прокручиваемая область настроек: при росте окна занимает всё
        # свободное место, при избытке содержимого — вертикальный scrollbar
        # (только этой области, нижняя панель не прокручивается).
        self._scroll_frame = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_fg_color=pal["field"],
            scrollbar_button_color=pal["scrollbar"],
            scrollbar_button_hover_color=pal["scrollbar_hover"])
        self._scroll_frame.pack(side="top", fill="both", expand=True,
                                padx=18, pady=(14, 0))
        # Сетка «label + control»: колонка подписей — фиксированный
        # минимальный width (выравнивает все контролы), колонка контролов —
        # weight=1: поля ввода и меню растягиваются по ширине окна.
        self._scroll_frame.grid_columnconfigure(0, weight=0, minsize=185)
        self._scroll_frame.grid_columnconfigure(1, weight=1, minsize=170)

        row = 0

        def row_label(text):
            nonlocal row
            ctk.CTkLabel(self._scroll_frame, text=text,
                         font=("Arial", 13, "bold"), text_color=pal["text"],
                         anchor="w", wraplength=170).grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=(12, 2))

        def make_entry(value):
            nonlocal row
            e = ctk.CTkEntry(self._scroll_frame, width=200, height=32,
                             corner_radius=8, font=("Arial", 13),
                             fg_color=pal["field"],
                             border_color=pal["panel_hover"],
                             text_color=pal["text"],
                             placeholder_text_color=pal["muted"])
            e.insert(0, value)
            e.grid(row=row, column=1, sticky="ew", pady=(0, 2))
            e._entry.bind("<KeyPress>", self.app._on_physical_hotkey,
                          add="+")
            self._make_resizable(e, min_width=140)
            row += 1
            return e

        def make_option(values, current, command=None):
            nonlocal row
            var = ctk.StringVar(value=current)
            m = ctk.CTkOptionMenu(self._scroll_frame, variable=var,
                                  values=values, width=240, height=32,
                                  corner_radius=8, font=("Arial", 13),
                                  command=command, fg_color=pal["field"],
                                  button_color=pal["panel_hover"],
                                  button_hover_color=pal["scrollbar_hover"],
                                  text_color=pal["text"],
                                  dropdown_fg_color=pal["panel"],
                                  dropdown_hover_color=pal["panel_hover"],
                                  dropdown_text_color=pal["text"])
            m.grid(row=row, column=1, sticky="ew", pady=(0, 2))
            self._make_resizable(m, min_width=170)
            # Виджет меню запоминаем для вызывающего кода (например,
            # переполнение списка моделей при смене направления).
            self._last_option_menu = m
            row += 1
            return var

        def make_switch(text, value):
            nonlocal row
            var = ctk.IntVar(value=1 if value else 0)
            sw = ctk.CTkSwitch(self._scroll_frame, text=text, variable=var,
                               font=("Arial", 13), text_color=pal["muted"],
                               fg_color=pal["panel"],
                               progress_color=pal["accent"],
                               switch_width=40, switch_height=22)
            sw.grid(row=row, column=1, sticky="w", pady=(14, 2))
            row += 1
            return var

        # Схема оформления (Этап 12): подпись и короткое пояснение —
        # назначение выбора очевидно без догадок (раньше был просто
        # безобъяснительный дропдаун «Тема»). Механика не изменилась:
        # выбор сохраняется в settings.json и по «Сохранить» применяется
        # к окну и диалогу сразу, без перезапуска.
        row_label("Схема оформления")
        self.theme_var = make_option(list(_THEME_LABELS.values()),
                                     _THEME_LABELS[s.get("theme")])
        self.theme_note_label = ctk.CTkLabel(
            self._scroll_frame,
            text="Тёмная — комфортнее при слабом освещении. "
                 "Светлая — выше яркость и контраст в освещённом "
                 "помещении. Применяется сразу, без перезапуска.",
            font=("Arial", 11), anchor="w", justify="left",
            text_color=pal["muted"], wraplength=380)
        self.theme_note_label.grid(row=row, column=1, sticky="ew", pady=(0, 2))
        row += 1

        # Языки (взаимоисключающие: выбор одного меняет второе)
        row_label("Исходный язык")
        self.source_lang_var = make_option(list(_LANG_LABELS.values()),
                                           _LANG_LABELS[s.get("source_lang")],
                                           self._on_source_lang)
        row_label("Язык перевода")
        self.target_lang_var = make_option(list(_LANG_LABELS.values()),
                                           _LANG_LABELS[s.get("target_lang")],
                                           self._on_target_lang)

        # Модель (Этап 10): список строится из ModelManager (дескрипторы +
        # ЛЁГКАЯ проверка локальной доступности) — никаких импортов/создания
        # inference-бэкендов и тяжёлых библиотек (torch/transformers и
        # т.п.) и обращений к сети в диалоге нет. Выбор применяется только по
        # «Сохранить» — через app._set_model (resolve_runtime + персистентность
        # + фоновая загрузка, если нужна).
        row_label("Модель")
        self.model_var = make_option([], "", self._on_model_selected)
        self.model_menu = self._last_option_menu
        self.model_note_label = ctk.CTkLabel(
            self._scroll_frame, text="", font=("Arial", 11), anchor="w",
            justify="left", text_color=pal["muted"], wraplength=380)
        self.model_note_label.grid(row=row, column=1, sticky="ew",
                                   pady=(0, 2))
        row += 1
        self._model_label_to_id = {}
        self._refresh_model_menu()

        # Автоперевод и тайминги
        row_label("Автоперевод")
        self.autotranslate_var = make_switch(
            "после остановки набора текста", s.get("autotranslate"))

        row_label("Задержка автоперевода, сек")
        self.debounce_entry = make_entry(str(s.get("debounce_sec")))

        row_label("Задержка статуса, сек")
        self.slow_entry = make_entry(str(s.get("slow_after_sec")))

        row_label("Уведомление, сек")
        self.notification_duration_entry = make_entry(
            str(s.get("notification_duration_sec")))

        row_label("Макс. длина, символов")
        self.max_len_entry = make_entry(str(s.get("max_text_length")))

        # Глобальный хоткей
        row_label("Автоопределение направления")
        self.filter_var = make_switch(
            "по написанию перехваченного текста", s.get("filter_cyrillic"))

        row_label("Глобальный хоткей")
        hk_frame = ctk.CTkFrame(self._scroll_frame, fg_color="transparent")
        hk_frame.grid(row=row, column=1, sticky="w", pady=(0, 2))
        self.hotkey_entry = ctk.CTkEntry(hk_frame, width=200, height=32,
                                         corner_radius=8, font=("Arial", 13),
                                         fg_color=pal["field"],
                                         border_color=pal["panel_hover"],
                                         text_color=pal["text"])
        self.hotkey_entry.insert(0, s.get("hotkey"))
        self.hotkey_entry._entry.bind(
            "<KeyPress>", self.app._on_physical_hotkey, add="+")
        self.hotkey_entry.pack(side="left")
        ctk.CTkButton(hk_frame, text="Записать", width=90, height=32,
                      font=("Arial", 13), corner_radius=8,
                      fg_color=pal["panel"], hover_color=pal["panel_hover"],
                      text_color=pal["text"],
                      command=self._start_recording).pack(side="left", padx=(8, 0))
        row += 1

        # Состояние глобального хоткея (backend) — последняя строка области.
        self.agent_state_label = ctk.CTkLabel(self._scroll_frame, text="",
                                              font=("Arial", 11), anchor="w",
                                              text_color=pal["muted"])
        self.agent_state_label.grid(row=row, column=0, columnspan=2,
                                    sticky="w", pady=(10, 2))
        row += 1
        self._update_agent_state()

    def _make_resizable(self, widget, min_width):
        """CTkEntry/CTkOptionMenu в grid-колонке с weight=1: растягивать
        контрол по ширине вместе с окном.

        Без этого canvas CTk-контрола перерисовывает рамку в начальном
        размере (встроенной перерисовки по <Configure> у этих виджетов
        нет): при изменении ширины колонки canvas отрисовываем заново
        с новой шириной. min_width — минимальная ширина контрола.
        """
        scale = widget._apply_widget_scaling(1.0) or 1.0
        def on_configure(event):
            w = int(event.width / scale)
            if w >= min_width and abs(w - widget._desired_width) > 3:
                widget.configure(width=w)
        widget._canvas.bind("<Configure>", on_configure, add="+")

    # ------------------------------------------------------------------ #
    #  Служебное                                                         #
    # ------------------------------------------------------------------ #
    def _update_agent_state(self):
        """Строка о состоянии глобального хоткея в диалоге."""
        pal = self.app._pal
        agent = self.app._hotkey_agent
        if PYNPUT_AVAILABLE and agent is not None and agent.active:
            text = f"Активен: {self.app.settings.get('hotkey')} (работает в любой программе)"
            color = pal["success"]
        elif self.app._agent_error:
            text = f"Недоступен: {self.app._agent_error}"
            color = pal["error"]
        elif not PYNPUT_AVAILABLE:
            text = "Недоступен: не установлен pynput (pip install pynput)"
            color = pal["error"]
        else:
            text = "Не активен"
            color = pal["muted"]
        self.agent_state_label.configure(text=text, text_color=color)

    def _show_error(self, text: str):
        self.error_label.configure(text=text)

    def _clear_error(self):
        self.error_label.configure(text="")

    def _on_escape(self, _event):
        # Во время записи хоткея Escape обрабатывает _hk_on_key
        # (отмена записи, окно остаётся открытым).
        if self._recording:
            return None
        self._on_close()
        return "break"

    def _on_close(self):
        if self._recording:
            self._stop_recording(cancel=True)
        if self.app._settings_dialog is self:
            self.app._settings_dialog = None
        try:
            self.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    #  Языковые меню: взаимоисключающие                                   #
    # ------------------------------------------------------------------ #
    def _on_source_lang(self, value):
        if self._syncing:
            return
        self._syncing = True
        try:
            other = ("Русский" if value in ("Английский", "Английский (EN)")
                     else "Английский")
            self.target_lang_var.set(other)
        finally:
            self._syncing = False
        # Направление изменилось — список моделей актуализируется
        # (неподдерживаемые помечаются, при необходимости — дефолт).
        self._refresh_model_menu()

    def _on_target_lang(self, value):
        if self._syncing:
            return
        self._syncing = True
        try:
            other = ("Английский" if value in ("Русский", "Русский (RU)")
                     else "Русский")
            self.source_lang_var.set(other)
        finally:
            self._syncing = False
        self._refresh_model_menu()

    # ------------------------------------------------------------------ #
    #  Выбор модели (Этап 10) — лёгкий, backend-нейтральный              #
    # ------------------------------------------------------------------ #
    def _model_direction(self):
        """Текущее направление диалога по языковым меню (меню
        взаимоисключающие → всегда "en-ru" или "ru-en")."""
        return (f"{_LANG_FROM_LABEL[self.source_lang_var.get()]}-"
                f"{_LANG_FROM_LABEL[self.target_lang_var.get()]}")

    def _model_labels(self, direction: str):
        """Пары (label опции, model id) в порядке реестра (детерминированно).

        Label показывает отображаемое имя и ЛЁГКУЮ проверку доступности
        (ModelManager.is_model_available — filesystem, без загрузки
        моделей и без сети). Модель, не поддерживающая текущее
        направление, получает однозначный маркер «не подходит для ...»
        (Этап 14: вместо противоречивого «доступна (направление не
        поддерживается)») — пользователь видит, что именно будет
        заменено дефолтом при сохранении.
        """
        manager = self.app.model_manager
        dir_label = "Русский → Английский" if direction == "ru-en" else "Английский → Русский"
        labels = []
        for desc in manager.list_models():
            if not desc.supports_direction(direction):
                label = f"{desc.name} — не подходит для {dir_label}"
            else:
                status = ("доступна" if manager.is_model_available(desc.id)
                          else "недоступна локально")
                label = f"{desc.name} — {status}"
            labels.append((label, desc.id))
        return labels

    def _refresh_model_menu(self):
        """(Пере)заполняет меню моделей под текущее направление и сохраняет
        текущий выбор, если он остался в списке; иначе — модель по
        умолчанию для направления (первая подходящая в реестре)."""
        direction = self._model_direction()
        labels = self._model_labels(direction)
        self._model_label_to_id = dict(labels)
        current = self.model_var.get()
        selected = current if current in self._model_label_to_id else ""
        if not selected:
            default = self.app.model_manager.get_default_model(direction)
            for label, mid in labels:
                if default is not None and mid == default.id:
                    selected = label
                    break
            if not selected and labels:
                selected = labels[0][0]
        self.model_menu.configure(values=[label for label, _id in labels])
        self.model_var.set(selected)
        self._update_model_note()

    def _on_model_selected(self, _value):
        """Пользователь выбрал модель в дропдауне — обновляем пояснение."""
        self._update_model_note()

    def _update_model_note(self):
        """Строка под меню модели: что произойдёт с выбранной моделью при
        сохранении/запуске (только лёгкие проверки, без загрузки моделей)."""
        pal = self.app._pal
        manager = self.app.model_manager
        label = self.model_var.get()
        model_id = self._model_label_to_id.get(label)
        if not model_id:
            self.model_note_label.configure(text="Модель не выбрана",
                                            text_color=pal["muted"])
            return
        direction = self._model_direction()
        desc = manager.get_model(model_id)
        if not desc.supports_direction(direction):
            default = manager.get_default_model(direction)
            self.model_note_label.configure(
                text=(f"Не поддерживает направление. При сохранении будет "
                      f"использована модель по умолчанию: "
                      f"{default.name if default is not None else '—'}"),
                text_color=pal["pending"])
        elif manager.is_model_available(model_id):
            self.model_note_label.configure(text="Доступна",
                                            text_color=pal["success"])
        else:
            default = manager.get_default_model(direction)
            fallback = (f" До её доступности будет использоваться "
                        f"модель по умолчанию: {default.name}"
                        if default is not None and default.id != model_id
                        else "")
            self.model_note_label.configure(
                text="Нет локального кэша. При запуске модель будет взята "
                     "из бандля или загружена из HuggingFace." + fallback,
                text_color=pal["pending"])

    # ------------------------------------------------------------------ #
    #  Запись хоткея (нажмите сочетание)                                  #
    # ------------------------------------------------------------------ #
    def _start_recording(self):
        if self._recording:
            return
        self._recording = True
        self._hk_orig = self.hotkey_entry.get().strip()
        self._hk_mods = []
        self._hk_pending = None
        self._clear_error()
        self._fill_hotkey("Нажмите сочетание...")
        self.hotkey_entry.configure(fg_color=self.app._pal["pending"])
        # Топ-левел диалога входит в binding-теги всех его детей —
        # клавиши ловим здесь, где бы фокус ни был.
        self.bind("<Key>", self._hk_on_key)
        self.bind("<KeyRelease>", self._hk_on_release)
        self.hotkey_entry.focus_set()

    def _stop_recording(self, cancel: bool = False):
        if not self._recording:
            return
        self._recording = False
        self.unbind("<Key>")
        self.unbind("<KeyRelease>")
        self.hotkey_entry.configure(fg_color=self.app._pal["field"])
        if cancel:
            self._fill_hotkey(self._hk_orig)

    def _fill_hotkey(self, text: str):
        self.hotkey_entry.delete(0, "end")
        self.hotkey_entry.insert(0, text)

    @staticmethod
    def _hk_key_name(keysym: str) -> str:
        if keysym in _HK_MODIFIER_KEYS:
            return _HK_MODIFIER_KEYS[keysym]
        if len(keysym) == 1 and keysym.isalnum():
            return keysym.lower()
        return keysym.lower()

    def _hk_event_name(self, event) -> str:
        """Имя основной клавиши по физическому коду при записи хоткея."""
        return _physical_key_name(event) or self._hk_key_name(event.keysym)

    def _hk_on_key(self, event):
        if not self._recording:
            return None
        if event.keysym == "Escape":
            # Esc во время записи — отмена (окно не закрывается)
            self._stop_recording(cancel=True)
            return "break"
        if event.keysym in _HK_IGNORE_KEYS:
            return None
        name = self._hk_event_name(event)
        if name in _HK_MODIFIER_NAMES:
            if name not in self._hk_mods:
                self._hk_mods.append(name)
            return None
        # Главная клавиша: снимок комбинации, пока модификаторы ещё зажаты.
        self._hk_pending = "+".join([*self._hk_mods, name])
        return None

    def _hk_on_release(self, event):
        if not self._recording:
            return None
        if event.keysym in _HK_IGNORE_KEYS:
            return None
        name = self._hk_event_name(event)
        if name in _HK_MODIFIER_NAMES:
            if name in self._hk_mods:
                self._hk_mods.remove(name)
            return None
        if self._hk_pending:
            combo = self._hk_pending
            self._hk_pending = None
            self._stop_recording(cancel=False)
            # Валидация парсером pynput (или паттерном без pynput).
            if normalize_hotkey(combo) is None:
                self._show_error(f"Сочетание {combo!r} не подходит — сохранено прежнее значение")
                self._fill_hotkey(self._hk_orig)
            else:
                self._fill_hotkey(combo)
        return None

    # ------------------------------------------------------------------ #
    #  Сохранение                                                         #
    # ------------------------------------------------------------------ #
    def _on_save(self):
        values = {
            "theme": _THEME_FROM_LABEL[self.theme_var.get()],
            "source_lang": _LANG_FROM_LABEL[self.source_lang_var.get()],
            "target_lang": _LANG_FROM_LABEL[self.target_lang_var.get()],
            "autotranslate": bool(self.autotranslate_var.get()),
            "debounce_sec": self.debounce_entry.get().strip().replace(",", "."),
            "slow_after_sec": self.slow_entry.get().strip().replace(",", "."),
            "notification_duration_sec": (
                self.notification_duration_entry.get().strip().replace(",", ".")),
            "max_text_length": self.max_len_entry.get().strip(),
            "filter_cyrillic": bool(self.filter_var.get()),
            "hotkey": self.hotkey_entry.get().strip(),
        }
        if values["source_lang"] == values["target_lang"]:
            self._show_error("Исходный язык и язык перевода должны отличаться")
            return
        # Сначала валидируем ВСЁ, потом применяем: при ошибке в одном поле
        # настройки не меняются вовсе (ни в памяти, ни в файле).
        normalized = {}
        bad = []
        for key, value in values.items():
            ok, norm = validate_value(key, value)
            if ok:
                normalized[key] = norm
            else:
                bad.append(key)
        if bad:
            self._show_error("Недопустимые значения: "
                             + ", ".join(_SETTING_NAMES[k] for k in bad))
            return
        self._clear_error()
        for key, norm in normalized.items():
            self.app.settings.set(key, norm)
        self.app.settings.save()

        # Применяем без перезапуска:
        ctk.set_appearance_mode(self.app.settings.get("theme"))
        self.app._set_theme(self.app.settings.get("theme"))
        # Направление ПЕРВЫМ (далее resolve_runtime в _set_model работает
        # уже с новым направлением).
        self.app._set_direction(f"{normalized['source_lang']}-{normalized['target_lang']}")
        # Модель (Этап 10): выбор из диалога — через app._set_model
        # (resolve_runtime + персистентность + фоновая загрузка, если
        # запуск использует другую модель). Неизменённый выбор — no-op.
        # Бэкенды в этом месте диалога по-прежнему не импортируются:
        # загрузка модели — фоновое действие приложения.
        model_id = self._model_label_to_id.get(self.model_var.get())
        if model_id != self.app.settings.get("model_id"):
            self.app._set_model(model_id)
        self.app._start_hotkey_agent()
        # Честный финальный статус (Этап 14): если сохранение запустило
        # (пере)загрузку модели, «Настройки сохранены» без указания этого
        # вела к ложному впечатлению готовности. Заметка о фолбэке, если
        # появилась, — тоже в статусе, а не только на момент сохранения.
        # _model_loading — синхронный флаг (_start_model_load), а не
        # «translator is None»: быстрый worker мог уже подставить
        # переводчик. После init_done статус станет обычным ready с
        # названием модели.
        if self.app._model_loading:
            self.app._set_status("busy",
                                 "Настройки сохранены. Загрузка модели...")
        elif self.app._model_note:
            note, self.app._model_note = self.app._model_note, None
            self.app._set_status("ready", "Настройки сохранены. %s" % note)
        else:
            self.app._set_status("ready", "Настройки сохранены")
        self._on_close()


if __name__ == "__main__":
    app = TranslatorApp()
    app.mainloop()
