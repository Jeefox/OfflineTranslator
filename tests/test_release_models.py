"""Раздельные артефакты релиза и локальная загрузка без сети."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from zipfile import ZipFile
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build
from app_icons import asset_path, application_icon, action_icon
import local_models
from model_registry import ModelManager
from backends.marian import MarianBackend
from scripts.package_release_models import package_models
from scripts.download_release_models import MODELS
from scripts.split_release_archive import split_archive

with tempfile.TemporaryDirectory(prefix="release-models-") as temporary:
    root = Path(temporary)
    for repo in MODELS:
        folder = root / "cache" / ("models--" + repo.replace("/", "--"))
        (folder / "refs").mkdir(parents=True)
        (folder / "refs" / "main").write_text("revision", encoding="utf-8")
        snapshot = folder / "snapshots" / "revision"
        snapshot.mkdir(parents=True)
        for name, data in (("config.json", b'{"model_type":"marian"}'), ("model.safetensors", b'weights'),
                           ("source.spm", b'tokenizer'), ("target.spm", b'tokenizer'),
                           ("tokenizer_config.json", b'{}'), ("vocab.json", b'{}')):
            (snapshot / name).write_bytes(data)
    archive = package_models(root / "cache", root / "models.zip")
    with ZipFile(archive) as packed:
        assert "models/marian-en-ru/config.json" in packed.namelist()
        assert "models/marian-ru-en/model.safetensors" in packed.namelist()
        packed.extractall(root / "portable")
    assert split_archive(archive) == [archive]
    assert archive.exists()
    source = root / "large.bin"
    source.write_bytes(b'1234567890')
    parts = split_archive(source, 4)
    assert b''.join(part.read_bytes() for part in parts) == b'1234567890'

    own = root / "portable" / "models" / "marian-en-ru"
    local_models.configure_paths(en_ru=str(own))
    manager = ModelManager(cache_dir=str(root / "empty-cache"))
    assert manager.is_model_available("marian-en-ru")
    local_models.configure_paths(en_ru=str(root / "missing"))
    assert not manager.is_model_available("marian-en-ru")
    local_models.configure_paths(en_ru=str(own))
    backend = MarianBackend(cache_dir=str(root / "empty-cache"), directions=("en-ru",))
    model = MagicMock()
    model.config = SimpleNamespace(max_position_embeddings=512)
    with patch("backends.marian.AutoTokenizer.from_pretrained") as tokenizer_load, \
            patch("backends.marian.AutoModelForSeq2SeqLM.from_pretrained", return_value=model) as model_load, \
            patch("backends.marian.torch.cuda.is_available", return_value=False):
        backend.load()
        assert model_load.call_count == tokenizer_load.call_count == 1
        assert model_load.call_args.args[0] == str(own)
        assert model_load.call_args.kwargs["use_safetensors"] is True
        assert model_load.call_args.kwargs["trust_remote_code"] is False
        assert model_load.call_args.kwargs["local_files_only"] is True
        assert tokenizer_load.call_args.kwargs["local_files_only"] is True
    local_models.configure_paths()
    with patch.object(sys, "frozen", True, create=True), \
            patch.object(sys, "executable", str(root / "portable" / "OfflineTranslator")):
        assert local_models.local_path("en-ru") == str(own)
    with patch.object(sys, "frozen", True, create=True), \
            patch("local_models.local_path", return_value=None), \
            patch("backends.marian.AutoTokenizer.from_pretrained") as tokenizer_load, \
            patch("backends.marian.AutoModelForSeq2SeqLM.from_pretrained", return_value=model):
        backend._load_model_direction("en-ru", backend.DIRECTIONS["en-ru"])
        assert tokenizer_load.call_args.kwargs["local_files_only"] is True

    dist = root / "dist"
    dist.mkdir()
    binary = dist / ("OfflineTranslator.exe" if build.os.name == "nt" else "OfflineTranslator")
    binary.write_bytes(b'executable')
    with patch.object(build, "BUILD_DIR", root / "new-build"):
        icon = build.prepare_icon()
        assert icon.read_bytes() == asset_path("offline_translator.ico").read_bytes()
        with Image.open(icon) as image:
            assert image.format == "ICO"
            assert image.ico.sizes() == {(n, n) for n in (16, 24, 32, 48, 64, 128, 256)}
    with Image.open(asset_path("offline_translator.png")) as image:
        assert image.size == (256, 256)
        assert image.tobytes() == application_icon().tobytes()
    with patch.object(sys, "_MEIPASS", str(root), create=True):
        assert asset_path("offline_translator.png") == root / "assets" / "offline_translator.png"
    for name in ("settings", "swap", "copy", "clear"):
        image = action_icon(name, "#ffffff")
        assert image.size == (24, 24) and image.mode == "RGBA"
        assert image.getbbox() is not None
        assert image.getpixel((0, 0))[3] == 0
    with patch.object(build, "DIST_DIR", dist), \
            patch("build.subprocess.run") as invoke:
        build.build([])
        args = invoke.call_args.args[0]
        assert "--onefile" in args
        sep = ";" if build.os.name == "nt" else ":"
        assert f"assets{sep}assets" in args
        assert not any("cache" in str(arg) for arg in args)
    (dist / "OfflineTranslator.exe").write_bytes(b'executable')
    with patch.object(build, "DIST_DIR", dist), \
            patch.object(build, "BUILD_DIR", root / "windows-build"), \
            patch.object(build, "os", SimpleNamespace(name="nt", walk=build.os.walk, environ=build.os.environ)), \
            patch("build.subprocess.run") as invoke:
        build.build([])
        args = invoke.call_args.args[0]
        assert "assets;assets" in args
        assert args[args.index("--icon") + 1] == str(root / "windows-build" / "offline_translator.ico")
local_models.configure_paths()
print("OK: separate release models, offline sources and model-free build passed")
