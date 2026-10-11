"""Settings scroll callbacks have a bounded lifetime; run under Xvfb."""
import os
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from ui_widgets import ManagedScrollableFrame
import customtkinter.windows.widgets.ctk_scrollable_frame as scroll_module
from customtkinter.windows.widgets.appearance_mode.appearance_mode_tracker import AppearanceModeTracker
from customtkinter.windows.widgets.scaling.scaling_tracker import ScalingTracker

sequences = ("<MouseWheel>", "<KeyPress-Shift_L>", "<KeyPress-Shift_R>",
             "<KeyRelease-Shift_L>", "<KeyRelease-Shift_R>")

with tempfile.TemporaryDirectory() as config, \
        patch.dict(os.environ, {"OFFLINE_TRANSLATE_CONFIG": config}), \
        patch.object(main.TranslatorApp, "_start_model_load"), \
        patch.object(main.TranslatorApp, "_start_hotkey_agent"), \
        patch.object(main.SystemTray, "start", return_value=False):
    app = main.TranslatorApp()
    app._load_seq = 0
    errors = []
    app.report_callback_exception = lambda *error: errors.append(error)
    foreign = []
    try:
        app.update()
        for sequence in sequences:
            funcid = app.bind_all(sequence, lambda event: foreign.append(event.type), add="+")
            foreign.append((sequence, funcid))
        baseline = {sequence: app.bind_all(sequence) for sequence in sequences}
        for cycle in range(3):
            app._open_settings()
            dialog = app._settings_dialog
            dialog.geometry("500x480")
            app.update()
            frame = dialog._scroll_frame
            owned = list(frame._global_bindings)
            assert len(owned) == 5
            for sequence, funcid in owned:
                assert funcid in app.bind_all(sequence)
            canvas = frame._parent_canvas
            control = dialog.theme_note_label._label
            canvas.yview_moveto(0)
            control.event_generate("<Button-5>")
            assert canvas.yview()[0] > 0
            control.event_generate("<Button-4>")
            assert canvas.yview()[0] == 0
            # Each actual Tk target processes exactly one Linux wheel step,
            # irrespective of a surrounding CTk wrapper's forwarding bind().
            for target in (control, dialog.theme_note_label._canvas,
                           dialog.hotkey_entry._entry, frame):
                canvas.yview_moveto(0)
                with patch.object(dialog, "_scroll_by", wraps=dialog._scroll_by) as scroll:
                    target.event_generate("<Button-5>")
                assert scroll.call_args_list == [((3,), {})], type(target)
                assert canvas.yview()[0] > 0
            # A control's existing callback need not return break: leave the
            # wheel entirely to that control instead of appending our callback.
            dialog._unbind_scroll_events()
            own = tk.Entry(frame)
            own.grid(row=99, column=0)
            control_wheel = []
            own.bind("<Button-5>", lambda event: control_wheel.append(event.num))
            own_script = own.bind("<Button-5>")
            dialog._bind_scroll_events()
            app.update()
            canvas.yview_moveto(1)
            with patch.object(dialog, "_scroll_by") as scroll:
                own.event_generate("<Button-5>")
            assert control_wheel == [5]
            scroll.assert_not_called()
            assert own.bind("<Button-5>") == own_script
            linux_commands = [funcid for _widget, _sequence, funcid in dialog._scroll_bindings]
            # Exercise CTk's platform branches with synthetic MouseWheel.
            # This validates routing and delta conversion, not native OS input.
            for platform, delta in (("win32", -120), ("darwin", -1), ("linux", -1)):
                canvas.yview_moveto(0)
                with patch.object(scroll_module.sys, "platform", platform):
                    control.event_generate("<MouseWheel>", delta=delta)
                assert canvas.yview()[0] > 0, platform
            # Also exercise destruction through the parent, not just WM close.
            if cycle == 1:
                dialog.destroy()
                app._settings_dialog = None
            else:
                dialog._on_close()
            assert not frame._global_bindings
            frame.destroy()  # Repeated destruction must not delete foreign commands.
            for sequence in sequences:
                assert app.bind_all(sequence).strip() == baseline[sequence].strip(), sequence
            for _sequence, funcid in owned:
                assert not app.tk.call("info", "commands", funcid)
            for funcid in linux_commands:
                assert not app.tk.call("info", "commands", funcid)
            count = len(foreign)
            app.event_generate("<MouseWheel>", delta=-120)
            assert len(foreign) == count + 1
            app.update()
        failed_commands = []
        original_bind_all = ManagedScrollableFrame.bind_all
        def record_binding(frame, *args, **kwargs):
            funcid = original_bind_all(frame, *args, **kwargs)
            failed_commands.append(funcid)
            return funcid
        children_before = app.winfo_children()
        appearance_before = list(AppearanceModeTracker.callback_list)
        scaling_before = list(ScalingTracker.window_widgets_dict[app])
        for cycle in range(3):
            with patch.object(tk.Canvas, "create_window", side_effect=RuntimeError("construction failure")), \
                    patch.object(ManagedScrollableFrame, "bind_all", record_binding):
                try:
                    ManagedScrollableFrame(app)
                except RuntimeError as error:
                    assert str(error) == "construction failure"
                else:
                    raise AssertionError("Expected constructor failure")
            assert app.winfo_children() == children_before
            assert AppearanceModeTracker.callback_list == appearance_before
            assert ScalingTracker.window_widgets_dict[app] == scaling_before
        assert len(failed_commands) == 15
        for funcid in failed_commands:
            assert not app.tk.call("info", "commands", funcid)
        for sequence in sequences:
            assert app.bind_all(sequence).strip() == baseline[sequence].strip()
        # An earlier failure, before Tk Frame initialization, also preserves it.
        with patch.object(main.ctk.CTkFrame, "__init__", side_effect=RuntimeError("early failure")):
            try:
                ManagedScrollableFrame(app)
            except RuntimeError as error:
                assert str(error) == "early failure"
            else:
                raise AssertionError("Expected early failure")
        # Failure after Tk Frame initialization but before scaling registration
        # must also destroy the native subtree without masking the exception.
        original_frame_init = tk.Frame.__init__
        def fail_after_frame(widget, *args, **kwargs):
            original_frame_init(widget, *args, **kwargs)
            if isinstance(widget, ManagedScrollableFrame):
                raise RuntimeError("partial frame failure")
        with patch.object(tk.Frame, "__init__", fail_after_frame):
            try:
                ManagedScrollableFrame(app)
            except RuntimeError as error:
                assert str(error) == "partial frame failure"
            else:
                raise AssertionError("Expected partial frame failure")
        assert app.winfo_children() == children_before
        assert AppearanceModeTracker.callback_list == appearance_before
        assert ScalingTracker.window_widgets_dict[app] == scaling_before
        assert not errors, errors
        print("OK: settings global bindings cleaned; foreign handlers and wheel preserved")
    finally:
        if app._settings_dialog is not None:
            app._settings_dialog._on_close()
        app._quit_app()
