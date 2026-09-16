"""Offline Translate — GUI на CustomTkinter (Catppuccin Mocha, dark theme).

Макет (grid):
  0-я строка  — заголовок "🌐 Offline Translate"
  1-я строка  — поле ввода (английский, ~140px)
  2-я строка  — панель: Clear | бейдж источника (NN/GLOSSARY) | Copy
  3-я строка  — поле вывода (русский, read-only, ~180px)
  4-я строка  — статус-бар (слева статус, справа время перевода)

Перевод выполняется автоматически: 1.5 секунды после последнего нажатия
клавиши (debounce через after / after_cancel). Перевод — в фоновом потоке,
результат возвращается в UI через очередь. Escape закрывает окно.
"""
from __future__ import annotations

import os
import queue
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

# Linux: бэкенд AppIndicator — нативное Gtk-меню, где работает правый
# клик (x11-бэкенд pystray меню вообще не рендерит, _update_menu — no-op).
# Гард по gi: без PyGObject принудительный бэкенд уронит import pystray
# (импорт в __init__ пакета, вне try/except) — пусть pystray сам
# откатится на x11, а трей-иконка отключится в _setup_tray.
if os.name == "posix" and not os.environ.get("PYSTRAY_BACKEND"):
    try:
        import gi  # noqa: F401
    except ImportError:
        pass

import pystray
from PIL import Image, ImageDraw

from hotkey_agent import HotkeyAgent
from offline_translate import OfflineTranslator
from settings import Settings, normalize_hotkey

_SETTINGS = Settings()  # один общий инстанс: GUI, агент и переводчик
_TRANSLATOR = OfflineTranslator(model_path=_SETTINGS.get_model_path())

WORDLIST_PATH = Path(__file__).with_name("wordlist.txt")  # словарь для автокомплита
AC_MAX_RESULTS = 10  # максимум подсказок в выпадающем списке
AC_MIN_PREFIX = 3    # popup показываем только с префикса длиннее 2 символов
AC_ROW_H = 32        # высота строки в списке
_WORD_RE = re.compile(r"[\w'-]+$")  # текущее слово перед курсором

# Разбиение на предложения/абзацы (та же логика, что в translator.py),
# но с сохранением реальных смещений в тексте — для клика по предложению.
# Граница предложения: после .!?… пробел + заглавная (латиница/кириллица).
_SENT_RE = re.compile(r"(?<=[.!?…])\s+(?=[A-Z0-9\"'(\u0400-\u04ff])")
_PARA_RE = re.compile(r"\n\s*\n")


def _line_offsets(text: str) -> list[int]:
    """Абсолютные позиции начала каждой строки (первая — 0)."""
    offs = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            offs.append(i + 1)
    return offs


def _off_to_tk(offs: list[int], off: int) -> str:
    """Абсолютный смещение -> Tk-индекс 'line.col'."""
    import bisect
    line = bisect.bisect_right(offs, off) - 1
    line = max(0, min(line, len(offs) - 1))
    return f"{line + 1}.{off - offs[line]}"


def _tk_to_off(offs: list[int], idx: str) -> int:
    """Tk-индекс 'line.col' -> абсолютное смещение."""
    try:
        line_s, col_s = idx.split(".", 1)
        return offs[int(line_s) - 1] + int(col_s)
    except (ValueError, IndexError):
        return 0


def _split_segments(text: str):
    """Разбивает текст на сегменты (абзацы -> предложения) как в translator.py.

    Возвращает (segments, line_offsets), где segments — список
    (tk_start, tk_end, off_start, off_end) по порядку без пересечений."""
    offs = _line_offsets(text)
    n = len(text)
    # Границы абзацев.
    para_starts = [0] + [m.start() for m in _PARA_RE.finditer(text)]
    para_ends = para_starts[1:] + [n]
    segments: list[tuple[str, str, int, int]] = []
    for ps, pe in zip(para_starts, para_ends):
        para = text[ps:pe]
        bounds = [0]
        for m in _SENT_RE.finditer(para):
            bounds.append(m.start())
        for j in range(len(bounds)):
            s = bounds[j]
            e = bounds[j + 1] if j + 1 < len(bounds) else len(para)
            if text[ps + s:ps + e].strip():
                cs, ce = ps + s, ps + e
                segments.append((_off_to_tk(offs, cs), _off_to_tk(offs, ce), cs, ce))
    return segments, offs

# keysym -> «дружельное» имя модификатора (режим записи хоткея).
_HK_KEY_MAP = {
    "Control_L": "ctrl", "Control_R": "ctrl",
    "Alt_L": "alt", "Alt_R": "alt",
    "Shift_L": "shift", "Shift_R": "shift",
    "Super_L": "cmd", "Super_R": "cmd",
    "Meta_L": "cmd", "Meta_R": "cmd",
}
_HK_MODIFIER_NAMES = {"ctrl", "alt", "shift", "cmd"}
# Клавиши, игнорируемые в режиме записи (навигация/сервисные).
_HK_IGNORE_KEYS = {
    "Up", "Down", "Left", "Right", "Tab", "ISO_Left_Tab", "Return",
    "BackSpace", "Prior", "Next", "Home", "End",
    "Caps_Lock", "Num_Lock", "Scroll_Lock", "Menu",
}

# --- Catppuccin Mocha ----------------------------------------------------- #
BASE = "#1e1e2e"      # фон окна
SURFACE = "#2a2a3c"   # поля
SURFACE0 = "#313244"  # кнопки (обычное состояние)
SURFACE2 = "#45475A"  # hover / нейтральный бейдж
BORDER = "#45475a"    # обводка полей ввода/вывода
ACCENT = "#89b4fa"    # акцент (blue)
ACCENT_HOVER = "#74a8fc"
TEXT = "#cdd6f4"
MUTED = "#a6adc8"
GREEN = "#a6e3a1"     # success / glossary
RED = "#f38ba8"       # error
ORANGE = "#fab387"    # режим записи хоткея

CORNER = 12           # скругления


def _is_short(source: str) -> bool:
    """Одно короткое предложение/фраза (без абзацев и множественных точек)."""
    return len(source) <= 300 and "\n\n" not in source and source.count(". ") < 2


def translate_text(source: str) -> tuple[str, str]:
    """Точка входа для перевода: английская строка -> (перевод, источник).

    Короткий вход — через translate() (даёт источник: nn/glossary),
    длинный — по абзацам/предложениям.
    """
    if _is_short(source):
        result = _TRANSLATOR.translate(source)
        return result.text, result.source
    return _TRANSLATOR.translate_text(source), "nn"


