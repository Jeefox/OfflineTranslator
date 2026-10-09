"""Safe-weight policy plus a real tiny Marian model on the pinned runtime."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import torch
from transformers import MarianConfig, MarianMTModel
from local_models import has_transformers_model, configure_paths
from backends.marian import MarianBackend
from scripts.download_release_models import prepare_release_model
from scripts.package_release_models import package_models
from packaging.version import Version

assert Version(torch.__version__.split("+")[0]) >= Version("2.10.0")
with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    (root / "config.json").write_text('{"model_type":"marian"}')
    (root / "pytorch_model.bin").write_bytes(b'not a checkpoint')
    assert not has_transformers_model(root)
    configure_paths(en_ru=str(root))
    backend = MarianBackend(cache_dir=str(root / "cache"), directions=("en-ru",))
    with patch("backends.marian.AutoModelForSeq2SeqLM.from_pretrained") as unsafe_load:
        try:
            backend.load()
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("Unsafe-only model was accepted")
        unsafe_load.assert_not_called()
    config = MarianConfig(vocab_size=32, d_model=16, encoder_layers=1, decoder_layers=1,
                          encoder_attention_heads=2, decoder_attention_heads=2,
                          encoder_ffn_dim=32, decoder_ffn_dim=32,
                          max_position_embeddings=32, pad_token_id=0, eos_token_id=2,
                          decoder_start_token_id=0)
    model = MarianMTModel(config)
    model.save_pretrained(root, safe_serialization=True)
    for name, data in (("tokenizer_config.json", b'{}'), ("vocab.json", b'{}'),
                       ("source.spm", b'tokenizer'), ("target.spm", b'tokenizer')):
        (root / name).write_bytes(data)
    assert has_transformers_model(root)
    fake_tokenizer = MagicMock()
    with patch("backends.marian.AutoTokenizer.from_pretrained", return_value=fake_tokenizer), \
            patch("backends.marian.torch.cuda.is_available", return_value=False):
        backend.load()  # real from_pretrained, while an invalid .bin is present
    output = backend._models["en-ru"][0].generate(torch.tensor([[3, 4, 2]]), max_new_tokens=4)
    assert output.shape[0] == 1
    with patch("scripts.download_release_models.snapshot_download") as download:
        try:
            prepare_release_model("untrusted/arbitrary-model", root)
        except ValueError:
            pass
        else:
            raise AssertionError("Converter accepted arbitrary source")
        download.assert_not_called()
    with patch.object(torch, "__version__", "2.9.1"), \
            patch("scripts.download_release_models.snapshot_download") as download:
        try:
            prepare_release_model("Helsinki-NLP/opus-mt-en-ru", root)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Converter accepted vulnerable runtime")
        download.assert_not_called()
configure_paths()
print("OK: safetensors-only policy and real Marian inference on", torch.__version__)
