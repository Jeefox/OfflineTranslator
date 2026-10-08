"""Shared inference, failure delivery and independent cancellation of callers."""
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import translation_service as service


class BlockingBackend:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.fail = False
        self.cancel_owner = None
    def load(self): pass
    def split_sentence(self, text, direction): return [text]
    def translate_chunk(self, text, direction):
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            if not self.release.wait(3): raise RuntimeError("test timed out")
            if self.cancel_owner: self.cancel_owner.cancel()
        if self.fail: raise RuntimeError("native failure")
        return text.upper()


class ConcurrentTests(unittest.TestCase):
    def test_shared_success_failure_and_cancellation(self):
        for scenario in ("uncached_success", "failure", "cancel_waiter", "cancel_owner"):
            with self.subTest(scenario=scenario):
                backend = BlockingBackend()
                translator = service.TranslationService(backend, snapshot_loader=lambda _: None)
                # Deduplication cannot rely solely on the LRU retaining the output.
                translator.translation_cache.max_entries = 0
                joined = threading.Event()
                class ObservedFuture(Future):
                    def exception(self, *args, **kwargs):
                        joined.set()
                        return super().exception(*args, **kwargs)
                owner_token, waiter_token = service.CancellationToken(), service.CancellationToken()
                backend.fail = scenario == "failure"
                if scenario == "cancel_owner": backend.cancel_owner = owner_token
                with patch.object(service, "Future", ObservedFuture), ThreadPoolExecutor(2) as pool:
                    owner = pool.submit(translator.translate, "same", cancellation_token=owner_token)
                    self.assertTrue(backend.started.wait(2))
                    waiter = pool.submit(translator.translate, "same", cancellation_token=waiter_token)
                    try:
                        self.assertTrue(joined.wait(2))
                        if scenario == "cancel_waiter":
                            waiter_token.cancel()
                            with self.assertRaises(service.TranslationCancelled): waiter.result(2)
                        self.assertEqual(backend.calls, 1)
                    finally:
                        backend.release.set()
                    if scenario == "failure":
                        for future in (owner, waiter):
                            with self.assertRaises(service.TranslationError): future.result(2)
                        backend.fail = False
                        self.assertEqual(translator.translate("same"), "SAME")
                        self.assertEqual(backend.calls, 2)
                    elif scenario == "cancel_owner":
                        with self.assertRaises(service.TranslationCancelled): owner.result(2)
                        self.assertEqual(waiter.result(2), "SAME")
                        self.assertEqual(backend.calls, 2)
                    else:
                        self.assertEqual(owner.result(2), "SAME")
                        if scenario != "cancel_waiter": self.assertEqual(waiter.result(2), "SAME")
                        self.assertEqual(backend.calls, 1)

    def test_different_requests_never_overlap_native_inference(self):
        backend = BlockingBackend()
        active, peak = 0, 0
        mutex = threading.Lock()
        second_entered = threading.Event()
        def chunk(text, direction):
            nonlocal active, peak
            with mutex:
                active += 1
                peak = max(peak, active)
                backend.calls += 1
                if backend.calls == 1: backend.started.set()
                else: second_entered.set()
            try:
                if not backend.release.wait(3): raise RuntimeError("test timeout")
                return text.upper()
            finally:
                with mutex: active -= 1
        backend.translate_chunk = chunk
        translator = service.TranslationService(backend, snapshot_loader=lambda _: None)
        barrier = threading.Barrier(2)
        def event(phase, *args):
            if phase == "start": barrier.wait(2)
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(translator.translate_stream, "first", on_sentence=event)
            second = pool.submit(translator.translate_stream, "second", on_sentence=event)
            try:
                self.assertTrue(backend.started.wait(2))
                self.assertFalse(second_entered.wait(0.1))
            finally:
                backend.release.set()
            self.assertEqual(first.result(2), "FIRST")
            self.assertEqual(second.result(2), "SECOND")
        self.assertEqual(peak, 1)
        self.assertEqual(backend.calls, 2)

    def test_distinct_units_are_serialized_and_wait_can_be_cancelled(self):
        backend = BlockingBackend()
        translator = service.TranslationService(backend, snapshot_loader=lambda _: None)
        token = service.CancellationToken()
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(translator.translate, "first")
            self.assertTrue(backend.started.wait(2))
            waiting = pool.submit(translator.translate, "second", cancellation_token=token)
            try:
                token.cancel()
                with self.assertRaises(service.TranslationCancelled): waiting.result(2)
                self.assertEqual(backend.calls, 1)
            finally:
                backend.release.set()
            self.assertEqual(first.result(2), "FIRST")
        self.assertEqual(translator.translate("second"), "SECOND")
        self.assertEqual(backend.calls, 2)


if __name__ == "__main__":
    unittest.main()
