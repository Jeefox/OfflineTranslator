"""Real source GUI smoke, error protocol, service isolation and CI wiring."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main
from scripts.gui_smoke import run_smoke
from scripts.check_executable import check_executable
from scripts.build_manifest import write_manifest

with tempfile.TemporaryDirectory() as directory:
    report = Path(directory) / "smoke.json"
    with patch.object(main.TranslatorApp, "_start_model_load", side_effect=AssertionError("model load")), \
            patch.object(main.TranslatorApp, "_start_hotkey_agent", side_effect=AssertionError("hotkey")), \
            patch.object(main.SystemTray, "start", side_effect=AssertionError("tray")):
        assert run_smoke(main.TranslatorApp, report) == 0, report.read_text()
    assert json.loads(report.read_text())["ok"] is True
    class FailApp:
        def __init__(self, **kwargs):
            raise RuntimeError("expected startup failure")
    assert run_smoke(FailApp, report) == 1
    assert "expected startup failure" in json.loads(report.read_text())["error"]
    # Optional inference must fail honestly when local weights are missing;
    # ordinary GUI smoke remains independent of downloaded models.
    assert run_smoke(main.TranslatorApp, report, model_root=Path(directory) / "missing-models") == 1
    failed_inference = json.loads(report.read_text())
    assert failed_inference["ok"] is False and "error" in failed_inference
    assert "real_marian_gui_translation_both_directions" not in failed_inference["checks"]
    check_executable(ROOT / "main.py", frozen=False)
    # A process returning zero without an honest report must not pass.
    fake = Path(directory) / "fake.py"
    fake.write_text("pass\n")
    try:
        check_executable(fake, frozen=False)
    except RuntimeError as error:
        assert "without a smoke report" in str(error)
    else:
        raise AssertionError("Missing smoke report accepted")
    write_manifest(directory, "Test")
    bom = json.loads((Path(directory) / "OfflineTranslator-Test.sbom.cdx.json").read_text())
    assert bom["bomFormat"] == "CycloneDX" and bom["specVersion"] == "1.6"
    pins = (Path(directory) / "OfflineTranslator-Test.resolved.txt").read_text()
    env = json.loads((Path(directory) / "OfflineTranslator-Test.build-environment.json").read_text())
    assert env["packages"] and len(bom["components"]) == len(env["packages"])
    for component in bom["components"]:
        assert env["packages"][component["name"]] == component["version"]
        assert f'{component["name"]}=={component["version"]}\n' in pins
    assert "torch" in env["packages"] and "customtkinter" in env["packages"]

workflow = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
for job in ("build-smoke", "build", "gguf-build"):
    steps = workflow["jobs"][job]["steps"]
    commands = [step.get("run", "") for step in steps]
    build_index = commands.index("python build.py")
    smoke_indices = [i for i, command in enumerate(commands) if "scripts/check_executable.py" in command]
    assert smoke_indices and all(i > build_index for i in smoke_indices), job
    if job != "build-smoke":
        assert any("scripts/build_manifest.py" in command for command in commands), job
        paths = "\n".join(step.get("with", {}).get("path", "") for step in steps)
        assert ".sbom.cdx.json" in paths and ".resolved.txt" in paths and ".build-environment.json" in paths
print("OK: GUI smoke isolation/error protocol, manifest and release CI wiring")
