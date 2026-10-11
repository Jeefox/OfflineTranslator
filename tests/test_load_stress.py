"""Deterministic shutdown under active inference and model loading (Xvfb)."""
import os
import gc
from pathlib import Path
import sys
import tempfile
import threading
import weakref
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from model_loader import ModelLoader
from translation_service import TranslationService


class BlockingBackend:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
    def load(self): pass
    def split_sentence(self, text, direction):
        return [text[:5], text[5:]]
    def translate_chunk(self, text, direction):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(5), "blocked inference timed out"
        return text.upper()


with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
        "OFFLINE_TRANSLATE_CONFIG": directory,
        "XDG_DATA_HOME": directory, "XDG_CACHE_HOME": directory}):
    app = main.TranslatorApp(start_services=False)
    backend = BlockingBackend()
    app.translator = TranslationService(backend, snapshot_loader=lambda _: None)
    generation = app._translation_generation
    worker = threading.Thread(target=app.translate_thread,
                              args=("abcdefghij", "en-ru", generation))
    worker.start()
    try:
        assert backend.entered.wait(5)
        app._quit_app()
    finally:
        backend.release.set()
        worker.join(5)
    assert not worker.is_alive(), "shutdown failed to cancel translation"
    assert backend.calls == 1, "closed app continued translating remaining chunks"
    messages = []
    while not app._gui_queue.empty():
        messages.append(app._gui_queue.get_nowait())
    assert ("stream_stale", generation) in messages
    assert not any(message[0] == "translation_done" for message in messages)

    # Replacements must release previous runtimes, including after GUI queue
    # delivery. A close during an active load must never publish its result.
    app = main.TranslatorApp(start_services=False)
    references = []
    class Runtime:
        def __init__(self, **kwargs):
            pass
    with patch.object(main, "OfflineTranslator", side_effect=Runtime):
        for sequence in range(1, 33):
            app._load_seq = sequence
            app._active_model_id = "marian-en-ru"
            app._init_translator_worker(sequence, "marian-en-ru")
            app._handle_message(app._gui_queue.get_nowait())
            references.append(weakref.ref(app.translator))
            gc.collect()
            assert all(reference() is None for reference in references[:-1])
    entered, release = threading.Event(), threading.Event()
    def load(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return Runtime()
    with patch.object(main, "OfflineTranslator", side_effect=load):
        app._start_model_load()
        try:
            assert entered.wait(5)
            app._quit_app()
        finally:
            release.set()
            app._model_loader._thread.join(5)
        assert not app._model_loader.is_alive()
        assert app._gui_queue.empty(), "closed app received a completed model"

# Closing an active loader drops pending work and exits after the active call.
entered, release = threading.Event(), threading.Event()
calls = []
def blocking(value):
    calls.append(value)
    entered.set()
    assert release.wait(5)
loader = ModelLoader()
loader.submit(blocking, 0)
assert entered.wait(5)
for value in range(1, 100):
    loader.submit(blocking, value)
loader.close()
assert not loader.submit(blocking, 100)
release.set()
loader._thread.join(5)
assert not loader.is_alive() and calls == [0]
print("OK: shutdown cancels inference; active loader exits and drops pending work")
