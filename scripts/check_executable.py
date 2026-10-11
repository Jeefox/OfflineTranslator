"""Run the packaged GUI, checking its report even for Windows --windowed."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def check_executable(executable, *, frozen=True):
    executable = Path(executable).resolve()
    if not executable.is_file() or executable.stat().st_size == 0:
        raise RuntimeError(f"Missing/empty executable: {executable}")
    with tempfile.TemporaryDirectory(prefix="offline-executable-check-") as directory:
        report = Path(directory) / "smoke.json"
        command = ([sys.executable, str(executable)] if executable.suffix == ".py"
                   else [str(executable)])
        process = subprocess.run(command + ["--smoke-test", str(report)],
                                 cwd=directory, timeout=120, check=False)
        if not report.is_file():
            raise RuntimeError(f"Executable exited {process.returncode} without a smoke report")
        result = json.loads(report.read_text(encoding="utf-8"))
        expected = {"marian_runtime_import", "main_window_and_textbox",
                    "settings_reopen_and_bindings", "tooltip"}
        if (process.returncode or result.get("ok") is not True
                or result.get("frozen") is not frozen or set(result.get("checks", [])) != expected):
            raise RuntimeError(f"Executable smoke failed: {result!r}")
        print("OK: executable GUI smoke: " + ", ".join(result["checks"]))


if __name__ == "__main__":
    check_executable(sys.argv[1])
