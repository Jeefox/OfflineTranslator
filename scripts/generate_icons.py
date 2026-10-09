"""Воспроизводимая генерация брендовых PNG/ICO из app_icons.py."""
from pathlib import Path

from app_icons import application_icon


def main():
    folder = Path(__file__).resolve().parents[1] / "assets"
    folder.mkdir(exist_ok=True)
    image = application_icon(256)
    image.save(folder / "offline_translator.png")
    image.save(folder / "offline_translator.ico", sizes=[(16, 16), (24, 24),
               (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    main()
