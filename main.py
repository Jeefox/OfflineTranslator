# -*- coding: utf-8 -*-

import customtkinter as ctk
import re
import tkinter as tk
from tkinter import messagebox
from translator import OfflineTranslator
from settings import Settings, normalize_hotkey, validate_value
from hotkey_agent import HotkeyAgent, PYNPUT_AVAILABLE
from sentence_pipeline import off_to_tk
import queue
import threading

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
        "bg": "#eff1f5", "field": "#ffffff", "panel": "#ccd0da",
        "panel_hover": "#bcc2d0", "scrollbar": "#bcc2d0",
        "scrollbar_hover": "#aab2c4",
        "accent": "#1e66f5", "accent_hover": "#3b82f6", "accent_text": "#eff1f5",
        "text": "#4c4f69", "muted": "#6c7086",
        "success": "#40a02b", "error": "#d20f39", "pending": "#fe640b",
        "hl": "#d2e0fd",
    },
}


class _StaleTranslation(Exception):
    """Поколение перевода устарело — worker завершается как можно раньше
    и не публикует результатов (проверка делается на каждом предложении)."""

class TranslatorApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Офлайн Переводчик (EN ↔ RU)")
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
        # Счётчик поколений операций перевода: результат применяется к UI
        # только если поколение совпадает (защита от «устаревшего» результата
        # после очистки полей / смены направления / swap).
        self._translation_generation = 0
        # Потокобезопасная очередь «фоновый поток -> главный поток Tkinter»:
        # все изменения GUI происходят только в главном потоке.
        self._gui_queue = queue.Queue()

        # Инициализация переводчика в отдельном потоке, чтобы окно не зависло при загрузке
        self.translator = None

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
        # подсветки Этапа 5): _hl_src_tag/_hl_dst_tag — теги подсветки
        # ТЕКУЩЕЙ пары «предложение k ↔ перевод k» в полях (мягкий фон
        # текущей палитры; готовые предложения — обычный фон, подсветка
        # снимается при завершении/очистке/смене направления/swap).
        # Одно логическое предложение = одна единица синхронизации UI:
        # сколько бы технических chunks ни было внутри предложения,
        # пара продвигается ровно один раз на предложение (на "done").
        # _hl_last_src/_hl_last_dst — символьные диапазоны текущего
        # (последнего завершённого) юнита в исходном поле / поле вывода.
        self._hl_src_tag = "hl_src"
        self._hl_dst_tag = "hl_dst"
        self._hl_last_src = None
        self._hl_last_dst = None
        # Синхронная прокрутка полей (Этап 5): _syncing_scroll — защита
        # от рекурсии (пока код программно выставляет вид партнёра,
        # обратный callback не синхронизирует снова); _auto_follow —
        # автопоказ текущей пары при переводе (выключается, когда
        # пользователь прокручивает вручную; включается заново при
        # следующем запуске перевода).
        self._syncing_scroll = False
        self._auto_follow = True
        # Текущий статус (вид, текст) — переотрисовывается при смене темы.
        self._status = ("busy", "Инициализация нейросети...")
        # Окно настроек и агент глобального хоткея.
        self._settings_dialog = None
        self._hotkey_agent = None
        self._agent_error = None

        self.setup_ui()
        self._bind_hotkeys()
        self._start_hotkey_agent()

        # Начинаем опрос очереди в главном потоке.
        self._process_gui_queue()

        # Запускаем загрузку модели в фоне: все виджеты уже созданы,
        # и сам поток не обращается к GUI (только к очереди).
        threading.Thread(target=self.init_translator, daemon=True).start()

        # При закрытии окна остановим глобальный агент хоткея.
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

    def init_translator(self):
        """Загружает модели в фоновом потоке; результат передаётся в UI через очередь.
        Поток не обращается к виджетам (нет гонки с setup_ui и с главным потоком)."""
        try:
            self.translator = OfflineTranslator()
            self._gui_queue.put(("init_done",))
        except Exception as e:
            self._gui_queue.put(("init_error", str(e)))

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
        self.header_subtitle = ctk.CTkLabel(header_right, text="EN ↔ RU · работает офлайн",
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
        self.input_label = ctk.CTkLabel(main_frame, text="Исходный текст (EN)",
                                        font=("Arial", 13, "bold"),
                                        text_color=self._pal["text"])
        self.input_label.grid(row=1, column=0, padx=10, pady=(10, 4), sticky="w")
        
        # Без фиксированных width/height: размер задаёт grid
        # (sticky="nsew" + weight) — поле тянется по ширине и высоте с окном.
        self.input_text = ctk.CTkTextbox(main_frame, font=("Arial", 14),
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
        self.output_label = ctk.CTkLabel(main_frame, text="Перевод (RU)",
                                         font=("Arial", 13, "bold"),
                                         text_color=self._pal["accent"])
        self.output_label.grid(row=1, column=1, padx=10, pady=(10, 4), sticky="w")
        
        self.output_text = ctk.CTkTextbox(main_frame, font=("Arial", 14),
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

        self.direction_var = ctk.StringVar(value="EN → RU")
        self.direction_menu = ctk.CTkOptionMenu(
            self.direction_frame,
            variable=self.direction_var,
            values=["EN → RU", "RU → EN"],
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
            self.translate_btn.configure(state="normal")
            # Если во время загрузки текста уже ввели — запланируем автоперевод.
            if self.settings.get("autotranslate"):
                self._schedule_autotranslate()
            if self._agent_error:
                # Глобальный хоткей недоступен — предупреждаем (повторно видно
                # и в окне «Настройки»).
                self._set_status("busy", f"Готово. Глобальный хоткей недоступен: {self._agent_error}")
            else:
                self._set_status("ready")
        elif kind == "init_error":
            # Загрузка не удалась: показать ошибку, кнопка остаётся отключённой.
            self._set_status("error", f"Ошибка загрузки: {message[1]}")
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
                # Замена текста стёрла тег подсветки перевода — переставляем
                # текущую пару на новый текст (диапазоны точны: результат
                # совпадает с накопленным стримом; если пользователь
                # вмешался, поколение уже устарело и мы сюда не пришли).
                if (self._hl_last_src is not None
                        and self._hl_last_dst is not None):
                    self._set_highlight(self._hl_last_src[0],
                                        self._hl_last_src[1],
                                        self._hl_last_dst[0],
                                        self._hl_last_dst[1])
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
            # После завершения: коалесированный запрос (если текст не изменился).
            self._continue_pending_translation()
        elif kind == "translation_error":
            generation, error = message[1], message[2]
            self._translation_busy = False
            self._active_text = None
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
        self.input_text.delete("1.0", "end")
        self.output_text.delete("1.0", "end")
        self._clear_highlight()
        # Запущенный перевод (если есть) больше не соответствует состоянию UI — помечаем его устаревшим.
        self._translation_generation += 1
        self._set_status("ready")

    def change_direction(self, value: str):
        """Сменяет направление перевода и обновляет подписи полей."""
        # Ожидающий автоперевод отменяется: смена направления — не причина
        # для «лишнего» перевода (направление поменялось, текст тот же).
        self._cancel_auto()
        self._pending_text = None
        # Перевод, запущенный до смены направления, был для другого направления — помечаем его устаревшим.
        self._translation_generation += 1
        self._clear_highlight()
        self._apply_direction("ru-en" if value == "RU → EN" else "en-ru")

    def swap_fields(self):
        """Меняет содержимое полей и направление перевода местами."""
        input_text = self.input_text.get("1.0", "end-1c")
        output_text = self.output_text.get("1.0", "end-1c")

        # Ожидающий автоперевод отменяем ДО изменения полей: после swap
        # запланируем ровно один новый (программный insert не порождает
        # <<Modified>>, поэтому рекурсивных/двойных событий нет).
        self._cancel_auto()
        self._pending_text = None

        self.input_text.delete("1.0", "end")
        self.output_text.delete("1.0", "end")
        if output_text:
            self.input_text.insert("1.0", output_text)
        if input_text:
            self.output_text.insert("1.0", input_text)
        # Содержимое полей изменилось — старые диапазоны подсветки недействительны.
        self._clear_highlight()

        # Содержимое полей изменилось — запущенный перевод (если есть) устарел.
        self._translation_generation += 1

        self.direction_var.set("RU → EN" if self.direction == "en-ru" else "EN → RU")
        self._apply_direction("ru-en" if self.direction == "en-ru" else "en-ru")

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
    #  Подсветка текущей пары предложений (Этап 4)                       #
    # ------------------------------------------------------------------
    def _setup_highlight_tags(self):
        """Настраивает теги подсветки в обоих полях (мягкий фон палитры).

        Таги ставим на внутренний tkinter.Text (_textbox): это штатный
        механизм подсветки диапазонов, который не влияет на редактирование,
        копирование, Ctrl+A/Ctrl+C/Ctrl+X (меняет только фон диапазона).
        """
        for box, tag in ((self.input_text._textbox, self._hl_src_tag),
                         (self.output_text._textbox, self._hl_dst_tag)):
            box.tag_configure(tag, background=self._pal["hl"])

    def _set_highlight(self, src_start, src_end, dst_start, dst_end):
        """Подсвечивает текущую пару «предложение ↔ его перевод».

        Диапазоны — символьные смещения в тексте соответствующего поля;
        None — диапазон не подсвечивать. Вызывать только из главного
        потока. В любой момент подсвечена ровно одна пара; готовые
        предложения — с обычным фоном.
        """
        in_tb = self.input_text._textbox
        out_tb = self.output_text._textbox
        in_tb.tag_remove(self._hl_src_tag, "1.0", "end")
        out_tb.tag_remove(self._hl_dst_tag, "1.0", "end")
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

    def _clear_highlight(self):
        """Снимает подсветку предложений из обоих полей и сбрасывает
        запомненный диапазон (вызывать только из главного потока)."""
        for box, tag in ((self.input_text._textbox, self._hl_src_tag),
                         (self.output_text._textbox, self._hl_dst_tag)):
            try:
                box.tag_remove(tag, "1.0", "end")
            except tk.TclError:
                pass
        self._hl_last_src = None
        self._hl_last_dst = None

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
        документа, что и в исходном поле. Без рекурсии: _syncing_scroll
        блокирует обратную синхронизацию из callback'а партнёра."""
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
            index = "1.0"        # поле вверху -> партнёр вверху
        elif f + l >= 1.0 - 5e-4:
            index = "end-1c"     # поле внизу -> партнёр внизу
        else:
            # Середина документа: та же доля (относительная позиция).
            self._syncing_scroll = True
            try:
                partner_tb.yview_moveto(f)
            except tk.TclError:
                pass
            finally:
                self._syncing_scroll = False
            return
        self._syncing_scroll = True
        try:
            partner_tb.see(index)
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
        """Автопоказ: подводит текущую пару в видимую область обоих полей
        (только во время перевода, если пользователь не прокрутил сам)."""
        if (not self._auto_follow
                or self._hl_last_src is None or self._hl_last_dst is None):
            return
        in_tb = self.input_text._textbox
        out_tb = self.output_text._textbox
        in_text = in_tb.get("1.0", "end-1c")
        out_text = out_tb.get("1.0", "end-1c")
        # Оба вида выставляем под общей защитой: их yscrollcommand'ы
        # сработают, но обратную синхронизацию не запустят.
        self._syncing_scroll = True
        try:
            in_tb.see(off_to_tk(in_text, self._hl_last_src[0]))
            out_tb.see(off_to_tk(out_text, self._hl_last_dst[0]))
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
                self._set_status("busy", f"Перевод… ({done}/{total})")
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
        # Смещения dst вычисляются из фактически вставленного текста,
        # поэтому соответствие original N <-> translated N точное.
        self._hl_last_src = (unit.src_start, unit.src_end)
        self._hl_last_dst = (dst_start, dst_end)
        self._set_highlight(unit.src_start, unit.src_end, dst_start, dst_end)
        self._show_current_pair()
        if total > 1:
            self._set_status("busy", f"Переведено {done}/{total} предложений…")

    def _apply_direction(self, direction: str):
        """Устанавливает направление и обновляет меню с подписями полей."""
        self.direction = direction
        if direction == "ru-en":
            self.direction_var.set("RU → EN")
            self.input_label.configure(text="Исходный текст (RU)")
            self.output_label.configure(text="Перевод (EN)")
        else:
            self.direction_var.set("EN → RU")
            self.input_label.configure(text="Исходный текст (EN)")
            self.output_label.configure(text="Перевод (RU)")

    def _set_theme(self, theme: str):
        """Применяет цветовую тему к основному окну (без перезапуска)."""
        pal = PALETTES.get(theme, PALETTES["dark"])
        self._pal = dict(pal)
        self.configure(fg_color=pal["bg"])
        # Фон подсветки предложений — из палитры темы.
        self._setup_highlight_tags()
        for box in (self.input_text, self.output_text):
            box.configure(fg_color=pal["field"],
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
        # bind_all: срабатывают, где бы фокус ни был (поле, кнопки).
        self.bind_all("<Control-Return>", self._on_hotkey_translate)    # Ctrl+Enter — перевести
        self.bind_all("<Control-l>", self._on_hotkey_clear)             # Ctrl+L — очистить
        self.bind_all("<Control-comma>", self._on_hotkey_settings)      # Ctrl+, — настройки
        # Escape на главном окне: закрыть окно настроек (если открыто).
        self.bind("<Escape>", self._on_hotkey_escape)
        # Ctrl+A — на каждом поле отдельно (не bind_all): выделяется всё
        # только в том поле, где нажата комбинация (CTkTextbox.bind
        # проксирует биндинг на внутренний Text).
        self.input_text.bind("<Control-a>", self._on_select_all)
        self.output_text.bind("<Control-a>", self._on_select_all)
        # Ctrl+C в поле перевода — копирует ВЕСЬ перевод (поведение старой
        # версии). В исходном поле системный Copy не перехватываем;
        # Ctrl+X (вырезать) — стандартное поведение текстовых полей.
        self.output_text.bind("<Control-c>", self._on_hotkey_copy)

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
            self._clear_highlight()
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
                self._clear_highlight()
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
            self._clear_highlight()
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
            self._clear_highlight()
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
        # Переводим сразу (без debounce), окно поднимаем — результат виден.
        self.start_translation()
        try:
            self.attributes("-topmost", True)
            self.lift()
        except tk.TclError:
            pass
        self.after(1500, self._drop_topmost)

    def _drop_topmost(self):
        try:
            self.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _set_direction(self, direction: str):
        """Программная смена направления (агент хоткея, настройки)."""
        self._cancel_auto()
        self._pending_text = None
        self._translation_generation += 1
        self._apply_direction(direction)

    def _open_settings(self):
        """Открывает окно «Настройки» (одно окно за раз)."""
        dialog = self._settings_dialog
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
            dialog.focus()
            return
        self._settings_dialog = SettingsDialog(self)

    def _on_window_close(self):
        self._stop_hotkey_agent()
        self.destroy()


# =====================================================================
# Окно «Настройки» (в стилистике основного окна)
# =====================================================================

# Языки/тема в интерфейсе — по-русски, в настройках хранятся внутренние коды.
# Текущая архитектура поддерживает только EN/RU (модели Helsinki-NLP opus-mt).
_LANG_LABELS = {"en": "Английский (EN)", "ru": "Русский (RU)"}
_LANG_FROM_LABEL = {v: k for k, v in _LANG_LABELS.items()}
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
    "debounce_sec": "debounce ввода",
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

        # Тема
        row_label("Тема")
        self.theme_var = make_option(list(_THEME_LABELS.values()),
                                     _THEME_LABELS[s.get("theme")])

        # Языки (взаимоисключающие: выбор одного меняет второе)
        row_label("Исходный язык")
        self.source_lang_var = make_option(list(_LANG_LABELS.values()),
                                           _LANG_LABELS[s.get("source_lang")],
                                           self._on_source_lang)
        row_label("Язык перевода")
        self.target_lang_var = make_option(list(_LANG_LABELS.values()),
                                           _LANG_LABELS[s.get("target_lang")],
                                           self._on_target_lang)

        # Автоперевод и тайминги
        row_label("Автоперевод")
        self.autotranslate_var = make_switch(
            "после остановки набора текста", s.get("autotranslate"))

        row_label("Дебаунс, сек")
        self.debounce_entry = make_entry(str(s.get("debounce_sec")))

        row_label("Timeout, сек")
        self.slow_entry = make_entry(str(s.get("slow_after_sec")))

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
            other = "Русский (RU)" if value == "Английский (EN)" else "Английский (EN)"
            self.target_lang_var.set(other)
        finally:
            self._syncing = False

    def _on_target_lang(self, value):
        if self._syncing:
            return
        self._syncing = True
        try:
            other = "Английский (EN)" if value == "Русский (RU)" else "Русский (RU)"
            self.source_lang_var.set(other)
        finally:
            self._syncing = False

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

    def _hk_on_key(self, event):
        if not self._recording:
            return None
        if event.keysym == "Escape":
            # Esc во время записи — отмена (окно не закрывается)
            self._stop_recording(cancel=True)
            return "break"
        if event.keysym in _HK_IGNORE_KEYS:
            return None
        name = self._hk_key_name(event.keysym)
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
        name = self._hk_key_name(event.keysym)
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
        self.app._set_direction(f"{normalized['source_lang']}-{normalized['target_lang']}")
        self.app._start_hotkey_agent()
        self.app._set_status("ready", "Настройки сохранены")
        self._on_close()


if __name__ == "__main__":
    app = TranslatorApp()
    app.mainloop()




