"""GUI ownership and atomic model replacement regressions; run under Xvfb."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main

with tempfile.TemporaryDirectory() as config, \
        patch.dict(os.environ, {"OFFLINE_TRANSLATE_CONFIG": config}), \
        patch.object(main.TranslatorApp, "_start_hotkey_agent"), \
        patch.object(main.SystemTray, "start", return_value=False), \
        patch.object(main, "_SingleInstance", return_value=SimpleNamespace(acquired=True, close=lambda: None)):
    with patch.object(main.TranslatorApp, "_start_model_load"):
        app = main.TranslatorApp()
    try:
        old = object()
        app.translator = old
        app._translator_model_id = "marian-en-ru"
        app._active_model_id = "marian-ru-en"
        app.model_id = "marian-ru-en"
        app._apply_direction("ru-en")
        app._load_seq = 1
        app._model_loading = True
        with patch.object(main, "OfflineTranslator", side_effect=RuntimeError("failed load")):
            app._init_translator_worker(1, "marian-ru-en")
        assert app.translator is old
        app._handle_message(app._gui_queue.get_nowait())
        assert app.translator is old
        assert app._active_model_id == "marian-en-ru"
        assert app.direction == "en-ru"
        assert app.translate_btn.cget("state") == "normal"

        new = object()
        app._load_seq = 2
        app._active_model_id = "marian-ru-en"
        with patch.object(main, "OfflineTranslator", return_value=new):
            app._init_translator_worker(2, "marian-ru-en")
        assert app.translator is old, "Worker must not publish directly"
        message = app._gui_queue.get_nowait()
        app._load_seq = 3
        app._handle_message(message)
        assert app.translator is old, "Queued stale load must not replace runtime"
        app._load_seq = 2
        app._handle_message(message)
        assert app.translator is new
        assert app._translator_model_id == "marian-ru-en"

        app._translation_generation = app._worker_generation = 8
        app._translation_busy = True
        app._active_text = "new operation"
        app._set_status("busy", "New operation")
        app.translate_btn.configure(state="disabled")
        for message in [("translation_done", 7, "OLD"),
                        ("translation_error", 7, "old error"), ("stream_stale", 7)]:
            app._handle_message(message)
            assert app._translation_busy
            assert app._active_text == "new operation"
            assert app._status == ("busy", "New operation")
            assert app.translate_btn.cget("state") == "disabled"

        # Returning to the old runtime while another model loads invalidates that load.
        app._translation_busy = False
        app._model_loading = False
        app.translator = old
        app._translator_model_id = app._active_model_id = app.model_id = "marian-en-ru"
        requests = []
        app._model_loader = SimpleNamespace(submit=lambda *args: requests.append(args), close=lambda: None)
        app._set_direction("ru-en")
        first_seq = app._load_seq
        app._set_direction("en-ru")
        assert app._load_seq > first_seq
        assert len(requests) == 2
        assert requests[-1][2] == "marian-en-ru"
    finally:
        app._quit_app()
print("OK: model lifecycle regressions passed")
