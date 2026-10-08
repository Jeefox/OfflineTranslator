"""Run standalone regression suites in isolated processes (use Xvfb for GUI)."""
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    failed = []
    for suite in sorted((root / "tests").glob("test_*.py")):
        print(f"\nRunning {suite.name}", flush=True)
        result = subprocess.run([sys.executable, str(suite)], cwd=root)
        if result.returncode:
            failed.append(suite.name)
    if failed:
        print("Failed: " + ", ".join(failed), file=sys.stderr)
    return bool(failed)


if __name__ == "__main__":
    sys.exit(main())
