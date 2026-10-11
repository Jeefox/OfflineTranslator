"""Build-environment inventory, including tooling (not a binary-only SBOM)."""
from importlib.metadata import distributions
import json
from pathlib import Path
import platform
import re
import sys
import sysconfig
from urllib.parse import quote


def write_manifest(directory, label):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    packages = {}
    # Runtime imports (notably setuptools) can add vendored libraries to
    # sys.path. Inventory installed top-level packages, not their private copies.
    installed_paths = sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
    for distribution in distributions(path=installed_paths):
        name = re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower()
        version = distribution.version
        if name in packages and packages[name] != version:
            raise RuntimeError(f"Ambiguous installed versions for {name}")
        packages[name] = version
    packages = dict(sorted(packages.items()))
    components = [{"type": "library", "name": name, "version": version,
                   "purl": f"pkg:pypi/{name}@{quote(version, safe='')}"}
                  for name, version in packages.items()]
    stem = directory / f"OfflineTranslator-{label}"
    sbom = {"$schema": "http://cyclonedx.org/schema/bom-1.6.schema.json",
            "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
            "metadata": {"component": {"type": "application", "name": "OfflineTranslator-build-environment",
                "description": "Installed Python build environment including tooling; not the exact binary contents."}},
            "components": components}
    Path(str(stem) + ".sbom.cdx.json").write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
    Path(str(stem) + ".resolved.txt").write_text(
        "# Observed build environment; includes tooling, use the original package indexes.\n"
        + "".join(f"{name}=={version}\n" for name, version in packages.items()), encoding="utf-8")
    environment = {"python": platform.python_version(), "platform": sys.platform,
                   "machine": platform.machine(), "packages": packages}
    Path(str(stem) + ".build-environment.json").write_text(json.dumps(environment, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_manifest(sys.argv[1], sys.argv[2])
