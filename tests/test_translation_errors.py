"""Public errors preserve causes; user messages never include diagnostics."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from translation_service import TranslationService, TranslationError
from translation_errors import BackendError, ModelLoadError, ModelUnavailableError, to_user_message
from model_registry import ModelUnavailableError as RegistryUnavailableError


class Backend:
    def load(self): pass
    def split_sentence(self, source, direction): return [source]
    def translate_chunk(self, source, direction):
        raise OSError("/private/path native diagnostic")


class ErrorTests(unittest.TestCase):
    def test_api_errors_keep_causes_and_safe_user_messages(self):
        service = TranslationService(Backend(), snapshot_loader=lambda _: None)
        for method in (service.translate, service.translate_stream):
            with self.assertRaises(BackendError) as raised:
                method("input")
            error = raised.exception
            self.assertIsInstance(error, TranslationError)
            self.assertIsInstance(error.__cause__, OSError)
            self.assertIn("/private/path", str(error))
            self.assertNotIn("private", to_user_message(error))
        self.assertNotIn("secret", to_user_message(RuntimeError("secret")))
        self.assertIs(RegistryUnavailableError, ModelUnavailableError)

    def test_load_error_categories(self):
        for error, expected in ((FileNotFoundError("secret"), ModelUnavailableError),
                                (ImportError("secret"), ModelUnavailableError),
                                (RuntimeError("secret"), ModelLoadError)):
            backend = Backend()
            def load(): raise error
            backend.load = load
            with self.assertRaises(expected) as raised:
                TranslationService(backend)
            self.assertIs(raised.exception.__cause__, error)
            self.assertNotIn("secret", to_user_message(raised.exception))


if __name__ == "__main__":
    unittest.main()
