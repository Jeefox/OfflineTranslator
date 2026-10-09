"""Создать отдельный переносимый архив моделей из локального HF-кэша."""
import argparse
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from dictionary_manager import model_cache_dir
from scripts.download_release_models import MODELS


def package_models(cache_dir, output):
    output = Path(output)
    # Проверяем всё до создания архива; нельзя опубликовать неполную пару.
    snapshots = []
    for repo in MODELS:
        root = Path(cache_dir) / ("models--" + repo.replace("/", "--"))
        revision = (root / "refs" / "main").read_text(encoding="utf-8").strip()
        snapshot = root / "snapshots" / revision
        from local_models import validate_transformers_model
        validation = validate_transformers_model(snapshot)
        if not validation.available:
            raise ValueError("Неполная модель: " + validation.reason)
        snapshots.append((repo, snapshot))
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for repo, snapshot in snapshots:
            direction = repo.rsplit("opus-mt-", 1)[-1]
            for file in sorted(snapshot.rglob("*")):
                if file.is_file() and file.suffix.lower() not in (".bin", ".pt", ".pth", ".pkl", ".pickle"):
                    archive.write(file, "models/marian-" + direction + "/" +
                                  file.relative_to(snapshot).as_posix())
        archive.writestr("MODELS-README.txt", "Extract models/ next to OfflineTranslator executable.\n"
                         "Alternatively select each model folder in Settings.\n"
                         "Models: Helsinki-NLP/opus-mt-en-ru and opus-mt-ru-en.\n"
                         "Source and licenses: see README.md inside each model folder.\n")
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default=model_cache_dir())
    parser.add_argument("--output", default="OfflineTranslator-Models-Marian.zip")
    args = parser.parse_args()
    print(package_models(args.cache_dir, args.output))


if __name__ == "__main__":
    main()
