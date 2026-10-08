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

        text_widget = main.tk.Text(app)
        text_widget.insert("1.0", "Hello.\nWorld.")
        first_offsets = app._hover_cache_line_offsets(text_widget)
        text_widget.delete("1.0", "end")
        text_widget.insert("1.0", "Hello. World.")
        second_offsets = app._hover_cache_line_offsets(text_widget)
        assert first_offsets != second_offsets
        assert second_offsets == main.line_offsets("Hello. World.")
        assert app._hover_cache_line_offsets(text_widget) is second_offsets
        text_widget.destroy()

        app.geometry("700x450")
        pump(app)
        assert abs(app.input_text.winfo_width() - app.output_text.winfo_width()) <= 1

        assert app._application_icon.width() == 256
        for button in (app.settings_btn, app.swap_btn, app.copy_btn, app.clear_btn):
            assert button.cget("text") == ""
            assert button.cget("image") is not None
            assert button._canvas.cget("takefocus") == "1"
            # Проверяем реальное событие клавиатуры, а не прямой invoke().
            with patch.object(button, "_command") as command:
                button._canvas.focus_force()
                pump(app)
                assert button.cget("border_width") == 2
                for key in ("<Return>", "<space>"):
                    button._canvas.event_generate(key)
                    pump(app, .05)
                assert command.call_count == 2
        app.input_text.focus_set()
        pump(app)
        assert app.clear_btn.cget("border_width") == 0

        app._open_settings()
        dialog = app._settings_dialog
        assert dialog._application_icon.width() == 256
        app.settings.set("autotranslate", False)
        app.translator = object()
        app._translator_model_id = app._active_model_id
        identity = app._model_request_identity(app._active_model_id)
        app._translator_request_identity = identity
        app._runtime_failures[identity] = "test failure"
        app._handle_message(("init_done",))
        assert identity not in app._runtime_failures
        assert "ошибка загрузки" not in dialog.model_var.get()
        # Failed persistence neither closes the dialog nor applies new settings.
        old_settings = app.settings.as_dict()
        dlg_status = app._status
        dialog.theme_var.set("Светлая")
        with patch.object(app.settings, "save", return_value=False), \
                patch.object(dialog, "_show_error") as error, \
                patch.object(app, "_set_direction") as direction, \
                patch.object(app, "_set_theme") as theme:
            dialog._on_save()
        assert error.called and "сохранить" in error.call_args.args[0]
        assert app.settings.as_dict() == old_settings
        assert dialog.winfo_exists() and app._status == dlg_status
        direction.assert_not_called()
        theme.assert_not_called()
        dialog.theme_var.set("Тёмная")
        for theme in ("dark", "light"):
            app._set_theme(theme)
            assert main.ctk.get_appearance_mode().lower() == theme
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
