#!/usr/bin/env python3
"""Сборка бинарников через PyInstaller."""
import io
import os
import subprocess
import sys
import platform

# Windows-консоль может использовать cp1252 — принудительно ставим UTF-8,
# иначе print() с кириллицей/emoji упадёт с UnicodeEncodeError.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

def check_deps():
    try:
        import PyInstaller
        print(f"✅ PyInstaller {PyInstaller.__version__}")
    except ImportError:
        print("❌ Установи: pip install pyinstaller")
        sys.exit(1)

    if not os.path.exists('glossary.txt'):
        print("⚠️  glossary.txt не найден")

def main():
    os_name = platform.system()
    print(f"\n🔨 Сборка для {os_name}...\n")
    check_deps()

    cmd = [sys.executable, '-m', 'PyInstaller', 'build.spec', '--clean', '--noconfirm']
    result = subprocess.run(cmd)

    if result.returncode == 0:
        exe = 'dist/OfflineTranslate.exe' if os_name == 'Windows' else 'dist/OfflineTranslate'
        if os.path.exists(exe):
            size_mb = os.path.getsize(exe) / (1024 * 1024)
            print(f"\n✅ Готово: {exe} ({size_mb:.1f} МБ)")
            print(f" Не забудь положить translate-en_ru-1_9.argosmodel рядом с бинарником!")
        else:
            print(f"\n⚠️  Бинарник не найден: {exe}")
    else:
        print(f"\n❌ Ошибка сборки")
        sys.exit(1)

if __name__ == '__main__':
    main()
