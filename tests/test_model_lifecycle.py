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

        # Same model ID: failed path B must not mark the retained path A unavailable.
        paths_a = {"en-ru": str(Path(config) / "A"), "ru-en": None, "gguf": None}
        paths_b = dict(paths_a, **{"en-ru": str(Path(config) / "B")})
        app._load_seq = 4
        app._active_model_id = "marian-en-ru"
        with patch.object(main, "OfflineTranslator", return_value=old):
            app._init_translator_worker(4, "marian-en-ru", paths_a)
        app._handle_message(app._gui_queue.get_nowait())
        ready_identity = app._translator_request_identity
        app._load_seq = 5
        with patch.object(main, "OfflineTranslator", side_effect=RuntimeError("path B failed")):
            app._init_translator_worker(5, "marian-en-ru", paths_b)
        app._handle_message(app._gui_queue.get_nowait())
        assert app.translator is old and app._translator_request_identity == ready_identity
        failed_identity = app._model_request_identity("marian-en-ru", paths_b)
        assert failed_identity in app._runtime_failures
        assert ready_identity not in app._runtime_failures
        with patch.object(main, "local_path", side_effect=lambda key: paths_b[key]):
            assert app._runtime_model_state("marian-en-ru") == (True, True)
            app._open_settings()
            dialog = app._settings_dialog
            labels = dialog._model_labels("en-ru")
            label = next(label for label, mid in labels if mid == "marian-en-ru")
            assert "работает" in label and "не загрузилась" in label
            dialog._model_label_to_id = dict(labels)
            dialog.model_var.set(label)
            dialog._update_model_note()
            assert "Текущая модель работает" in dialog.model_note_label.cget("text")
            # Switching sources cannot carry a failure onto another path.
            with patch.object(main, "local_path", side_effect=lambda key: paths_a[key]):
                assert app._runtime_model_state("marian-en-ru") == (True, False)
            app._load_seq = 6
            with patch.object(main, "OfflineTranslator", return_value=new):
                app._init_translator_worker(6, "marian-en-ru", paths_b)
            app._handle_message(app._gui_queue.get_nowait())
            assert failed_identity not in app._runtime_failures
            assert app._translator_request_identity == failed_identity
        dialog.destroy()
        app._settings_dialog = None

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

        # Full diagnostic exception is logged, but private paths never reach UI.
        from translation_errors import BackendError
        try:
            raise OSError("/private/model/path native tokenizer diagnostic")
        except OSError as cause:
            try:
                raise BackendError(str(cause)) from cause
            except BackendError as error:
                with patch.object(app, "_continue_pending_translation"), \
                        patch.object(main.logger, "error") as log:
                    app._handle_message(("translation_error", 8, error))
                assert log.call_args.kwargs["exc_info"][1] is error
        assert "private" not in app._status[1] and "native" not in app._status[1]
        assert "Ошибка перевода" in app._status[1]

        # Failed asynchronous notification restores a hidden window on the GUI thread.
        with patch.object(main, "show_notification", return_value=False):
            app._send_translation_notification("result", 8)
        notification = app._gui_queue.get_nowait()
        assert notification == ("notification_done", 8, False)
        with patch.object(app, "state", return_value="withdrawn"), \
                patch.object(app, "_show_from_tray") as show:
            app._handle_message(("notification_done", 7, False))
            show.assert_not_called()
            app._handle_message(notification)
            show.assert_called_once()

        # An agent request B queued while A is running must not let A consume
        # B's notification request.  B remains single-flight/pending and only
        # its own result produces the notification.
        app.translator = old
        app._model_loading = False
        app._translation_generation = app._worker_generation = 20
        app._translation_busy = True
        app._active_text = "text A"
        app.input_text.delete("1.0", "end")
        app.input_text.insert("1.0", "text A")
        started_threads = []

        class DeferredThread:
            def __init__(self, target, args=(), **_kwargs):
                self.target = target
                self.args = args

            def start(self):
                started_threads.append((self.target, self.args))

        with patch.object(main.threading, "Thread", DeferredThread):
            app._handle_agent_request("text B")
            assert app._pending_text == "text B"
            assert app._notification_text == "text B"
            app._handle_message(("translation_done", 20, "result A"))
            assert all(target != app._send_translation_notification
                       for target, _args in started_threads)
            assert app._notification_after_translation
            assert app._active_text == "text B"
            generation_b = app._translation_generation
            app._handle_message(("translation_done", generation_b, "result B"))
            notifications = [(target, args) for target, args in started_threads
                             if target == app._send_translation_notification]
            assert notifications == [(app._send_translation_notification,
                                      ("result B", generation_b))]
            assert not app._notification_after_translation
            assert app._notification_text is None

            # A standalone agent translation still notifies once, and a
            # duplicate completion event cannot create a second notification.
            app._translation_busy = False
            app._active_text = None
            app._translation_generation = app._worker_generation = 30
            app.input_text.delete("1.0", "end")
            app.input_text.insert("1.0", "standalone")
            app._handle_agent_request("standalone")
            generation_standalone = app._translation_generation
            threads_before_repeat = len(started_threads)
            app._handle_agent_request("standalone")
            assert len(started_threads) == threads_before_repeat
            assert app._pending_text is None
            app._handle_message(("translation_done", generation_standalone,
                                 "standalone result"))
            notification_count = sum(
                target == app._send_translation_notification
                for target, _args in started_threads)
            assert notification_count == 2
            app._handle_message(("translation_done", generation_standalone,
                                 "standalone result"))
            assert sum(target == app._send_translation_notification
                       for target, _args in started_threads) == notification_count

            # A's error must not clear the notification belonging to pending B.
            app._translation_generation = app._worker_generation = 40
            app._translation_busy = True
            app._active_text = "text A"
            app._handle_agent_request("text B")
            app._handle_message(("translation_error", 40, "expected test error"))
            assert app._notification_after_translation
            assert app._notification_text == app._active_text == "text B"
            generation_b = app._translation_generation
            app._handle_message(("translation_done", generation_b, "result B"))
            assert started_threads[-1] == (app._send_translation_notification,
                                           ("result B", generation_b))
            assert not app._notification_after_translation

        # Agent truncation retains all whitespace inside the selected slice.
        clipboard_text = "  hello\t\r\n  tail"
        app.settings.set("max_text_length", 11)
        with patch.object(app, "_start_translation_internal"):
            app._handle_agent_request(clipboard_text)
        assert app.input_text.get("1.0", "end-1c") == clipboard_text[:11]

        # GUI invalidation during a real service chunk stops the remaining chunks.
        from translation_service import TranslationService
        class ChunkBackend:
            calls = 0
            def load(self): pass
            def split_sentence(self, text, direction): return ["some ", "text"]
            def translate_chunk(self, text, direction):
                self.calls += 1
                app._translation_generation += 1
                return text
        backend = ChunkBackend()
        app.translator = TranslationService(backend, snapshot_loader=lambda _: None)
        app._translation_generation = 9
        app.translate_thread("some text", "en-ru", 9)
        messages = []
        while not app._gui_queue.empty():
            messages.append(app._gui_queue.get_nowait())
        assert backend.calls == 1
        assert messages[-1] == ("stream_stale", 9)
        assert all(message[0] != "translation_done" for message in messages)

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
