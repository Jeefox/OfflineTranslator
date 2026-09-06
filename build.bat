@echo off
echo ========================================
echo Сборка OfflineTranslator в EXE файл
echo ========================================
echo.

echo Устанавливаем PyInstaller...
pip install pyinstaller
echo.

echo Очищаем старые сборки...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist *.spec del /q *.spec
echo.

echo Начинаем сборку...
echo (Это займет 2-5 минут)
pyinstaller --onefile ^
    --windowed ^
    --name "OfflineTranslator" ^
    --add-data "dictionary.json;." ^
    --add-data "cache;cache" ^
    --hidden-import torch ^
    --hidden-import transformers ^
    --hidden-import customtkinter ^
    main.py

echo.
echo ========================================
if exist dist\OfflineTranslator.exe (
    echo ✓ СБОРКА УСПЕШНА!
    echo.
    echo EXE файл находится в папке: %CD%\dist\
    echo Размер файла: 
    dir dist\OfflineTranslator.exe | find "OfflineTranslator.exe"
    echo.
    echo Можешь скопировать OfflineTranslator.exe куда угодно!
) else (
    echo ✗ ОШИБКА СБОРКИ
    echo Проверь консоль выше
)
echo ========================================
pause