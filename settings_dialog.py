"""Settings window; application orchestration remains in TranslatorApp."""
import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog
import logging
import os
import platform
import re
from hotkey_agent import PYNPUT_AVAILABLE
from input_shortcuts import _physical_key_name
from settings import normalize_hotkey, validate_value
from local_models import configure_paths, local_path, has_transformers_model, validate_transformers_model
from model_registry import ModelNotFoundError
from ui_widgets import _Tooltip, PALETTES
logger = logging.getLogger("offline_translate.gui")

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
        app._set_window_icon(self)
        self.geometry("760x640")
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
                                        width=1,
                                        text_color=pal["error"], anchor="w",
                                        justify="left")
        self.error_label.grid(row=0, column=0, columnspan=2, sticky="ew")
        bottom.bind("<Configure>", lambda event: self.error_label.configure(
            wraplength=max(100, int(event.width / bottom._apply_widget_scaling(1.0)))), add="+")
        btns = ctk.CTkFrame(bottom, fg_color="transparent")
        btns.grid(row=1, column=1, sticky="e", pady=(4, 0))
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

        # Навигация слева остаётся видимой, а длинный список настроек
        # прокручивается справа. Это сохраняет текущие имена виджетов и
        # делает окно заметно удобнее на небольших экранах.
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(side="top", fill="both", expand=True, padx=18, pady=(14, 0))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        sidebar = ctk.CTkFrame(body, fg_color=pal["panel"], corner_radius=10,
                               width=125)
        sidebar.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        sidebar.pack_propagate(False)
        ctk.CTkLabel(sidebar, text="Разделы", font=("Arial", 13, "bold"),
                     text_color=pal["text"]).pack(padx=10, pady=(14, 8))

        self._scroll_frame = ctk.CTkScrollableFrame(
            body, fg_color="transparent",
            scrollbar_fg_color=pal["field"],
            scrollbar_button_color=pal["scrollbar"],
            scrollbar_button_hover_color=pal["scrollbar_hover"])
        self._scroll_frame.grid(row=0, column=1, sticky="nsew")
        # Сетка «label + control»: колонка подписей — фиксированный
        # минимальный width (выравнивает все контролы), колонка контролов —
        # weight=1: поля ввода и меню растягиваются по ширине окна.
        self._scroll_frame.grid_columnconfigure(0, weight=0, minsize=160)
        self._scroll_frame.grid_columnconfigure(1, weight=1, minsize=170)
        def resize_body(event):
            if event.width / body._apply_widget_scaling(1.0) < 650:
                sidebar.grid_remove()
            else:
                sidebar.grid()
        body.bind("<Configure>", resize_body, add="+")

        row = 0
        section_labels = {}

        def row_label(text):
            nonlocal row
            label = ctk.CTkLabel(self._scroll_frame, text=text,
                                font=("Arial", 13, "bold"), text_color=pal["text"],
                                anchor="w", justify="left", wraplength=145)
            label.grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=(12, 2))
            _Tooltip(label, text)
            section_labels[text] = label

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

        def make_spinbox(value, minimum, maximum, step, nested=False):
            """Числовое поле с кнопками и клавишами увеличения/уменьшения."""
            nonlocal row
            parent = self._scroll_frame
            if nested:
                parent = ctk.CTkFrame(self._scroll_frame, fg_color="transparent")
                parent.grid(row=row, column=1, sticky="ew", pady=(0, 2))
                parent.grid_columnconfigure(0, weight=1)
            entry = ctk.CTkEntry(parent, width=140, height=32,
                                 corner_radius=8, font=("Arial", 13),
                                 fg_color=pal["field"],
                                 border_color=pal["panel_hover"],
                                 text_color=pal["text"])
            entry.insert(0, value)
            entry.grid(row=0 if nested else row, column=0 if nested else 1,
                       sticky="ew", pady=(0, 2) if not nested else 0)
            entry._entry.bind("<KeyPress>", self.app._on_physical_hotkey, add="+")
            self._make_resizable(entry, min_width=140)
            def change(delta):
                try:
                    current = float(entry.get().replace(",", "."))
                except ValueError:
                    current = minimum
                current = max(minimum, min(maximum, current + delta))
                text = str(int(current)) if float(current).is_integer() else f"{current:g}"
                entry.delete(0, "end")
                entry.insert(0, text)
            entry._entry.bind("<Up>", lambda _event: (change(step), "break")[1], add="+")
            entry._entry.bind("<Down>", lambda _event: (change(-step), "break")[1], add="+")
            buttons = ctk.CTkFrame(parent, fg_color="transparent")
            buttons.grid(row=0 if nested else row, column=1 if nested else 2,
                         padx=(4, 0), sticky="e")
            for caption, delta, tip in (("−", -step, "Уменьшить значение"),
                                        ("+", step, "Увеличить значение")):
                button = ctk.CTkButton(buttons, text=caption, width=28, height=32,
                                      fg_color=pal["panel"], hover_color=pal["panel_hover"],
                                      text_color=pal["text"], command=lambda d=delta: change(d))
                button.pack(side="left", padx=1)
                _Tooltip(button, tip)
            row += 1
            return entry

        def make_option(values, current, command=None):
            nonlocal row
            var = ctk.StringVar(value=current)
            m = ctk.CTkOptionMenu(self._scroll_frame, variable=var,
                                  values=values, width=170, height=32,
                                  dynamic_resizing=False,
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
            _Tooltip(m, lambda: var.get())
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
            sw.grid(row=row, column=1, sticky="ew", pady=(14, 2))
            sw._text_label.configure(wraplength=110, justify="left")
            sw._bg_canvas.bind("<Configure>", lambda event: sw._text_label.configure(
                wraplength=max(90, int(event.width / sw._apply_widget_scaling(1.0)) - 60)), add="+")
            _Tooltip(sw, text)
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
            text_color=pal["muted"], width=1, wraplength=170)
        self.theme_note_label.grid(row=row, column=1, sticky="ew", pady=(0, 2))
        self.theme_note_label.bind("<Configure>", lambda event: self.theme_note_label.configure(
            wraplength=max(100, int(event.width / self.theme_note_label._apply_widget_scaling(1.0)))), add="+")
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
            justify="left", text_color=pal["muted"], width=1, wraplength=170)
        self.model_note_label.grid(row=row, column=1, sticky="ew",
                                   pady=(0, 2))
        self.model_note_label.bind("<Configure>", lambda event: self.model_note_label.configure(
            wraplength=max(100, int(event.width / self.model_note_label._apply_widget_scaling(1.0)))), add="+")
        row += 1
        self._model_label_to_id = {}
        self._refresh_model_menu()

        self.model_path_entries = {}
        for key, caption, is_file in (
                ("marian_en_ru_path", "Локальная Marian EN → RU", False),
                ("marian_ru_en_path", "Локальная Marian RU → EN", False),
                ("gguf_path", "Локальный GGUF Hy-MT2", True)):
            row_label(caption)
            frame = ctk.CTkFrame(self._scroll_frame, fg_color="transparent")
            frame.grid(row=row, column=1, columnspan=2, sticky="ew", pady=(0, 2))
            frame.grid_columnconfigure(0, weight=1)
            entry = ctk.CTkEntry(frame, width=140, height=32,
                                fg_color=pal["field"], text_color=pal["text"],
                                border_color=pal["panel_hover"],
                                placeholder_text="Автоматический поиск")
            entry.insert(0, s.get(key))
            entry.grid(row=0, column=0, sticky="ew")
            self.model_path_entries[key] = entry
            def browse(field=entry, file=is_file):
                path = (filedialog.askopenfilename(parent=self, title="Выберите GGUF Hy-MT2",
                        filetypes=[("GGUF", "*.gguf")]) if file else
                        filedialog.askdirectory(parent=self, title="Папка модели с config.json и весами"))
                if path:
                    field.delete(0, "end")
                    field.insert(0, path)
            button = ctk.CTkButton(frame, text="Обзор…", width=82, height=32,
                                  fg_color=pal["panel"], hover_color=pal["panel_hover"],
                                  text_color=pal["text"], command=browse)
            button.grid(row=0, column=1, padx=(6, 0))
            _Tooltip(entry, lambda field=entry: field.get() or
                     "Пустое поле: искать модель в папке models рядом с приложением, затем в кэше.")
            row += 1

        sections = (("Основные", "Схема оформления"),
                    ("Перевод", "Автоперевод"), ("Хоткей", "Глобальный хоткей"))
        def jump(section):
            self.update_idletasks()
            label = section_labels[section]
            fraction = label.winfo_y() / max(1, self._scroll_frame.winfo_height())
            self._scroll_frame._parent_canvas.yview_moveto(fraction)
        for title, section in sections:
            ctk.CTkButton(sidebar, text=title, height=30, anchor="w",
                          fg_color="transparent", hover_color=pal["panel_hover"],
                          text_color=pal["text"], command=lambda s=section: jump(s)
                          ).pack(fill="x", padx=6, pady=2)

        # Автоперевод и тайминги
        row_label("Автоперевод")
        self.autotranslate_var = make_switch(
            "после остановки набора текста", s.get("autotranslate"))

        row_label("Задержка автоперевода, сек")
        self.debounce_entry = make_spinbox(str(s.get("debounce_sec")), 0.1, 60, 0.5)

        row_label("Задержка статуса, сек")
        self.slow_entry = make_spinbox(str(s.get("slow_after_sec")), 0, 600, 0.5)

        row_label("Уведомление, сек")
        self.notification_duration_entry = make_spinbox(
            str(s.get("notification_duration_sec")), 1, 120, 1, nested=True)

        row_label("Макс. длина, символов")
        self.max_len_entry = make_spinbox(str(s.get("max_text_length")), 0, 1000000, 100)

        # Глобальный хоткей
        row_label("Автоопределение направления")
        self.filter_var = make_switch(
            "по написанию перехваченного текста", s.get("filter_cyrillic"))

        row_label("Глобальный хоткей")
        hk_frame = ctk.CTkFrame(self._scroll_frame, fg_color="transparent")
        hk_frame.grid(row=row, column=1, columnspan=2, sticky="ew", pady=(0, 2))
        hk_frame.grid_columnconfigure(0, weight=1)
        self.hotkey_entry = ctk.CTkEntry(hk_frame, width=140, height=32,
                                         corner_radius=8, font=("Arial", 13),
                                         fg_color=pal["field"],
                                         border_color=pal["panel_hover"],
                                         text_color=pal["text"])
        self.hotkey_entry.insert(0, s.get("hotkey"))
        self.hotkey_entry._entry.bind(
            "<KeyPress>", self.app._on_physical_hotkey, add="+")
        self.hotkey_entry.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(hk_frame, text="Записать", width=90, height=32,
                      font=("Arial", 13), corner_radius=8,
                      fg_color=pal["panel"], hover_color=pal["panel_hover"],
                      text_color=pal["text"],
                      command=self._start_recording).grid(row=0, column=1, padx=(8, 0))
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
        dir_label = "RU → EN" if direction == "ru-en" else "EN → RU"
        labels = []
        for desc in manager.list_models():
            if not desc.supports_direction(direction):
                label = f"{desc.name} — не подходит для {dir_label}"
            elif self.app._runtime_model_state(desc.id)[0]:
                suffix = ("работает; новая конфигурация не загрузилась"
                          if self.app._runtime_model_state(desc.id)[1] else "готова")
                label = f"{desc.name} — доступна ({suffix})"
            elif self.app._runtime_model_state(desc.id)[1]:
                label = f"{desc.name} — ошибка загрузки"
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
        current = self.model_var.get()
        current_id = getattr(self, "_model_label_to_id", {}).get(current)
        if (getattr(self, "_model_menu_direction", None) != direction and current_id
                and not self.app.model_manager.get_model(current_id).supports_direction(direction)):
            current_id, current = None, ""
        self._model_menu_direction = direction
        labels = self._model_labels(direction)
        self._model_label_to_id = dict(labels)
        current = next((label for label, mid in labels if mid == current_id), current)
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
        elif self.app._runtime_model_state(model_id)[0]:
            failed = self.app._runtime_model_state(model_id)[1]
            self.model_note_label.configure(
                text=("Текущая модель работает. Новая конфигурация не загрузилась."
                      if failed else "Модель загружена и готова к переводу."),
                text_color=pal["pending"] if failed else pal["success"])
        elif self.app._runtime_model_state(model_id)[1]:
            self.model_note_label.configure(
                text="Последняя загрузка завершилась ошибкой. Проверьте "
                     "файлы модели и перезапустите загрузку.",
                text_color=pal["error"])
        elif manager.is_model_available(model_id):
            loaded = (self.app.translator is not None
                      and not self.app._model_loading
                      and self.app._translator_model_id == model_id)
            self.model_note_label.configure(
                text=("Модель загружена и готова к переводу." if loaded else
                      "Файлы модели найдены. Готовность будет проверена при загрузке."),
                text_color=pal["success"] if loaded else pal["muted"])
        else:
            default = manager.get_default_model(direction)
            fallback = (f" До её доступности будет использоваться "
                        f"модель по умолчанию: {default.name}"
                        if default is not None and default.id != model_id
                        else "")
            self.model_note_label.configure(
                text="Модель не найдена локально. Распакуйте архив моделей рядом с приложением "
                     "или укажите свою совместимую модель ниже." + fallback,
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
            **{key: field.get().strip() for key, field in self.model_path_entries.items()},
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
        for key in ("marian_en_ru_path", "marian_ru_en_path", "gguf_path"):
            path = normalized[key]
            if path:
                path = normalized[key] = os.path.abspath(os.path.expanduser(path))
            if path and key != "gguf_path":
                validation = validate_transformers_model(path)
                if not validation.available:
                    self._show_error("Неполная локальная модель: " + validation.reason)
                    return
            elif path and not (os.path.isfile(path) and path.lower().endswith(".gguf")):
                self._show_error("Не найдена локальная модель: " + path)
                return
        paths_changed = any(self.app.settings.get(key) != normalized[key]
                            for key in self.model_path_entries)
        load_seq = self.app._load_seq
        previous_settings = self.app.settings.as_dict()
        for key, norm in normalized.items():
            self.app.settings.set(key, norm)
        if not self.app.settings.save():
            for key, value in previous_settings.items():
                self.app.settings.set(key, value)
            self._show_error("Не удалось сохранить настройки. Проверьте доступ к файлу настроек.")
            return
        configure_paths(normalized["marian_en_ru_path"], normalized["marian_ru_en_path"],
                        normalized["gguf_path"])

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
        if model_id != self.app.settings.get("model_id") or paths_changed:
            self.app._set_model(model_id)
        if paths_changed and self.app._load_seq == load_seq:
            self.app._start_model_load()
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


