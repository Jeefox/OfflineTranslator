"""Exercise the real GUI and packaged imports without models or OS services."""
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
import time
import re


def _translation_checks(app, model_root, result):
    """Opt-in real inference through the production GUI workers; no fake models."""
    from local_models import configure_paths
    root = Path(model_root).resolve()
    configure_paths(str(root / "en-ru"), str(root / "ru-en"))
    app.settings.set("autotranslate", False)
    app.settings.set("filter_cyrillic", False)
    app.settings.set("notifications_enabled", False)
    result["translations"] = []

    def wait_until(done, timeout=180):
        deadline = time.monotonic() + timeout
        def poll():
            if done() or app._status[0] == "error" or time.monotonic() >= deadline:
                app.quit()
            else:
                app.after(25, poll)
        app.after(0, poll)
        app.mainloop()
        if app._status[0] == "error" or not done():
            raise RuntimeError("Inference did not complete: " + repr(app._status))

    cases = (
        ("en-ru", "The small observatory opened its doors on Tuesday. The scientists studied distant stars.", r"[А-Яа-я]"),
        ("ru-en", "Небольшая обсерватория открыла свои двери во вторник. Учёные изучали далёкие звёзды.", r"[A-Za-z]"),
    )
    for direction, text, alphabet in cases:
        started = time.monotonic()
        app._set_direction(direction)
        if app.translator is None:
            app._start_model_load()
        wait_until(lambda: not app._model_loading and app.translator is not None)
        app.input_text.delete("1.0", "end")
        app.input_text.insert("1.0", text)
        app.start_translation()
        wait_until(lambda: not app._translation_busy)
        output = app.output_text.get("1.0", "end-1c")
        if output == text or not re.search(alphabet, output):
            raise RuntimeError("Unexpected real-model translation: " + repr(output))
        result["translations"].append({"direction": direction, "input": text,
                                       "output": output, "model": app._translator_model_id,
                                       "seconds": round(time.monotonic() - started, 2)})
    result["checks"].append("real_marian_gui_translation_both_directions")


def run_smoke(app_class, report_path, *, model_root=None):
    report = Path(report_path).resolve()
    result = {"ok": False, "frozen": bool(getattr(sys, "frozen", False)), "checks": []}
    app = None
    variables = ("OFFLINE_TRANSLATE_CONFIG", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                 "LOCALAPPDATA", "HOME", "USERPROFILE", "OFFLINE_TRANSLATOR_OFFLINE", "HF_HUB_OFFLINE")
    previous = {name: os.environ.get(name) for name in variables}
    try:
        with tempfile.TemporaryDirectory(prefix="offline-gui-smoke-") as directory:
            os.environ.update({"OFFLINE_TRANSLATE_CONFIG": directory + "/config",
                               "XDG_DATA_HOME": directory + "/data",
                               "XDG_CACHE_HOME": directory + "/cache",
                               "LOCALAPPDATA": directory + "/local",
                               "HOME": directory, "USERPROFILE": directory,
                               "OFFLINE_TRANSLATOR_OFFLINE": "1", "HF_HUB_OFFLINE": "1"})
            try:
                # Verify the heavy runtime imports are present in the bundle.
                from backends.marian import MarianBackend
                assert callable(MarianBackend)
                result["checks"].append("marian_runtime_import")
                errors = []
                class SmokeApp(app_class):
                    def report_callback_exception(self, _kind, error, _traceback):
                        errors.append(str(error))
                app = SmokeApp(start_services=False)
                app.update()
                if not app.winfo_ismapped():
                    raise RuntimeError("Main window did not become visible")
                app.input_text.insert("1.0", "GUI smoke: English → Русский")
                if app.input_text.get("1.0", "end-1c") != "GUI smoke: English → Русский":
                    raise RuntimeError("Textbox roundtrip failed")
                result["checks"].append("main_window_and_textbox")
                sequences = ("<MouseWheel>", "<KeyPress-Shift_L>", "<KeyPress-Shift_R>",
                             "<KeyRelease-Shift_L>", "<KeyRelease-Shift_R>")
                baseline = [app.tk.call("bind", "all", event) for event in sequences]
                for _ in range(2):
                    app._open_settings()
                    app.update()
                    dialog = app._settings_dialog
                    if not dialog.winfo_ismapped() or not dialog.save_btn.winfo_ismapped():
                        raise RuntimeError("Settings window did not become visible")
                    dialog._on_close()
                    app.update()
                    if [app.tk.call("bind", "all", event) for event in sequences] != baseline:
                        raise RuntimeError("Settings leaked global bindings")
                result["checks"].append("settings_reopen_and_bindings")
                from ui_widgets import _Tooltip
                tooltip = _Tooltip(app.settings_btn, "GUI smoke tooltip")
                tooltip._show()
                app.update()
                if tooltip.window is None or not tooltip.window.winfo_ismapped():
                    raise RuntimeError("Tooltip did not become visible")
                tooltip._hide()
                result["checks"].append("tooltip")
                if model_root is not None:
                    _translation_checks(app, model_root, result)
                if errors:
                    raise RuntimeError("Tk callback errors: " + repr(errors))
            finally:
                if app is not None:
                    app._quit_app()
                    app = None
        result["ok"] = True
    except Exception:
        result["error"] = traceback.format_exc()
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1
