"""Regression checks for bounded loading, lossless layout and cache limits."""
import os
from pathlib import Path
import random
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model_loader import ModelLoader
from sentence_pipeline import split_units, assemble_output
from translation_cache import TranslationCache
from translation_service import (TranslationService, TranslationError, split_sentence_to_chunks,
                                 CancellationToken, TranslationCancelled)
from dictionary_manager import build_snapshot
from single_instance import SingleInstanceGuard


class Backend:
    def __init__(self):
        self.calls = []
        self.loads = 0

    def load(self):
        self.loads += 1

    def split_sentence(self, text, direction):
        return [text]

    def translate_chunk(self, text, direction):
        self.calls.append(text)
        if text == "fail":
            raise RuntimeError("failure")
        return text.upper()


class StabilizationTests(unittest.TestCase):
    def test_serial_latest_loader(self):
        loader = ModelLoader()
        started, release, finished = (threading.Event() for _ in range(3))
        calls = []
        def load(value):
            calls.append(value)
            if value == 0:
                started.set()
                self.assertTrue(release.wait(3))
            else:
                finished.set()
        try:
            loader.submit(load, 0)
            self.assertTrue(started.wait(3))
            for value in range(1, 100):
                loader.submit(load, value)
            self.assertEqual(calls, [0])
            release.set()
            self.assertTrue(finished.wait(3))
            self.assertEqual(calls, [0, 99])
        finally:
            release.set()
            loader.close()

    def test_loader_survives_callback_and_error_handler_failures(self):
        failures, completed = threading.Event(), threading.Event()
        errors = []
        def failed(exc, function, args):
            errors.append((str(exc), args))
            failures.set()
            raise RuntimeError("handler failed too")
        loader = ModelLoader(failure_callback=failed)
        def broken(value):
            raise ValueError(value)
        try:
            with self.assertLogs("model_loader", level="ERROR") as logs:
                self.assertTrue(loader.submit(broken, "load failed"))
                self.assertTrue(failures.wait(3))
                self.assertTrue(loader.submit(completed.set))
                self.assertTrue(completed.wait(3))
            self.assertTrue(loader.is_alive())
            self.assertEqual(errors, [("load failed", ("load failed",))])
            self.assertEqual(len(logs.records), 2)
        finally:
            loader.close()
            loader._thread.join(3)
        self.assertFalse(loader.is_alive())
        self.assertFalse(loader.submit(completed.set))

    def test_cancel_after_chunk_does_not_cache_or_publish_unit(self):
        token = CancellationToken()
        backend = Backend()
        backend.split_sentence = lambda text, direction: ["long ", "sen", "tence"]
        original = backend.translate_chunk
        def chunk(text, direction):
            result = original(text, direction)
            token.cancel()
            return result
        backend.translate_chunk = chunk
        service = TranslationService(backend, snapshot_loader=lambda _: None)
        events = []
        with self.assertRaises(TranslationCancelled):
            service.translate_stream("long sentence", cancellation_token=token,
                                     on_sentence=lambda *args: events.append(args))
        self.assertEqual(backend.calls, ["long"])
        self.assertEqual([event[0] for event in events], ["start"])
        self.assertEqual(len(service.translation_cache), 0)
        with self.assertRaises(TranslationCancelled):
            service.translate("another text", cancellation_token=token)
        self.assertEqual(backend.calls, ["long"])

    def test_cancel_between_units_and_before_first_chunk(self):
        backend = Backend()
        token = CancellationToken()
        service = TranslationService(backend, snapshot_loader=lambda _: None)
        def after_unit(phase, *args):
            if phase == "done":
                token.cancel()
        with self.assertRaises(TranslationCancelled):
            service.translate_stream("One. Two.", on_sentence=after_unit,
                                     cancellation_token=token)
        self.assertEqual(backend.calls, ["One."])
        token = CancellationToken()
        backend.calls.clear()
        backend.split_sentence = lambda text, direction: (token.cancel() or [text])
        with self.assertRaises(TranslationCancelled):
            service.translate("uncached", cancellation_token=token)
        self.assertEqual(backend.calls, [])

    def test_inference_chunk_boundaries_preserve_layout(self):
        backend = Backend()
        backend.split_sentence = lambda text, direction: split_sentence_to_chunks(text, len, 8)
        service = TranslationService(backend, snapshot_loader=lambda _: None)
        for source in ["  alpha    beta\t\tgamma.\r\n", "averylongwordwithoutspaces",
                       "left\t\t right\nlast", "aaaaaaa       b", "🙂" * 20]:
            with self.subTest(source=source):
                self.assertEqual(service.translate(source), source.upper())
                self.assertEqual(service.translate_stream(source), source.upper())
        self.assertTrue(all(call == call.strip() for call in backend.calls))
        # Formatting supplied by inference must not duplicate source separators.
        backend.translate_chunk = lambda text, direction: " \t" + text.upper() + " \n"
        self.assertEqual(service.translate("new    words"), "NEW    WORDS")

    def test_lossless_layout(self):
        rng = random.Random(42)
        for _ in range(200):
            text = rng.choice(["", "  ", "\t"]) + rng.choice([" ", "\n", "\r\n", "\n \n", "\t"]).join(
                rng.choices(["Hello.", "Текст!", "🙂?", "no punctuation"], k=8)) + "\n\t"
            units = split_units(text)
            self.assertEqual(assemble_output(units, [u.text for u in units]), text)
            for unit in units:
                self.assertEqual(text[unit.start:unit.end], unit.text)

    def test_chunk_properties(self):
        for text in ["", "a\t b\n c", "🙂" * 100, "a" * 100, "word,  next; end"]:
            chunks = split_sentence_to_chunks(text, len, 8)
            self.assertEqual("".join(chunks), text)
            self.assertTrue(all(len(chunk) <= 8 for chunk in chunks))
        text = "a abcdef z"
        counter = lambda value: 3 if value == "a abcdef " else len(value)
        self.assertEqual("".join(split_sentence_to_chunks(text, counter, 3)), text)
        with self.assertRaises(ValueError):
            split_sentence_to_chunks("x", len, 0)

    def test_abbreviations(self):
        self.assertEqual([u.text for u in split_units("Dr. Smith uses e.g. examples. Next.")],
                         ["Dr. Smith uses e.g. examples.", "Next."])

    def test_lru_limits(self):
        cache = TranslationCache(max_entries=2, max_chars=20)
        keys = [cache.make_key("m", "en-ru", value) for value in ("a", "b", "c")]
        cache.put(keys[0], "A")
        cache.put(keys[1], "B")
        cache.get(keys[0])
        cache.put(keys[2], "C")
        self.assertNotIn(keys[1], cache)
        cache.put(keys[0], "x" * 21)
        self.assertNotIn(keys[0], cache)
        cache.clear()
        self.assertEqual(len(cache), 0)

    def test_api_layout_protected_tokens_and_lazy_loading(self):
        backend = Backend()
        service = TranslationService(backend, snapshot_loader=lambda _: None, auto_load=False)
        self.assertEqual(backend.loads, 0)
        url = "https://example.com/" + "a" * 1000
        source = "  hello.\nsee " + url + " now.\n\nlast\t\n"
        expected = "  HELLO.\nSEE " + url + " NOW.\n\nLAST\t\n"
        self.assertEqual(service.translate(source), expected)
        self.assertEqual(service.translate_stream(source), expected)
        self.assertEqual(backend.loads, 1)
        self.assertFalse(any(url in call for call in backend.calls))
        for method in (service.translate, service.translate_stream):
            with self.assertRaises(TranslationError):
                method("fail")

    def test_dictionary_integrity(self):
        self.assertIsNone(build_snapshot({"hello": "привет", "привет": "hi", "hi": "здравствуйте"}))
        snapshot = build_snapshot({"hello": "привет", "привет": "hello"})
        self.assertEqual(snapshot.lookup("hello"), "привет")
        self.assertEqual(snapshot.lookup("привет"), "hello")

    def test_dictionary_layout(self):
        snapshot = build_snapshot({"hello": "привет"})
        backend = Backend()
        service = TranslationService(backend, snapshot_loader=lambda _: snapshot)
        self.assertEqual(service.translate("  hello\r\n"), "  привет\r\n")
        events = []
        result = service.translate_stream("  hello\r\n", on_sentence=lambda *args: events.append(args))
        self.assertEqual(result, "  привет\r\n")
        unit = events[-1][-1]
        self.assertEqual((unit.src_start, unit.src_end), (2, 7))
        self.assertEqual(unit.separator_before + unit.translation + unit.separator_after, result)
        self.assertFalse(backend.calls)

    def test_history_location_and_retention(self):
        from history import HistoryManager
        with tempfile.TemporaryDirectory() as folder, patch("dictionary_manager.user_data_dir", return_value=folder):
            manager = HistoryManager()
            self.assertEqual(manager.db_path, os.path.join(folder, "history.db"))
            for index in range(60):
                self.assertTrue(manager.add_translation(str(index), "result", "en-ru"))
            rows = manager.get_history()
            self.assertEqual(len(rows), 50)
            self.assertEqual([row[0] for row in rows], list(range(60, 10, -1)))

    def test_single_instance_lock(self):
        with tempfile.TemporaryDirectory() as folder, patch("single_instance.user_data_dir", return_value=folder):
            first = SingleInstanceGuard()
            second = SingleInstanceGuard()
            self.assertTrue(first.acquired)
            self.assertFalse(second.acquired)
            first.close()
            third = SingleInstanceGuard()
            self.assertTrue(third.acquired)
            third.close()


if __name__ == "__main__":
    unittest.main()
