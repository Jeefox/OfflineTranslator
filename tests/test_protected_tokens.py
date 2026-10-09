"""Sentence-context protection and explicit rejection of damaged markers."""
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protected_tokens import mask_tokens, restore_tokens
from translation_service import TranslationService, TranslationError


class Backend:
    def __init__(self):
        self.calls = []

    def load(self): pass
    def split_sentence(self, source, direction): return [source]
    def translate_chunk(self, source, direction):
        self.calls.append(source)
        return source.upper()


class ProtectedTokensTests(unittest.TestCase):
    def test_full_sentence_context(self):
        backend = Backend()
        service = TranslationService(backend, snapshot_loader=lambda _: None)
        source = "Open https://example.com for details and email me@example.com."
        result = service.translate(source)
        self.assertEqual(result, "OPEN https://example.com FOR DETAILS AND EMAIL me@example.com.")
        self.assertEqual(len(backend.calls), 1)
        self.assertTrue(backend.calls[0].startswith("Open "))
        self.assertTrue(backend.calls[0].endswith("."))
        self.assertNotIn("https://", backend.calls[0])
        self.assertNotIn("me@example.com", backend.calls[0])

    def test_repeated_tokens_and_source_marker_collisions(self):
        source = "314159, `314160`, `314160` and https://example.com."
        masked, mapping = mask_tokens(source)
        self.assertEqual(restore_tokens(masked, mapping), source)
        self.assertEqual(len(mapping), 3)
        self.assertFalse(any(marker in source for marker in mapping))
        # Reordered placeholders follow the translated grammar.
        markers = list(mapping)
        self.assertEqual(restore_tokens(" ".join(reversed(markers)), mapping),
                         " ".join(mapping[marker] for marker in reversed(markers)))

    def test_url_punctuation_stays_in_sentence_context(self):
        for source, url, suffix in [
            ("See https://example.com/docs, next.", "https://example.com/docs", ","),
            ("See (https://example.com/docs).", "https://example.com/docs", ")."),
            ("See https://example.com/path_(x).", "https://example.com/path_(x)", "."),
            ("See https://example.com/docs; next.", "https://example.com/docs", ";"),
            ("See https://example.com/docs: next.", "https://example.com/docs", ":"),
        ]:
            with self.subTest(source=source):
                masked, mapping = mask_tokens(source)
                self.assertEqual(list(mapping.values()), [url])
                marker = next(iter(mapping))
                self.assertIn(marker + suffix, masked)
                self.assertEqual(restore_tokens(masked, mapping), source)

    def test_damaged_or_duplicated_marker_is_rejected_and_not_cached(self):
        for mutation in (lambda source: re.sub(r"\d+", "", source),
                         lambda source: source + " 314159"):
            backend = Backend()
            backend.translate_chunk = lambda source, direction: mutation(source)
            service = TranslationService(backend, snapshot_loader=lambda _: None)
            with self.assertRaises(TranslationError):
                service.translate("Open https://example.com for details.")
            self.assertEqual(len(service.translation_cache), 0)


if __name__ == "__main__":
    unittest.main()
