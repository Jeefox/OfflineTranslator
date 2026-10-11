"""Release/install paths must not silently downgrade patched dependency pins."""
from pathlib import Path
import re
import sys

from packaging.version import Version
import yaml

ROOT = Path(__file__).resolve().parents[1]

def pins(filename):
    return dict(re.findall(r"^([\w-]+)==([^\s#]+)", (ROOT / filename).read_text(), re.M))

runtime = pins("requirements.txt")
tools = pins("requirements-build-tools.txt")
assert Version(runtime["torch"]) >= Version("2.13.0")
assert Version(tools["pip"]) >= Version("26.2.1")
assert Version(tools["setuptools"]) >= Version("84.0.0")
assert runtime["setuptools"] == tools["setuptools"]
assert "-r requirements-build-tools.txt" in (ROOT / "requirements-release.txt").read_text()

workflow = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
for name in ("test", "build-smoke", "build", "gguf-build", "models"):
    commands = [step["run"] for step in workflow["jobs"][name]["steps"] if "run" in step]
    torch_steps = [command for command in commands if "torch==" in command]
    assert len(torch_steps) == 1, name
    assert f'torch=={runtime["torch"]}' in torch_steps[0], name
    bootstrap = "python -m pip install -r requirements-build-tools.txt"
    assert commands.index(bootstrap) < commands.index(torch_steps[0]), name

release_audit = [step["run"] for step in workflow["jobs"]["test"]["steps"]
                 if "pip_audit" in step.get("run", "")]
assert len(release_audit) == 1 and "--ignore-vuln" not in release_audit[0]
gguf_audit = [step["run"] for step in workflow["jobs"]["gguf-build"]["steps"]
              if "pip_audit" in step.get("run", "")]
assert gguf_audit == ["python -m pip_audit --vulnerability-service osv --ignore-vuln GHSA-w8v5-vhqr-4h9v"]
for filename in ("build.bat", "build.py", "README.md"):
    text = (ROOT / filename).read_text()
    assert set(re.findall(r"torch==([\d.]+)", text)) == {runtime["torch"]}, filename
    assert "requirements-build-tools.txt" in text, filename
release_steps = workflow["jobs"]["release"]["steps"]
publish_index = next(i for i, step in enumerate(release_steps)
                     if step.get("uses", "").startswith("softprops/action-gh-release@"))
assert any(step.get("uses", "").startswith("actions/checkout@")
           for step in release_steps[:publish_index]), "Release guide must be checked out before publishing"
publish = release_steps[publish_index]["with"]
assert publish["body_path"] == ".github/RELEASE_GUIDE.md" and "body" not in publish
guide = (ROOT / publish["body_path"]).read_text(encoding="utf-8")
for required in ("OfflineTranslator-Models-Marian.zip", "model.safetensors",
                 "SHA256SUMS.txt", "Helsinki-NLP/opus-mt-en-ru", "Helsinki-NLP/opus-mt-ru-en",
                 "tencent/Hy-MT2-1.8B-GGUF", "scripts.download_release_models"):
    assert required in guide, required
print("OK: patched pins and dependency audits agree across release/install paths")