class TranslatorGUI:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._timer_id: int | str | None = None  # id отложенного перевода
        self._pending = False                    # идёт ли сейчас перевод
        self._request_id = 0                     # устаревшие результаты игнорируем
        self._sent_cache: dict[str, str] = {}    # инкрементальный кэш по предложениям
        self._scroll_sync = False                # защита от рекурсии синхронной прокрутки
        # --- подсветка предложений при клике ---------------------------- #
        self._hl_src = "highlight_src"           # tag для in_text
        self._hl_dst = "highlight_dst"          # tag для out_text
        # --- настройки -------------------------------------------------- #
        self.settings = _SETTINGS
        ctk.set_appearance_mode(self.settings.get("theme", "dark"))
        # --- автокомплит ------------------------------------------------ #
        self.wordlist: list[str] = []            # словарь для подсказок
        self._ac_matches: list[tuple[str, ctk.CTkCanvas, dict]] = []  # (слово, canvas, item ids)
        self._ac_selected: int = -1              # выделенный пункт (подсветка)
        self._ac_prefix: str = ""                # набранный префикс (цветом в списке)
        self._ac_anchor: str | None = None       # позиция начала текущего слова
        self._ac_popup: ctk.CTkToplevel | None = None  # выпадающий список
        # --- диалог настроек + режим записи хоткея ---------------------- #
        self.settings_window: ctk.CTkToplevel | None = None
        self._recording = False                  # идёт ли запись хоткея
        self._hk_win: ctk.CTkToplevel | None = None   # окно настроек (bind'и)
        self._hk_entry: ctk.CTkEntry | None = None    # поле хоткея
        self._hk_orig: str = ""                  # значение до записи (отмена)
        self._hk_result: str | None = None       # итог записи (None = отклонено)
        self._hk_mods: list[str] = []            # модификаторы (порядок нажатия)
        self._hk_main: str | None = None         # «главная» клавиша (последняя)
        self._hk_main_held: bool = False         # зажат ли главный ключ
        self._hk_pending: str | None = None      # снимок комбинации при нажатии
        # --- системный трей ---------------------------------------------- #
        self.tray_icon = None                    # pystray.Icon (Linux: X11)

        self.hotkey_agent = HotkeyAgent(_TRANSLATOR, self.settings)

        root.title("Offline Translate")
        root.geometry("850x560")
        root.minsize(640, 420)
        root.configure(fg_color=BASE, text_color=TEXT)
        root.bind("<Escape>", self._on_escape)
        root.bind("<Button-1>", self._on_popup_outside_click, add="+")
        self.root.bind("<Control-x>", self._on_cut)
        # Ctrl+Enter — немедленный перевод (без дебаунса), Ctrl+L — очистка.
        # bind_all: срабатывает, откуда бы фокус ни был (поле, кнопки и т.д.).
        # Имена событий — как требует Tk: <Control-Return>, <Control-l>.
        self.root.bind_all("<Control-Return>", self._on_translate_now)
        self.root.bind_all("<Control-l>", self._on_clear_hotkey)
        # Ctrl+C — только в поле перевода (bind, не bind_all: не глушим
        # системный Copy в остальных местах). Ctrl+, — настройки.
        self.root.bind("<Control-c>", self._on_copy_hotkey)
        self.root.bind_all("<Control-comma>", self._on_open_settings_hotkey)
        # Крестик — сворачивание в трей (полный выход — из меню трея).
        root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        self._build()
        self._load_wordlist()
        self._ac_popup = self._create_popup()
        self.in_text.bind("<<Modified>>", self._on_text_modified)
        self.in_text.bind("<KeyRelease>", self._on_key_release)
        self.in_text.bind("<FocusOut>", lambda _e: self._hide_popup())
        # Ctrl+A — отдельно на каждом поле (не bind_all): выделяется
        # только то, по которому нажата комбинация.
        self.in_text.bind("<Control-a>", self._on_select_all, add="+")
        self.out_text.bind("<Control-a>", self._on_select_all, add="+")
        self.hotkey_agent.start()
        root.after(100, self._poll_results)
        self._setup_tray()

    def _on_close(self) -> None:
        """Единая точка закрытия: агент + трей-иконка + деструкция окна.

        Стоп трея — в фоновом потоке: pystray.stop() может ждать
        завершение setup-потока (до SETUP_THREAD_TIMEOUT), и UI
        на это время не должен застревать."""
        self.hotkey_agent.stop()
        icon = self.tray_icon
        self.tray_icon = None
        if icon is not None:
            threading.Thread(
                target=self._stop_tray, args=(icon,), daemon=True
            ).start()
        self.root.destroy()

    @staticmethod
    def _stop_tray(icon: "pystray.Icon") -> None:
        try:
            icon.stop()
        except Exception:  # noqa: BLE001 — трей уже остановлен
            pass

    # ------------------------------------------------------------------ #
    #  Системный трей (Linux/X11 через pystray)                          #
    # ------------------------------------------------------------------ #
    def _create_icon(self) -> "Image.Image":
        """Создаёт иконку программно (64x64): акцентный круг + буква T."""
        size = 64
        image = Image.new("RGB", (size, size), color=(88, 101, 126))
        draw = ImageDraw.Draw(image)
        draw.ellipse([10, 10, 54, 54], fill=(137, 180, 250))
        draw.text((24, 18), "T", fill=(30, 30, 46))
        return image

    def _setup_tray(self) -> None:
        """Инициализирует системный трей (отдельный поток pystray).

        Если трей недоступен (нет системного трей-хоста), GUI продолжает
        работать без иконки — ошибки только логируются."""
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Открыть", lambda icon, item: self.root.after(0, self._on_tray_show), default=True),
                pystray.MenuItem("Настройки", self._on_tray_settings),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выйти", self._on_tray_quit),
            )
            self.tray_icon = pystray.Icon(
                "OfflineTranslate", self._create_icon(), "Offline Translate", menu,
                on_click=self._on_tray_click,
            )
            # Не run_detached(): он поднимает non-daemon-поток, и при выходе
            # из процесса Python ждёт его в threading._shutdown. Свой
            # daemon-поток: при выходе (в т.ч. в тестах) не блокирует.
            threading.Thread(
                target=self.tray_icon.run, daemon=True, name="tray"
            ).start()
        except Exception:  # noqa: BLE001 — нет X-трея/прав
            import logging
            logging.getLogger("gui").exception("Трей недоступен — работа без иконки")
            self.tray_icon = None

    def _on_tray_click(self, icon, button, pressed):
        print(f"DEBUG: tray click button={button} pressed={pressed}")
        if not pressed:
            return
        if button == pystray.Button.LEFT:
            print("DEBUG: opening window")
            self.root.after(0, self._on_tray_show)

    def _on_tray_show(self, icon=None, item=None) -> None:
        """Клик по трею «Открыть»: показать окно.

        Вызывается из потока pystray — через after(0) в главный поток
        (Tk не потокобезопасен)."""
        self.root.after(0, self._show_from_tray)

    def _show_from_tray(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self._close_settings()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_tray_settings(self, icon=None, item=None) -> None:
        """Открыть настройки из трея."""
        def _do_settings():
            if not self.root.winfo_viewable():
                self.root.deiconify()
                self.root.lift()
                # Ждём пока окно появится
                self.root.after(300, self._open_settings)
            else:
                self._open_settings()
        self.root.after(0, _do_settings)

    def _on_tray_quit(self, icon=None, item=None) -> None:
        """Меню «Выйти»: полный выход (стоп трея сделаем в _on_close)."""
        self.root.after(0, self._on_close)

    def _on_window_close(self) -> None:
        """Крестик — сворачивание в трей (полный выход — из меню трея)."""
        self.root.withdraw()
        import shutil
        import subprocess
        if shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", "Offline Translate", "Свёрнуто в трей"],
                check=False,
            )

    def _load_wordlist(self) -> None:
        """Загружает wordlist.txt (одно слово на строку).

        Если файла нет — warning в статус-баре, автокомплит просто
        остаётся пустым, приложение не падает."""
        try:
            self.wordlist = [
                line.strip()
                for line in WORDLIST_PATH.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except OSError:
            self._set_status("wordlist.txt не найден — автокомплит выключен", RED)
            self.wordlist = []

    # ------------------------------------------------------------------ #
    #  Верстка                                                           #
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = self.root
        root.grid_columnconfigure(0, weight=1)
        root.grid_columnconfigure(1, weight=0)   # кнопка настроек
        root.grid_rowconfigure(0, weight=0)   # заголовок
        root.grid_rowconfigure(1, weight=1, minsize=140)   # ввод
        root.grid_rowconfigure(2, weight=0)   # панель кнопок
        root.grid_rowconfigure(3, weight=1, minsize=180)   # вывод
        root.grid_rowconfigure(4, weight=0)   # статус-бар

        # --- Строка 0: заголовок + кнопка настроек ---------------------- #
        title = ctk.CTkLabel(
            root,
            text="🌐  Offline Translate",
            font=ctk.CTkFont(size=17, weight="bold"),
            text_color=TEXT,
        )
        title.grid(row=0, column=0, sticky="w", padx=20, pady=(14, 8))

        self.btn_settings = ctk.CTkButton(
            root,
            text="⚙️ Настройки",
            width=120,
            height=32,
            corner_radius=10,
            fg_color=SURFACE0,
            hover_color=SURFACE2,
            text_color=TEXT,
            command=self._open_settings,
        )
        self.btn_settings.grid(row=0, column=1, sticky="e", padx=20, pady=(14, 8))

        # --- Строка 1: вводный текст ------------------------------------- #
        self.in_text = ctk.CTkTextbox(
            root,
            fg_color=SURFACE,
            border_color=BORDER,
            border_width=1,
            corner_radius=CORNER,
            text_color=TEXT,
            wrap="word",
            activate_scrollbars=True,
            height=140,
        )
        self.in_text.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=16, pady=4)

        # --- Строка 2: панель Clear | бейдж | Copy ------------------------ #
        bar = ctk.CTkFrame(root, fg_color=BASE)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=6)
        bar.grid_columnconfigure(1, weight=1)

        self.btn_clear = ctk.CTkButton(
            bar,
            text="Clear",
            width=90,
            height=34,
            corner_radius=10,
            fg_color=SURFACE0,
            hover_color=SURFACE2,
            text_color=TEXT,
            command=self._on_clear,
        )
        self.btn_clear.grid(row=0, column=0, sticky="w")

        self.badge = ctk.CTkLabel(
            bar,
            text="—",
            width=110,
            height=26,
            corner_radius=8,
            fg_color=SURFACE2,
            text_color=MUTED,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.badge.grid(row=0, column=1)  # без sticky = по центру ячейки

        self.btn_copy = ctk.CTkButton(
            bar,
            text="Copy",
            width=90,
            height=34,
            corner_radius=10,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color=BASE,
            command=self._on_copy,
        )
        self.btn_copy.grid(row=0, column=2, sticky="e")

        # --- Строка 3: вывод (read-only) --------------------------------- #
        self.out_text = ctk.CTkTextbox(
            root,
            fg_color=SURFACE,
            border_color=BORDER,
            border_width=1,
            corner_radius=CORNER,
            text_color=TEXT,
            state="disabled",
            wrap="word",
            activate_scrollbars=True,
            height=180,
        )
        self.out_text.grid(row=3, column=0, columnspan=2, sticky="nsew", padx=16, pady=4)

        # --- Строка 4: статус-бар ----------------------------------------- #
        status_bar = ctk.CTkFrame(root, fg_color=BASE, corner_radius=CORNER)
        status_bar.grid(row=4, column=0, columnspan=2, sticky="ew", padx=16, pady=(4, 14))
        status_bar.grid_columnconfigure(1, weight=1)

        self.status = ctk.CTkLabel(
            status_bar, text="Готово", text_color=MUTED,
            font=ctk.CTkFont(size=12),
        )
        self.status.grid(row=0, column=0, sticky="w", padx=12, pady=5)

        self.time_label = ctk.CTkLabel(
            status_bar, text="", text_color=MUTED,
            font=ctk.CTkFont(size=12),
        )
        self.time_label.grid(row=0, column=2, sticky="e", padx=12, pady=5)

        self._setup_scroll_sync()
        self._setup_highlight()

    # ------------------------------------------------------------------ #
    #  Подсветка предложений при клике                                    #
    # ------------------------------------------------------------------ #
    def _setup_highlight(self) -> None:
        """Настройки тегов и клик-обработчик подсветки предложений."""
        self.in_text.tag_config(self._hl_src, background="#4a4a6a")
        self.out_text.tag_config(self._hl_dst, background="#4a4a6a")
        # Клик по оригиналу. add="+" обязателен (требование CTkTextbox:
        # нельзя сбивать внутренние колбэки).
        self.in_text.bind("<Button-1>", self._on_click_sentence)

    def _clear_highlight(self) -> None:
        self.in_text.tag_remove(self._hl_src, "1.0", "end")
        self.out_text.tag_remove(self._hl_dst, "1.0", "end")

    def _on_click_sentence(self, event: tk.Event) -> None:
        """Клик по in_text: подсвечивает предложение и его перевод.

        Сегменты (абзацы/предложения) пересчитываются на лету из текущего
        текста обоих полей — кэш не нужен, всегда актуально. Соответствие
        src[i] <-> dst[i] — по порядку (как в translator.py)."""
        src_text = self.in_text.get("1.0", "end-1c")
        dst_text = self.out_text.get("1.0", "end-1c")
        src_segs, src_offs = _split_segments(src_text)
        dst_segs, _dst_offs = _split_segments(dst_text)

        # Позиция клика в исходном тексте.
        idx = self.in_text.index(f"@{event.x},{event.y}")
        pos = _tk_to_off(src_offs, idx)
        hi = None
        for i, (_ts, _te, cs, ce) in enumerate(src_segs):
            if cs <= pos < ce:
                hi = i
                break

        self._clear_highlight()
        if hi is None:
            return
        ts, te, _cs, _ce = src_segs[hi]
        self.in_text.tag_add(self._hl_src, ts, te)
        if hi < len(dst_segs):
            dts, dte, _dcs, _dce = dst_segs[hi]
            self.out_text.tag_add(self._hl_dst, dts, dte)
        # Прокрутка обоих полей к подсвеченному предложению.
        try:
            self.in_text.see(ts)
            if hi < len(dst_segs):
                self.out_text.see(dst_segs[hi][0])
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    #  Синхронная прокрутка original <-> перевод                          #
    # ------------------------------------------------------------------ #
    def _setup_scroll_sync(self) -> None:
        """Связывает вертикальную прокрутку in_text и out_text.

        Единая точка схода всех видов скролла (колесо по тексту, колесо по
        scrollbar'у, перетаскивание ползунка, клавиши) — yscrollcommand
        текстового виджета. Оборачиваем его: любое смещение зеркалируется
        на парном поле через yview_moveto(fraction). Флаг _scroll_sync
        гасит обратную волну (скролл одного поля не триггерит второе
        повторно)."""
        self._sync_pairs = [
            (self.in_text, self.out_text),
            (self.out_text, self.in_text),
        ]
        # Колесо на теле текста (голый tkinter.Text колесом не скроллится;
        # CTkScrollbar уже обрабатывает колесо на своём canvas).
        for box in (self.in_text, self.out_text):
            box._textbox.bind("<MouseWheel>", self._on_text_wheel, add="+")
            box._textbox.bind("<Button-4>", self._on_text_wheel_linux, add="+")
            box._textbox.bind("<Button-5>", self._on_text_wheel_linux, add="+")
        # Оборачиваем yscrollcommand: сохраняем оригинал (CTkScrollbar.set),
        # дёргаем синхронизацию на каждом изменении положения.
        for box, partner in self._sync_pairs:
            original_set = box._y_scrollbar.set
            partner_box = partner

            def _synced_set(start, end, _box=box, _partner=partner_box, _orig=original_set):
                _orig(start, end)
                self._mirror_scroll(_box, _partner)

            box._y_scrollbar.set = _synced_set
            box._textbox.configure(yscrollcommand=_synced_set)

    def _mirror_scroll(self, src, dst) -> None:
        """Меняет dst на то же вертикальное положение, что у src."""
        if self._scroll_sync:
            return
        try:
            fraction = float(src.yview()[0])
        except (tk.TclError, IndexError, TypeError):
            return
        self._scroll_sync = True
        try:
            dst.yview_moveto(fraction)
        except tk.TclError:
            pass
        finally:
            self._scroll_sync = False

    def _on_text_wheel(self, event: tk.Event) -> str:
        """Колесо (Windows/macOS): скроллит текст под курсором."""
        delta = -1 if event.delta > 0 else 1
        self._scroll_box(event.widget, delta)
        return "break"

    def _on_text_wheel_linux(self, event: tk.Event) -> str:
        """Колесо (Linux X11: Button-4/5)."""
        delta = -1 if event.num == 4 else 1
        self._scroll_box(event.widget, delta)
        return "break"

    def _scroll_box(self, text_widget, delta: int) -> None:
        # text_widget — внутренний tkinter.Text; скроллим его, что
        # автоматически вызовет yscrollcommand -> синхронизация.
        text_widget.yview_scroll(delta, "units")

    # ------------------------------------------------------------------ #
    #  Автокомплит                                                       #
    # ------------------------------------------------------------------ #
    # Клавиши, не вызывающие поиск (навигация/сервисные).
    _NAV_KEYS = ("Up", "Down", "Tab", "Return", "Escape", "Prior", "Next",
                 "Home", "End", "Shift_L", "Shift_R", "Control_L",
                 "Control_R", "Alt_L", "Alt_R")

    def _create_popup(self) -> ctk.CTkToplevel:
        """Выпадающий список: безрамочное toplevel, поверх всех окон.

        Фон окна — SURFACE (CTkToplevel 6.0 не принимает transparent),
        скругления и обводка — у внутреннего CTkFrame, поэтому по краям
        окна видны те же цвета, что и в контуре панели. Размер
        фиксируем через geometry (минимально возможный для
        window-виджетов CustomTkinter)."""
        popup = ctk.CTkToplevel(self.root)
        popup.attributes("-topmost", True)
        popup.configure(fg_color=SURFACE)  # CTkToplevel 6.0 без transparent
        popup.withdraw()
        try:
            popup.overrideredirect(True)
        except tk.TclError:
            pass

        # Панель заполняет всё окно (сквозь углы окна видно SURFACE —
        # тот же цвет, что и у панели, поэтому граница не видна).
        self._ac_frame = ctk.CTkFrame(
            popup, fg_color=SURFACE,
            corner_radius=8, border_width=1, border_color=BORDER,
        )
        self._ac_frame.grid(row=0, column=0, sticky="nsew")

        popup.grid_rowconfigure(0, weight=1)
        popup.grid_columnconfigure(0, weight=1)

        popup.bind("<Escape>", self._on_popup_escape)
        popup.bind("<Return>", self._on_popup_accept)
        popup.bind("<Tab>", self._on_popup_accept)
        popup.bind("<Down>", lambda _e: self._ac_select(self._ac_selected + 1))
        popup.bind("<Up>", lambda _e: self._ac_select(self._ac_selected - 1))
        return popup

    def _on_popup_escape(self, _event: tk.Event) -> str:
        """Escape, пока фокус на popup-окне: закрыть его.

        "break" обязателен: иначе событие дошло бы и до root-биндинга
        _on_escape, который увидел бы уже закрытый popup и закрыл бы
        всё окно."""
        self._hide_popup()
        self.in_text.focus_set()
        return "break"

    def _on_popup_accept(self, _event: tk.Event) -> None:
        """Вставляет выделенное, если фокус почему-то на самом popup."""
        if self._ac_popup.state() == "normal" and self._ac_matches \
                and 0 <= self._ac_selected < len(self._ac_matches):
            self._insert_completion(self._ac_selected)

    def _hide_popup(self, _event=None) -> None:
        if self._ac_popup.winfo_exists():
            self._ac_popup.withdraw()
        self._ac_selected = -1
        self._ac_anchor = None

    def _ac_select(self, idx: int) -> None:
        """Подсветка выделенной строки (индекс с закручиванием)."""
        n = len(self._ac_matches)
        if n == 0:
            return
        self._ac_selected = idx % n
        for i, (_word, canvas, items) in enumerate(self._ac_matches):
            selected = i == self._ac_selected
            canvas.itemconfig(items["rect"], fill=SURFACE2 if selected else SURFACE)
            canvas.itemconfig(items["rest"], fill=TEXT if selected else MUTED)

    def _on_key_release(self, _event: tk.Event) -> None:
        """Наблюдательный обработчик: стандартное поведение Textbox
        (ввод, стрелки, перенос на Enter) работает как обычно, мы лишь
        реагируем сверху.

        Popup открыт:  ↑/↓ — навигация, Tab/Enter — вставить выделенное,
                       остальное — новый поиск (закрывает popup,
                       если совпадений нет). Escape — в _on_escape.
        Popup закрыт:  любая «печатная» клавиша запускает поиск
                       (открывает popup, если совпадения есть)."""
        popup = self._ac_popup
        key = _event.keysym
        # Escape намеренно НЕ обрабатывается здесь: нажатие Escape
        # ловит root-биндинг (_on_escape), который закрывает popup или окно.
        if popup.state() == "normal":
            if key in ("Up", "Down"):
                self._ac_select(self._ac_selected + (1 if key == "Down" else -1))
            elif key in ("Tab", "Return") and self._ac_matches:
                self._insert_completion(self._ac_selected)
            elif key not in self._NAV_KEYS:
                self._search_completions()
        elif key not in self._NAV_KEYS:
            self._search_completions()

    def _on_popup_outside_click(self, _event: tk.Event) -> None:
        """Клик вне dropdown и вне поля ввода закрывает подсказки.

        Привязан через root.bind с add="+": срабатывает только на
        кликах по окну root; клики по popup-окну сюда не приходят."""
        if self._ac_popup.state() != "normal":
            return
        widget = _event.widget
        while widget is not None and widget is not self.root:
            if widget is self.in_text:
                return  # клик по полю ввода — оставляем popup открытым
            widget = getattr(widget, "master", None)
        self._hide_popup()

    def _search_completions(self) -> None:
        """Ищет совпадения с текущим словом и обновляет popup.

        Правила: пустое слово или единственный кандидат, совпадающий с
        уже набранным, — popup скрыт; иначе показывает до AC_MAX_RESULTS.
        Якорь хранится в абсолютном индексе ("2.7"), чтобы не потерять
        позицию при переносе строки/изменении текста вокруг каретки."""
        if not self.wordlist:
            self._hide_popup()
            return
        line_prefix = self.in_text.get("insert linestart", "insert")
        match = _WORD_RE.search(line_prefix)
        if not match or len(match.group(0)) < AC_MIN_PREFIX:
            self._hide_popup()
            return
        prefix = match.group(0)
        lowered = prefix.lower()
        candidates = [
            word for word in self.wordlist if word.lower().startswith(lowered)
        ][:AC_MAX_RESULTS]
        if not candidates or len(candidates) == 1 and candidates[0].lower() == lowered:
            self._hide_popup()
            return
        self._ac_prefix = prefix
        self._ac_anchor = self.in_text.index(f"insert -{len(prefix)}c")
        self._ac_selected = 0
        self._show_completions(candidates)

    def _show_completions(self, words: list[str]) -> None:
        """Перерисовывает список и позиционирует popup под полем ввода
        (по rootx/rooty — так же, как DropdownMenu у CTkComboBox).

        Каждая строка — canvas: набранный префикс ACCENT (акцент), остаток
        слова — TEXT/MUTED; выделение — скруглённым прямоугольником."""
        frame = self._ac_frame
        for child in frame.winfo_children():
            child.destroy()
        font = ctk.CTkFont(size=13)
        prefix = self._ac_prefix
        width = max(self.in_text.winfo_width(), 180)
        self._ac_matches = []
        for i, word in enumerate(words):
            canvas = ctk.CTkCanvas(
                frame, width=width, height=AC_ROW_H,
                highlightthickness=0, bg=SURFACE,
            )
            canvas.grid(row=i, column=0, sticky="ew", padx=2, pady=1)
            # Подложка строки: SURFACE (норма) / SURFACE2 (выделено).
            rect = canvas.create_rectangle(
                2, 2, width - 3, AC_ROW_H - 3,
                fill=SURFACE2 if i == 0 else SURFACE, outline="")
            # Два фрагмента текста: префикс цветом, остаток нейтральным.
            text_items = {
                "rect": rect,
                "prefix": canvas.create_text(
                    10, AC_ROW_H / 2, anchor="w",
                    text=word[:len(prefix)],
                    font=font.create_scaled_tuple(
                        ctk.ScalingTracker.widget_scaling),
                    fill=ACCENT),
                "rest": canvas.create_text(
                    10 + font.measure(word[:len(prefix)]), AC_ROW_H / 2,
                    anchor="w", text=word[len(prefix):],
                    font=font.create_scaled_tuple(
                        ctk.ScalingTracker.widget_scaling),
                    fill=TEXT if i == 0 else MUTED),
            }
            canvas.bind("<Enter>", lambda _e, idx=i: self._ac_select(idx))
            canvas.bind("<Button-1>", lambda _e, idx=i: self._insert_completion(idx))
            self._ac_matches.append((word, canvas, text_items))

        self._ac_popup.update_idletasks()
        height = frame.winfo_reqheight() + 2
        x = self.in_text.winfo_rootx()
        y = self.in_text.winfo_rooty() + self.in_text.winfo_height() + 2
        self._ac_popup.geometry(f"{width}x{height}+{x}+{y}")
        self._ac_popup.deiconify()
        # Каретка — обратно в поле ввода (для платформ, где overrideredirect
        # окно всё-таки забирает фокус).
        self.in_text.focus_set()

    def _insert_completion(self, idx: int) -> None:
        """Замещает текущее слово выбранным и закрывает popup.

        После вставки каретка стоит в конце слова: следующий ввод
        продолжит расширять его, Enter — вставить перенос строки."""
        word, _canvas, _items = self._ac_matches[idx]
        if self._ac_anchor is None:
            return
        self.in_text.delete(self._ac_anchor, "insert")
        self.in_text.insert(self._ac_anchor, word)
        self._ac_anchor = None
        self._hide_popup()

    def _on_escape(self, _event: tk.Event) -> None:
        """Escape сначала закрывает popup; только если он был закрыт —
        закрывает окно. "break" возвращаем всегда: root.bind срабатывает
        до обработчиков виджетов, без него закрытие окна дублировалось бы."""
        if self._ac_popup.state() == "normal":
            self._hide_popup()
        else:
            self._on_close()
        return "break"

    # ------------------------------------------------------------------ #
    #  Обработчики                                                       #
    # ------------------------------------------------------------------ #
    def _set_output(self, text: str) -> None:
        self.out_text.configure(state="normal")
        self.out_text.delete("1.0", "end")
        self.out_text.insert("1.0", text)
        self.out_text.configure(state="disabled")

    def _set_status(self, text: str, color: str = MUTED) -> None:
        self.status.configure(text=text, text_color=color)

    def _set_time(self, elapsed: float | None) -> None:
        self.time_label.configure(text=f"{elapsed:.2f} с" if elapsed is not None else "")

    def _set_badge(self, kind: str | None) -> None:
        if kind == "glossary":
            self.badge.configure(text="GLOSSARY", fg_color=GREEN, text_color=BASE)
        elif kind == "nn":
            self.badge.configure(text="NN", fg_color=ACCENT, text_color=BASE)
        else:
            self.badge.configure(text="—", fg_color=SURFACE2, text_color=MUTED)

    def _on_text_modified(self, _event=None) -> None:
        """Дебаунс: любое изменение текста сбрасывает и запускает таймер
        на ``debounce_sec`` из настроек. Новые нажатия отменяют
        предыдущий через after_cancel."""
        self.in_text.edit_modified(False)

        if self._timer_id is not None:
            self.root.after_cancel(self._timer_id)
        delay_ms = int(self.settings.get("debounce_sec", 1.5) * 1000)
        self._timer_id = self.root.after(delay_ms, self._start_translate)

    def _start_translate(self) -> None:
        """Срабатывает через ``debounce_sec`` после последнего ввода."""
        self._timer_id = None
        source = self.in_text.get("1.0", "end").strip()
        if not source:
            self._set_output("")
            self._set_status("Готово")
            self._set_badge(None)
            self._set_time(None)
            return
        # Лимит длины из настроек (0 = без лимита).
        max_len = int(self.settings.get("max_text_length", 5000))
        if max_len > 0 and len(source) > max_len:
            self._set_status(f"Текст слишком длинный (макс. {max_len} символов)", RED)
            return
        if self._pending:  # предыдущий перевод ещё идёт — ждём его
            return
        self._pending = True
        self._request_id += 1
        req_id = self._request_id
        self._set_status("Перевод…", ACCENT)
        threading.Thread(target=self._worker, args=(source, req_id),
                         daemon=True).start()

    def _on_clear(self) -> None:
        # Отменяем отложенный автоперевод и инвалидируем запущенные потоки
        if self._timer_id is not None:
            self.root.after_cancel(self._timer_id)
            self._timer_id = None
        self._request_id += 1
        self._pending = False
        self.in_text.edit_modified(False)
        self.in_text.delete("1.0", "end")
        self._clear_highlight()
        self._set_output("")
        self._set_badge(None)
        self._set_time(None)
        self._set_status("Готово")

    def _on_copy(self) -> None:
        text = self.out_text.get("1.0", "end-1c").strip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_status("Скопировано в буфер обмена", GREEN)

    def _on_select_all(self, event) -> str:
        """Ctrl+A: выделяет всё в том поле, по которому нажата
        комбинация (bind на каждом поле, не bind_all)."""
        widget = event.widget
        if widget == self.in_text._textbox or "in_text" in str(widget):
            self.in_text.tag_add('sel', '1.0', 'end')
        elif widget == self.out_text._textbox or "out_text" in str(widget):
            self.out_text.tag_add('sel', '1.0', 'end')
        return "break"

    def _on_cut(self, event) -> None:
        focused = self.root.focus_get()
        if str(self.in_text) in str(focused):
            try:
                selected = self.in_text.get('sel.first', 'sel.last')
                self.in_text.delete('sel.first', 'sel.last')
                import pyperclip
                pyperclip.copy(selected)
            except:
                pass

    def _on_translate_now(self, event) -> str:
        """Ctrl+Enter: немедленный перевод, без ожидания дебаунса."""
        if self._timer_id is not None:
            self.root.after_cancel(self._timer_id)
            self._timer_id = None
        self._start_translate()
        # "break": не вставляем перенос строки в поле ввода.
        return "break"

    def _on_clear_hotkey(self, event) -> str:
        """Ctrl+L: очистка полей (тот же код, что кнопка Clear)."""
        self._on_clear()
        # "break": не вставляем букву 'l' в поле ввода.
        return "break"

    def _on_copy_hotkey(self, event) -> str | None:
        """Ctrl+C: копирует перевод, но только если фокус в out_text.

        bind (не bind_all) на root: биндинг топ-левела входит в bindtags
        всех дочерних виджетов, а для других окон не срабатывает."""
        focused = self.root.focus_get()
        if str(self.out_text) in str(focused):
            import pyperclip
            pyperclip.copy(self.out_text.get("1.0", "end-1c"))
            self._set_status("Перевод скопирован")
            return "break"
        # Фокус не в out_text — не перехватываем: системный Copy работает.

    def _on_open_settings_hotkey(self, event) -> str:
        """Ctrl+, — открыть окно настроек."""
        self._open_settings()
        return "break"

    # ------------------------------------------------------------------ #
    #  Настройки                                                         #
    # ------------------------------------------------------------------ #
    def _open_settings(self) -> None:
        """Диалог настроек: поля под каждый параметр + Сохранить/Отмена."""
        win = ctk.CTkToplevel(self.root)
        win.title("Настройки")
        win.geometry("580x730")
        win.resizable(False, False)
        # transient: диалог привязан к root — не становится выше других
        # окон (без -topmost), а сворачивается/прикрывается вместе с ним.
        win.transient(self.root)
        win.configure(fg_color=BASE)
        win.update_idletasks()
        self.settings_window = win
        win.protocol("WM_DELETE_WINDOW", self._close_settings)
        # Escape закрывает диалог (bind на toplevel: bind на root сюда
        # не доходит). Во время записи хоткея Escape означает «отменить
        # запись» (_hk_on_key) — диалог не закрываем.
        win.bind("<Escape>", self._on_settings_escape)

        entries: dict[str, ctk.CTkEntry] = {}
        row_font = ctk.CTkFont(size=13)

        # key -> (подпись, вид: entry/bool/choice, [выбор])
        fields = [
            ("hotkey", "Хоткей (pynput, напр. ctrl+alt+t)", "entry"),
            ("source_lang", "Исходный язык", "choice",
             ["en", "ru", "de", "fr", "es", "it", "pl", "cs", "ca", "el", "uk", "tr"]),
            ("target_lang", "Язык перевода", "choice",
             ["en", "ru", "de", "fr", "es", "it", "pl", "cs", "ca", "el", "uk", "tr"]),
            ("theme", "Тема", "choice", ["dark", "light"]),
            ("slow_after_sec", "Сек. до «Перевод...»", "entry"),
            ("debounce_sec", "Дебаунс ввода, сек", "entry"),
            ("notify_timeout", "Таймаут уведомления, сек", "entry"),
            ("max_text_length", "Макс. длина текста", "entry"),
            ("filter_cyrillic", "Фильтр кириллицы в буфере", "bool"),
            ("model_path", "Модель (.argosmodel)", "entry"),
        ]
        # Подсказки под отдельными полями.
        hints = {
            "max_text_length": "Лимит символов для перевода (0 = без лимита)",
            "model_path": "Путь к модели; пусто = автоопределение. Применяется после перезапуска.",
        }

        scroll_frame = ctk.CTkScrollableFrame(
            win,
            width=500,
            height=580,
            fg_color="transparent",
            scrollbar_button_color="#313244",
            scrollbar_button_hover_color="#45475a",
        )
        scroll_frame.pack(fill="both", expand=True, padx=20, pady=10)
        # Внутренний фрейм: все виджеты размещаем здесь, а не в
        # CTkScrollableFrame (pack/sticky в самом scroll-фрейме не
        # поддерживается).
        body = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=0)
        body.grid_columnconfigure(1, weight=1)
        body.grid_columnconfigure(2, weight=0)   # кнопка «Обзор...»

        def read(key: str, kind: str, widget, choices=None):
            if kind == "bool":
                return bool(widget.get())
            if kind == "choice":
                return widget.get()  # CTkOptionMenu.get() возвращает строку
            return widget.get().strip()

        def save_and_apply():
            values = {}
            for key, label, kind, *rest in fields:
                widget, choices = entries[key], (rest[0] if rest else None)
                values[key] = read(key, kind, widget, choices)
            model_changed = values["model_path"] != (self.settings.get("model_path") or "")
            for key, value in values.items():
                self.settings.set(key, value)
            self.settings.save()
            # Применяем: тема мгновенно, хоткей — через restart агента.
            ctk.set_appearance_mode(self.settings.get("theme", "dark"))
            self.hotkey_agent.restart()
            if model_changed:
                self._set_status("Настройки сохранены. Модель изменится после перезапуска приложения", GREEN)
            else:
                self._set_status("Настройки сохранены", GREEN)
            self._stop_hotkey_recording()
            win.destroy()

        def cancel():
            self._stop_hotkey_recording()
            win.destroy()

        row = 0  # номер строки (растёт и на подсказки)
        for field in fields:
            key, label, kind = field[0], field[1], field[2]
            choices = field[3] if len(field) > 3 else None
            ctk.CTkLabel(
                body, text=label, anchor="w",
                font=row_font, text_color=TEXT,
            ).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)

            if kind == "entry":
                e = ctk.CTkEntry(
                    body, width=220 if key == "model_path" else 160,
                    fg_color=SURFACE,
                    border_color=BORDER, border_width=1,
                    corner_radius=8, text_color=TEXT,
                )
                value = self.settings.get(key)
                e.insert(0, "" if value is None else str(value))
            elif kind == "choice":
                e = ctk.CTkOptionMenu(
                    body, width=160, values=choices,
                    fg_color=SURFACE0, button_color=SURFACE2,
                    button_hover_color=SURFACE2,
                    text_color=TEXT,
                )
                e.set(self.settings.get(key))
            else:  # bool
                e = ctk.CTkSwitch(
                    body, text="",
                    onvalue=True, offvalue=False,
                    command=lambda: None,
                )
                e.select() if self.settings.get(key) else e.deselect()
            if key == "model_path":
                # Поле + кнопка «Обзор...» в той же строке.
                e.grid(row=row, column=1, sticky="ew", padx=(0, 6), pady=5)
                ctk.CTkButton(
                    body, text="Обзор...", width=80, height=28,
                    corner_radius=8, fg_color=SURFACE0,
                    hover_color=SURFACE2, text_color=TEXT,
                    command=lambda _e=e: self._pick_model_file(_e),
                ).grid(row=row, column=2, sticky="w", pady=5)
            elif key == "hotkey":
                # Поле + кнопка «🎯 Записать» (нажать комбинацию).
                e.grid(row=row, column=1, sticky="ew", padx=(0, 6), pady=5)
                ctk.CTkButton(
                    body, text="🎯 Записать", width=96, height=28,
                    corner_radius=8, fg_color=SURFACE0,
                    hover_color=SURFACE2, text_color=TEXT,
                    command=lambda _e=e: self._start_hotkey_recording(_e),
                ).grid(row=row, column=2, sticky="w", pady=5)
            else:
                e.grid(row=row, column=1, sticky="e", padx=(0, 0), pady=5)
            entries[key] = e
            row += 1

            if key in hints:
                ctk.CTkLabel(
                    body, text=hints[key], anchor="w",
                    font=ctk.CTkFont(size=11), text_color=MUTED,
                ).grid(row=row, column=0, columnspan=2,
                       sticky="w", padx=2, pady=(0, 6))
                row += 1

        # --- Секция: горячие клавиши (не редактируемый список) ---- #
        ctk.CTkLabel(
            body, text="Горячие клавиши", anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT,
        ).grid(row=row, column=0, columnspan=3, sticky="w", padx=2, pady=(12, 4))
        row += 1

        hk_mono_font = ctk.CTkFont(size=12, family="monospace")
        for combo, desc in [
            ("Ctrl+Enter", "Перевести текст"),
            ("Ctrl+C", "Копировать перевод (в поле перевода)"),
            ("Ctrl+X", "Вырезать текст (в поле ввода)"),
            ("Ctrl+A", "Выделить всё"),
            ("Ctrl+L", "Очистить поля"),
            ("Ctrl+,", "Открыть настройки"),
            ("Escape", "Закрыть настройки"),
        ]:
            ctk.CTkLabel(
                body, text=combo, anchor="w", width=90,
                font=hk_mono_font, text_color=ACCENT,
            ).grid(row=row, column=0, sticky="w", padx=(8, 6), pady=1)
            ctk.CTkLabel(
                body, text=desc, anchor="w",
                font=ctk.CTkFont(size=12), text_color=MUTED,
            ).grid(row=row, column=1, sticky="w", pady=1)
            row += 1

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(side="bottom", fill="x", padx=16, pady=(8, 14))
        btns.grid_columnconfigure(0, weight=1)
        btns.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(
            btns, text="Отмена", height=34, corner_radius=10,
            fg_color=SURFACE0, hover_color=SURFACE2, text_color=TEXT,
            command=cancel,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(
            btns, text="Сохранить", height=34, corner_radius=10,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=BASE,
            command=save_and_apply,
        ).grid(row=0, column=1, sticky="ew", padx=(6, 0))

        win.grab_set()

    def _on_settings_escape(self, _event: tk.Event) -> str:
        """Escape в окне настроек: закрыть диалог.

        Во время записи хоткея Escape означает «отменить запись»
        (_hk_on_key); не закрываем диалог, а лишь останавливаем запись.
        "break": не даём событию дойти до root-биндинга _on_escape."""
        if self._recording:
            self._stop_hotkey_recording()
        else:
            self._close_settings()
        return "break"

    def _close_settings(self) -> None:
        """Закрытие диалога (кнопка X): останавливает запись хоткея."""
        self._stop_hotkey_recording()
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.destroy()
        self.settings_window = None

    # ------------------------------------------------------------------ #
    #  Диалог выбора модели + запись хоткея                              #
    # ------------------------------------------------------------------ #
    def _pick_model_file(self, entry: ctk.CTkEntry) -> None:
        """Файловый диалог выбора .argosmodel; путь ставится в поле.

        Окно настроек на время диалога — topmost + grab, чтобы filedialog
        открывался поверх, а не под ним (grab освобождаем в finally)."""
        win = self.settings_window
        raw = entry.get().strip()
        initial = Path(raw) if raw else self.settings.get_model_path()
        initialdir = str(initial.parent) if initial.parent.exists() else str(Path.home())
        if win is not None:
            try:
                win.attributes("-topmost", True)
                win.grab_set()
                win.focus_force()
            except tk.TclError:
                pass
        try:
            path = filedialog.askopenfilename(
                parent=win,
                title="Выбрать модель (.argosmodel)",
                filetypes=[("Argos модель", "*.argosmodel"), ("Все файлы", "*.*")],
                initialdir=initialdir,
                initialfile=initial.name,
            )
        finally:
            if win is not None:
                try:
                    win.grab_release()
                    win.attributes("-topmost", False)
                except tk.TclError:
                    pass
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)

    # --- Запись хоткея -------------------------------------------------- #
    def _start_hotkey_recording(self, entry: ctk.CTkEntry) -> None:
        """Переводит поле хоткея в режим записи: ждёт нажатия комбинации."""
        if self._recording:
            return  # уже записываем
        self._recording = True
        self._hk_entry = entry
        self._hk_win = self.settings_window
        self._hk_orig = entry.get().strip()
        self._hk_mods: list[str] = []      # модификаторы (порядок нажатия)
        self._hk_main: str | None = None   # «главная» клавиша (последняя)
        self._hk_main_held = False         # зажат ли сейчас главный ключ
        self._hk_pending: str | None = None  # снимок комбинации при нажатии главной
        self._hk_result: str | None = None   # итог (после валидации, None = нет)
        entry.configure(fg_color=ORANGE, text_color=BASE)
        self._hk_fill("Нажмите сочетание...")
        win = self._hk_win
        if win is not None:
            # Топ-левел диалога входит в binding-теги всех его дочерних
            # виджетов — клавиши ловим здесь, откуда бы фокус ни был.
            win.bind("<Key>", self._hk_on_key)
            win.bind("<KeyRelease>", self._hk_on_release)
            entry.focus_set()

    def _stop_hotkey_recording(self) -> None:
        """Возвращает поле в обычный вид и снимает обработчики."""
        if not self._recording:
            return
        self._recording = False
        entry = self._hk_entry
        if entry is not None:
            entry.configure(fg_color=SURFACE, text_color=TEXT, border_color=BORDER)
            value = self._hk_result if self._hk_result is not None else self._hk_orig
            self._hk_fill(value)
        if self._hk_win is not None:
            self._hk_win.unbind("<Key>")
            self._hk_win.unbind("<KeyRelease>")
        self._hk_entry = None
        self._hk_win = None

    def _hk_fill(self, text: str) -> None:
        entry = self._hk_entry
        if entry is not None:
            entry.delete(0, "end")
            entry.insert(0, text)

    @staticmethod
    def _hk_normalize(keysym: str) -> tuple[str, bool]:
        """keysym -> (имя, является ли модификатором)."""
        if keysym in _HK_KEY_MAP:
            return _HK_KEY_MAP[keysym], True
        return keysym.lower(), False

    def _hk_on_key(self, event: tk.Event) -> str | None:
        if not self._recording:
            return None
        keysym = event.keysym
        if keysym == "Escape":
            self._stop_hotkey_recording()  # отмена: возвращаем старое значение
            return "break"
        if keysym in _HK_IGNORE_KEYS:
            return None
        name, is_mod = self._hk_normalize(keysym)
        if is_mod:
            if name not in self._hk_mods:
                self._hk_mods.append(name)
            # Поле не обновляем: итог показывается после отпускания
            # всех клавиш (иначе мелькают «alt», «ctrl+alt» и т.п.).
        else:
            self._hk_main = name
            self._hk_main_held = True
            # Снимок комбинации: модификаторы ещё «на месте», запоминаем.
            self._hk_pending = "+".join([*self._hk_mods, name])
        return None

    def _hk_on_release(self, event: tk.Event) -> str | None:
        if not self._recording:
            return None
        keysym = event.keysym
        name, is_mod = self._hk_normalize(keysym)
        if is_mod and name in self._hk_mods:
            self._hk_mods.remove(name)
            if not self._hk_main and not self._hk_mods:
                # Только модификатор нажали и отпустили — сбрасываем вид.
                self._hk_fill("Нажмите сочетание...")
        elif not is_mod and name == self._hk_main and keysym not in _HK_IGNORE_KEYS:
            self._hk_main_held = False
        # Финализация — когда отпущены все клавиши и записан главный ключ.
        # Берём снимок, сделанный при нажатии главной клавиши: к этому
        # моменту _hk_mods уже пуст (моды отпущены), собирать заново нельзя.
        if self._hk_main and not self._hk_mods and not self._hk_main_held:
            self._recording = False
            combo = self._hk_pending or self._hk_main
            # Валидация парсером pynput (источник правды — settings.py).
            if normalize_hotkey(combo) is None:
                self._hk_result = None
                self._hk_fill(self._hk_orig or "—")
                border = RED
            else:
                self._hk_result = combo
                self._hk_fill(combo)
                border = BORDER
            entry = self._hk_entry
            if entry is not None:
                entry.configure(fg_color=SURFACE, text_color=TEXT, border_color=border)
            if self._hk_win is not None:
                self._hk_win.unbind("<Key>")
                self._hk_win.unbind("<KeyRelease>")
        return None

    # ------------------------------------------------------------------ #
    #  Фоновый перевод + возврат результата в UI                         #
    # ------------------------------------------------------------------ #
    def _worker(self, source: str, req_id: int) -> None:
        """Выполняется в отдельном потоке, чтобы не блокировать UI.

        Короткий текст — единый перевод (глоссарий/NN, кэш не трогаем).
        Длинный — инкрементальный стриминг с кэшем: уже переведённые
        предложения берутся из ``self._sent_cache`` без вызова NN, новые —
        переводятся и дописываются в кэш. После каждого предложения в
        очередь уходит ("partial", req_id, so_far, done, total), в конце —
        ("ok", ...). Устаревшие req_id отбрасывает _poll_results."""
        started = time.perf_counter()
        try:
            if _is_short(source):
                text, kind = translate_text(source)
            else:
                def _progress(so_far: str, done: int, total: int) -> None:
                    self._queue.put(("partial", req_id, so_far, done, total))

                text, self._sent_cache = _TRANSLATOR.translate_incremental_streaming(
                    source, self._sent_cache, _progress
                )
                kind = "nn"
            elapsed = time.perf_counter() - started
            self._queue.put(("ok", req_id, text, kind, elapsed))
        except Exception:  # noqa: BLE001
            import traceback
            self._queue.put(("error", req_id, traceback.format_exc(), None, None))

    def _poll_results(self) -> None:
        try:
            while True:
                kind, req_id, payload, source_kind, elapsed = self._queue.get_nowait()
                if req_id != self._request_id:
                    # Текст уже изменился — устаревший результат отбрасываем
                    continue
                if kind == "partial":
                    # Стриминг: payload — накопленный перевод,
                    # source_kind/elapsed — счётчики done/total.
                    done, total = int(source_kind), int(elapsed)
                    self._set_output(payload)
                    self._set_status(
                        f"Переведено {done}/{total} предложений...", ACCENT
                    )
                    continue  # _pending остаётся True — перевод ещё идёт
                if kind == "ok":
                    self._set_output(payload)
                    self._set_badge(source_kind)
                    self._set_time(elapsed)
                    self._set_status("Готово", GREEN)
                else:
                    self._set_output("Ошибка перевода:\n" + payload)
                    self._set_badge(None)
                    self._set_time(None)
                    self._set_status("Ошибка", RED)
                self._pending = False
        except queue.Empty:
            pass
        self.root.after(100, self._poll_results)


def main() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    root = ctk.CTk()
    TranslatorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
