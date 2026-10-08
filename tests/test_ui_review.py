"""Регрессии issue #5: статус очистки, узкие настройки, спинбоксы и навигация.

Запуск: xvfb-run -a .venv/bin/python tests/test_ui_review.py
"""
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


def pump(app, seconds=.25):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.update()
        time.sleep(.01)


def children(widget):
    for child in widget.winfo_children():
        yield child
        yield from children(child)


with tempfile.TemporaryDirectory(prefix="offline-ui-review-") as config, \
        patch.dict(os.environ, {"OFFLINE_TRANSLATE_CONFIG": config}), \
        patch.object(main.TranslatorApp, "_start_model_load"), \
        patch.object(main.TranslatorApp, "_start_hotkey_agent"), \
        patch.object(main.SystemTray, "start", return_value=False):
    app = main.TranslatorApp()
    callback_errors = []
    app.report_callback_exception = lambda *error: callback_errors.append(error)
    try:
        app._model_loading = False
        app.translator = None
        app._set_status("error", "Ошибка загрузки модели")
        app.clear_fields()
        assert app._status == ("error", "Ошибка загрузки модели")
        app._model_loading = True
        app._set_status("busy", "Загрузка модели")
        app.clear_fields()
        assert app._status == ("busy", "Загрузка модели")
        app._load_seq = 2
        with patch.object(main, "OfflineTranslator", side_effect=RuntimeError("old load")):
            app._init_translator_worker(1)
        assert app._gui_queue.empty(), "Устаревшая ошибка загрузки попала в очередь"

        app.geometry("700x450")
        pump(app)
        assert abs(app.input_text.winfo_width() - app.output_text.winfo_width()) <= 1

        app._open_settings()
        dialog = app._settings_dialog
        app.settings.set("autotranslate", False)
        app.translator = object()
        app._translator_model_id = app._active_model_id
        app._runtime_unavailable.add(app._active_model_id)
        app._handle_message(("init_done",))
        assert app._active_model_id not in app._runtime_unavailable
        assert "ошибка загрузки" not in dialog.model_var.get()
        for theme in ("dark", "light"):
            app._set_theme(theme)
            for width in (500, 560, 700, 900):
                dialog.geometry(f"{width}x640")
                pump(app)
                right = dialog._scroll_frame.winfo_rootx() + dialog._scroll_frame.winfo_width()
                for widget in children(dialog._scroll_frame):
                    if isinstance(widget, (main.ctk.CTkEntry, main.ctk.CTkOptionMenu,
                                           main.ctk.CTkButton)):
                        assert widget.winfo_rootx() + widget.winfo_width() <= right + 2, \
                            (width, type(widget).__name__)
                assert dialog.save_btn.winfo_rooty() + dialog.save_btn.winfo_height() <= \
                    dialog.winfo_rooty() + dialog.winfo_height()

        for entry, low, high in ((dialog.debounce_entry, .1, 60),
                                 (dialog.slow_entry, 0, 600),
                                 (dialog.notification_duration_entry, 1, 120),
                                 (dialog.max_len_entry, 0, 1000000)):
            row = entry.grid_info()["row"]
            frame = next(w for w in entry.master.grid_slaves(row=row)
                         if isinstance(w, main.ctk.CTkFrame) and
                         any(isinstance(c, main.ctk.CTkButton) for c in w.winfo_children()))
            minus, plus = frame.winfo_children()
            entry.delete(0, "end")
            entry.insert(0, str(low))
            minus.invoke()
            assert float(entry.get()) == low
            plus.invoke()
            assert float(entry.get()) > low
            entry.delete(0, "end")
            entry.insert(0, str(high))
            plus.invoke()
            assert float(entry.get()) == high

        nav = {w.cget("text"): w for w in children(dialog)
               if isinstance(w, main.ctk.CTkButton) and
               w.cget("text") in ("Основные", "Перевод", "Хоткей")}
        dialog.geometry("700x480")
        pump(app)
        positions = []
        for name in ("Основные", "Перевод", "Хоткей"):
            nav[name].invoke()
            pump(app)
            positions.append(dialog._scroll_frame._parent_canvas.yview()[0])
        assert positions[0] < positions[1] <= positions[2], positions
        assert positions[2] > positions[0], positions
        assert not callback_errors, callback_errors
        print("OK: UI review regressions passed")
    finally:
        if app._settings_dialog is not None:
            app._settings_dialog._on_close()
        app._quit_app()
