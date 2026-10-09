"""One filesystem validator for availability, backend and sharded weights."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_models import validate_transformers_model, configure_paths
from model_registry import ModelManager
from scripts.package_release_models import package_models


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name, value in (("config.json", '{"model_type":"marian"}'),
                            ("tokenizer_config.json", '{}'), ("vocab.json", '{}'),
                            ("source.spm", 'spm'), ("target.spm", 'spm'),
                            ("model.safetensors", 'weights')):
            (self.root / name).write_text(value)

    def tearDown(self):
        configure_paths()
        self.temporary.cleanup()

    def test_complete(self):
        result = validate_transformers_model(self.root)
        self.assertTrue(result.available and result.complete)
        self.assertIn("model.safetensors", result.files)
        configure_paths(en_ru=str(self.root))
        self.assertTrue(ModelManager().is_model_available("marian-en-ru"))

    def test_tokenizer_files(self):
        for name in ("tokenizer_config.json", "vocab.json", "source.spm", "target.spm"):
            file = self.root / name
            data = file.read_bytes()
            file.unlink()
            result = validate_transformers_model(self.root)
            self.assertFalse(result.available)
            self.assertIn(name, result.reason)
            file.write_bytes(data)

    def test_shards(self):
        (self.root / "model.safetensors").unlink()
        (self.root / "model.safetensors.index.json").write_text(json.dumps({
            "weight_map": {"encoder": "a.safetensors", "decoder": "b.safetensors"}}))
        (self.root / "a.safetensors").write_bytes(b'a')
        self.assertFalse(validate_transformers_model(self.root).available)
        (self.root / "b.safetensors").write_bytes(b'b')
        self.assertTrue(validate_transformers_model(self.root).available)
        (self.root / "b.safetensors").write_bytes(b'')
        self.assertFalse(validate_transformers_model(self.root).available)

    def test_bad_indexes(self):
        (self.root / "model.safetensors").unlink()
        for data in ({}, {"weight_map": []}, {"weight_map": {}},
                     {"weight_map": {"x": "../outside.safetensors"}},
                     {"weight_map": {"x": "pytorch_model.bin"}},
                     {"weight_map": {"x": 123}}):
            (self.root / "model.safetensors.index.json").write_text(json.dumps(data))
            self.assertFalse(validate_transformers_model(self.root).available)
        (self.root / "model.safetensors.index.json").write_text('broken')
        self.assertFalse(validate_transformers_model(self.root).available)

    def test_invalid_config_and_architecture(self):
        for config in ('broken', '[]', '{"model_type":"bart"}'):
            (self.root / "config.json").write_text(config)
            self.assertFalse(validate_transformers_model(self.root).available)

    def test_separate_vocab(self):
        (self.root / "tokenizer_config.json").write_text('{"separate_vocabs":true}')
        self.assertFalse(validate_transformers_model(self.root).available)
        (self.root / "target_vocab.json").write_text('{}')
        self.assertTrue(validate_transformers_model(self.root).available)


if __name__ == "__main__":
    unittest.main()
