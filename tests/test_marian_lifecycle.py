"""Per-direction limits and atomic loading without downloading model weights."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backends.marian as marian


class Tokenizer:
    def __init__(self):
        self.input_limits = []

    def __call__(self, source, **kwargs):
        if "return_tensors" not in kwargs:
            return {"input_ids": list(range(len(source)))}
        self.input_limits.append(kwargs["max_length"])
        return SimpleNamespace(to=lambda device: {"input_ids": [1, 2]})

    def decode(self, output, **kwargs): return "translated"


class MarianTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.models = {}
        self.tokenizers = {}
        def model(source, **kwargs):
            result = MagicMock()
            result.config.max_position_embeddings = 512 if source.endswith("en-ru") else 1024
            result.generate.return_value = [[1]]
            result.to.return_value = result
            self.models[source] = result
            return result
        def tokenizer(source, **kwargs):
            result = Tokenizer()
            self.tokenizers[source] = result
            return result
        for patcher in (patch.object(marian, "local_path", return_value=None),
                        patch.object(marian, "cached_model_path", return_value=None),
                        patch.object(marian.torch.cuda, "is_available", return_value=False),
                        patch.object(marian.AutoModelForSeq2SeqLM, "from_pretrained", side_effect=model),
                        patch.object(marian.AutoTokenizer, "from_pretrained", side_effect=tokenizer)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.backend = marian.MarianBackend(cache_dir=self.directory.name)

    def test_output_budget_is_configurable_and_decoder_bounded(self):
        self.backend.max_new_tokens = 700
        self.backend.load()
        for direction, expected in (("en-ru", 511), ("ru-en", 700)):
            self.backend.translate_chunk("input", direction)
            model = self.models[self.backend.DIRECTIONS[direction]]
            args = model.generate.call_args.kwargs
            self.assertNotIn("max_length", args)
            self.assertEqual(args["max_new_tokens"], expected)
        for invalid in (0, -1, True, "512"):
            with self.assertRaises(ValueError):
                marian.MarianBackend(cache_dir=self.directory.name, max_new_tokens=invalid)

    def test_partial_failure_preserves_complete_previous_state(self):
        for initially_loaded in (False, True):
            with self.subTest(initially_loaded=initially_loaded):
                if initially_loaded:
                    self.backend.load()
                old_models = self.backend._models
                old_limits = self.backend._max_source_tokens
                old_device = self.backend.device
                original = self.backend._load_model_direction
                def load(direction, source, **kwargs):
                    if direction == "ru-en":
                        raise RuntimeError("second direction failed")
                    return original(direction, source, **kwargs)
                with patch.object(self.backend, "_load_model_direction", side_effect=load), \
                        patch.object(marian.torch.cuda, "is_available", return_value=True), \
                        patch.object(marian.torch.cuda, "get_device_name", return_value="fake GPU"):
                    with self.assertRaisesRegex(RuntimeError, "second direction failed"):
                        self.backend.load()
                self.assertIs(self.backend._models, old_models)
                self.assertIs(self.backend._max_source_tokens, old_limits)
                self.assertEqual(self.backend.device, old_device)

    def test_direction_limits_used_for_splitting_and_inference(self):
        self.backend.load()
        self.assertEqual(self.backend.max_source_tokens_for("en-ru"), 480)
        self.assertEqual(self.backend.max_source_tokens_for("ru-en"), 992)
        self.assertEqual(self.backend.max_source_tokens, 480)
        for direction, limit in (("en-ru", 480), ("ru-en", 992)):
            source = "x" * 900
            chunks = self.backend.split_sentence(source, direction)
            self.assertEqual("".join(chunks), source)
            self.assertTrue(all(len(chunk) <= limit for chunk in chunks))
            self.backend.translate_chunk(chunks[0], direction)
            tokenizer = self.tokenizers[self.backend.DIRECTIONS[direction]]
            self.assertEqual(tokenizer.input_limits, [limit])
        self.assertEqual(len(self.backend.split_sentence("x" * 900, "ru-en")), 1)


if __name__ == "__main__":
    unittest.main()
