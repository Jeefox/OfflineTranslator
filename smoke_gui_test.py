"""Smoke-тест GUI: окно, диалог настроек, запись хоткея, выбор модели.

Запуск: .venv-linux/bin/python smoke_gui_test.py
Проверяет логику программно (синтетические события, без реальных нажатий):
  1. GUI создаётся, окно настроек открывается без ошибок;
  2. запись хоткея: Ctrl+Alt+T -> "ctrl+alt+t";
     Escape -> отмена (старое значение возвращается);
     одиночный F5 -> отклоняется валидатором (красная рамка, ст. значение);
  3. _pick_model_file: filedialog подменяется (реальный блокирует);
     результат подставляется в поле, parent=окно, grab_set/release по разу.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest import mock

import customtkinter as ctk
import tkinter as tk

from gui_ctk import TranslatorGUI
from offline_translate.translator import PROJECT_ROOT


def make_event(keysym: str) -> SimpleNamespace:
    """Фейковое событие: обработчики читают только event.keysym."""
    return SimpleNamespace(keysym=keysym)


def _walk(w):
    yield w
    for child in w.winfo_children():
        yield from _walk(child)


def run() -> int:
    failures: list[str] = []
    root = ctk.CTk()
    gui = TranslatorGUI(root)

    # --- 1. Диалог настроек открывается -------------------------------- #
    gui._open_settings()
    root.update_idletasks()
    win = gui.settings_window
    if win is None or not win.winfo_exists():
        print("SMOKE_FAIL: settings_window не открыт")
        root.destroy()
        return 1
    entries = [w for w in _walk(win) if isinstance(w, ctk.CTkEntry)]
    if len(entries) < 2:
        print("SMOKE_FAIL: не найдены CTkEntry в диалоге")
        root.destroy()
        return 1
    hk_entry = entries[0]      # первое поле — hotkey
    model_entry = entries[-1]  # последнее — model_path
    # Текущий хоткей из настроек (тест не зависит от значения в конфиге).
    orig_hk = hk_entry.get().strip() or "ctrl+alt+t"
    if not orig_hk:
        orig_hk = "ctrl+alt+t"
    if hk_entry.get().strip() != orig_hk:
        failures.append(f"дефолтный хоткей в поле: {hk_entry.get()!r}")

    # --- 2. Запись: Ctrl+Alt+T ----------------------------------------- #
    gui._start_hotkey_recording(hk_entry)
    if not gui._recording:
        failures.append("режим записи не активирован")
    for ks in ("Control_L", "Alt_L", "t"):
        gui._hk_on_key(make_event(ks))
    # Пока клавиши зажаты — поле не мелькает, остаётся подсказка.
    if hk_entry.get() != "Нажмите сочетание...":
        failures.append(f"пока зажат: {hk_entry.get()!r}, ожидал 'Нажмите сочетание...'")
    for ks in ("t", "Alt_L", "Control_L"):
        gui._hk_on_release(make_event(ks))
    if gui._recording:
        failures.append("запись не финализирована после отпускания")
    if hk_entry.get() != "ctrl+alt+t":
        failures.append(f"итог записи: {hk_entry.get()!r}, ожидал ctrl+alt+t")

    # --- 2b. Escape = отмена -------------------------------------------- #
    # Базовое значение — то, что в поле сейчас (тест 2 записал ctrl+alt+t).
    base_hk = hk_entry.get().strip()
    gui._start_hotkey_recording(hk_entry)
    gui._hk_on_key(make_event("Control_L"))
    gui._hk_on_key(make_event("x"))         # combo ctrl+x
    gui._hk_on_key(make_event("Escape"))
    if gui._recording:
        failures.append("Escape не отменил запись")
    if hk_entry.get() != base_hk:
        failures.append(f"после Escape: {hk_entry.get()!r}, ожидал {base_hk!r}")

    # --- 2c. Одиночный F5 -> валидатор отклоняет ------------------------ #
    # (settings.normalize_hotkey принимает только 1-символьные основные клавиши)
    gui._start_hotkey_recording(hk_entry)
    gui._hk_on_key(make_event("F5"))
    gui._hk_on_release(make_event("F5"))
    if hk_entry.get() != base_hk:
        failures.append(f"F5 (невалидный): {hk_entry.get()!r}, ожидал возврат к {base_hk!r}")
    if gui._hk_result is not None:
        failures.append(f"F5: _hk_result={gui._hk_result!r}, ожидал None")

    # --- 3. _pick_model_file (filedialog подменяем) -------------------- #
    calls = {"grab_set": 0, "grab_release": 0}
    real_set, real_release = win.grab_set, win.grab_release
    win.grab_set = lambda: calls.__setitem__("grab_set", calls["grab_set"] + 1)
    win.grab_release = lambda: calls.__setitem__("grab_release", calls["grab_release"] + 1)
    model_file = "translate-en_ru-1_9.argosmodel"
    with mock.patch("gui_ctk.filedialog.askopenfilename",
                    return_value=str(PROJECT_ROOT / model_file)) as m:
        gui._pick_model_file(model_entry)
        parent_ok = m.call_args.kwargs.get("parent") is win
    if model_entry.get() != str(PROJECT_ROOT / model_file):
        failures.append(f"модель в поле: {model_entry.get()!r}")
    if not parent_ok:
        failures.append("filedialog вызван без parent=окно настроек")
    if calls != {"grab_set": 1, "grab_release": 1}:
        failures.append(f"grab_set/release: {calls}")
    # Отмена выбора (пустая строка) — поле не меняется.
    before = model_entry.get()
    with mock.patch("gui_ctk.filedialog.askopenfilename", return_value=""):
        gui._pick_model_file(model_entry)
    if model_entry.get() != before:
        failures.append("поле изменилось при отмене выбора")
    win.grab_set, win.grab_release = real_set, real_release

    # --- Итог ----------------------------------------------------------- #
    try:
        root.destroy()
    except tk.TclError:
        pass
    if failures:
        print("SMOKE_FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("SMOKE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(run())
