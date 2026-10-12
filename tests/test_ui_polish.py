"""Screen bounds, small-screen/DPI layout and private agent notifications."""
import os
import faulthandler
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from ui_widgets import _Tooltip

faulthandler.dump_traceback_later(60, exit=True)

with tempfile.TemporaryDirectory() as directory, \
        patch.dict(os.environ, {"OFFLINE_TRANSLATE_CONFIG": directory,
                               "XDG_DATA_HOME": directory + "/data", "XDG_CACHE_HOME": directory + "/cache"}), \
        patch.object(main.TranslatorApp, "_start_model_load"), \
        patch.object(main.TranslatorApp, "_start_hotkey_agent"), \
        patch.object(main.SystemTray, "start", return_value=False):
    app = main.TranslatorApp()
    app._load_seq = 0
    errors = []
    app.report_callback_exception = lambda *error: errors.append(error)
    try:
        app.update()
        # Simulated screen metrics and CTk scaling; no claim of native DPI QA.
        main.ctk.set_widget_scaling(2)
        main.ctk.set_window_scaling(2)
        with patch.object(main.SettingsDialog, "winfo_screenwidth", return_value=800), \
                patch.object(main.SettingsDialog, "winfo_screenheight", return_value=600):
            app._open_settings()
            dialog = app._settings_dialog
            app.update()
            assert dialog.winfo_width() <= 768
            assert dialog.winfo_height() <= 520
            assert dialog.winfo_rootx() >= 0 and dialog.winfo_rooty() >= 0
            assert dialog.winfo_rootx() + dialog.winfo_width() <= 800
            assert dialog.winfo_rooty() + dialog.winfo_height() <= 600
            assert dialog._content_stacked
            right = dialog._scroll_frame.winfo_rootx() + dialog._scroll_frame.winfo_width()
            for widget in dialog._scroll_frame.winfo_children():
                if isinstance(widget, (main.ctk.CTkEntry, main.ctk.CTkOptionMenu, main.ctk.CTkFrame)):
                    assert widget.winfo_rootx() + widget.winfo_width() <= right + 2, type(widget)
            assert dialog.model_name_label.cget("text") == dialog.model_var.get()
            assert dialog.save_btn.winfo_rooty() + dialog.save_btn.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height()
            dialog._on_close()
        main.ctk.set_widget_scaling(1)
        main.ctk.set_window_scaling(1)
        app.update()
        app._open_settings()
        dialog = app._settings_dialog
        original_delay = dialog.debounce_entry.get()
        for width, stacked in ((500, True), (760, False), (500, True)):
            dialog.geometry(f"{width}x480")
            app.update()
            assert dialog._content_stacked == stacked
            assert dialog.debounce_entry.get() == original_delay
            assert dialog.model_name_label._label.winfo_width() <= dialog.model_name_label.winfo_width()
        dialog._on_close()
        tooltip = _Tooltip(app.settings_btn, "Long tooltip at the bottom right of the screen")
        with patch.object(app.settings_btn, "winfo_rootx", return_value=app.winfo_screenwidth() - 4), \
                patch.object(app.settings_btn, "winfo_rooty", return_value=app.winfo_screenheight() - 4):
            tooltip._show()
        app.update()
        window = tooltip.window
        assert window.winfo_rootx() >= 0 and window.winfo_rooty() >= 0
        assert window.winfo_rootx() + window.winfo_width() <= app.winfo_screenwidth()
        assert window.winfo_rooty() + window.winfo_height() <= app.winfo_screenheight()
        tooltip._hide()

        app.settings.set("autotranslate", False)
        app.settings.set("filter_cyrillic", False)
        app.settings.set("notifications_enabled", False)
        app.settings.set("max_text_length", 5)
        app._model_loading = False
        app.translator = object()
        started = []
        class DeferredThread:
            def __init__(self, target, args=(), **kwargs): self.target, self.args = target, args
            def start(self): started.append((self.target, self.args))
        with patch.object(main.threading, "Thread", DeferredThread):
            app._handle_agent_request("0123456789")
            assert app.input_text.get("1.0", "end-1c") == "01234"
            assert "первые 5 из 10" in app._status[1]
            generation = app._translation_generation
            with patch.object(app, "_send_translation_notification") as notify, \
                    patch.object(app, "state", return_value="withdrawn"), \
                    patch.object(app, "_show_from_tray") as show:
                app._handle_message(("translation_done", generation, "translated"))
            notify.assert_not_called()
            assert all(target != notify for target, _args in started)
            show.assert_called_once()
            assert not app._notification_after_translation and app._notification_text is None
            app.clear_fields()
            assert app._agent_truncation_warning is None
            assert "сокращён" not in app._status[1]
            app.settings.set("notifications_enabled", True)
            app.settings.set("max_text_length", 0)
            app._handle_agent_request("standalone agent text")
            generation = app._translation_generation
            app._handle_message(("translation_done", generation, "standalone result"))
            app._handle_message(("translation_done", generation, "standalone result"))
            notifications = [args for target, args in started
                             if target == app._send_translation_notification]
            assert notifications == [("standalone result", generation)], notifications
            # A queued worker must respect disabling notifications before it runs.
            app.settings.set("notifications_enabled", False)
            with patch.object(main, "show_notification") as native_notify:
                app._send_translation_notification("private result", generation)
            native_notify.assert_not_called()
        assert not errors, errors
        print("OK: screen bounds, scaled layout, truncation warning and private notification mode")
    finally:
        main.ctk.set_widget_scaling(1)
        main.ctk.set_window_scaling(1)
        if app._settings_dialog is not None:
            app._settings_dialog._on_close()
        app._quit_app()
        faulthandler.cancel_dump_traceback_later()
