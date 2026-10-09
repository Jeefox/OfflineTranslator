"""Shared palette, tooltips and the CustomTkinter text adapter."""
import tkinter as tk
import customtkinter as ctk


class ManagedScrollableFrame(ctk.CTkScrollableFrame):
    """Keep CTk's wheel behaviour, but own its interpreter-wide callbacks.

    CTk 5.2.2 does not unregister the bind_all callbacks in destroy().
    Tkinter's selective _unbind removes only the supplied function ID;
    unbind_all would also remove callbacks belonging to other components.
    """

    def __init__(self, *args, **kwargs):
        self._global_bindings = []
        try:
            super().__init__(*args, **kwargs)
        except Exception:
            # Construction can fail after CTk has registered root callbacks.
            self._release_global_bindings()
            raise

    def bind_all(self, sequence=None, func=None, add=None):
        funcid = super().bind_all(sequence, func, add)
        if funcid is not None and callable(func):
            self._global_bindings.append((sequence, funcid))
        return funcid

    def _release_global_bindings(self):
        # bind_all registers the Tcl commands on the root, not on this frame.
        # Remove both their script entries and commands before widget teardown.
        bindings, self._global_bindings = self._global_bindings, []
        if not bindings:
            return
        root = self._root()
        for sequence, funcid in bindings:
            root._unbind(("bind", "all", sequence), funcid)

    def destroy(self):
        self._release_global_bindings()
        super().destroy()

BG_COLOR = "#1e1e2e"        # фон окна
FIELD_BG = "#2a2a3c"        # текстовые поля
PANEL_BG = "#313244"       # панели, вторичные кнопки
PANEL_HOVER = "#45475a"    # hover вторичных элементов
ACCENT = "#89b4fa"         # основное действие, подпись «Перевод»
ACCENT_HOVER = "#74a8fc"
TEXT_COLOR = "#cdd6f4"      # основной текст
MUTED_COLOR = "#a6adc8"     # приглушённый текст (подзаголовок, статус)
SUCCESS_COLOR = "#a6e3a1"   # статус: готово / скопировано
ERROR_COLOR = "#f38ba8"     # статус: ошибки
PENDING_COLOR = "#fab387"   # статус: инициализация / идёт перевод

# Стандартный текст «готового» статуса.
READY_TEXT = "Готов к переводу! (локальная модель)"


class _Tooltip:
    """Dependency-free tooltip for controls whose meaning is not obvious."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.window = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<FocusIn>", self._show, add="+")
        widget.bind("<FocusOut>", self._hide, add="+")

    def _show(self, _event=None):
        if self.window is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
            self.window = tk.Toplevel(self.widget)
            # A newly-created Toplevel may be mapped before its children and
            # colours are configured.  Prepare it off-screen from the window
            # manager's point of view, then reveal it in one completed state.
            self.window.withdraw()
            self.window.configure(bg="#11111b")
            self.window.wm_overrideredirect(True)
            text = self.text() if callable(self.text) else self.text
            tk.Label(self.window, text=text, justify="left", wraplength=360,
                     bg="#11111b", fg="#f5f5f5", padx=8, pady=5).pack()
            self.window.update_idletasks()
            self.window.wm_geometry("+%d+%d" % (x, y))
            self.window.deiconify()
        except tk.TclError:
            self.window = None

    def _hide(self, _event=None):
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None

# Цветовые темы (Catppuccin). "dark" — текущая палитра по умолчанию (Mocha),
# "light" — та же палитра в светлой гамме (Latte). Тема выбирается в «Настройках»
# и применяется без перезапуска.
# "hl" — мягкая подсветка текущего предложения в полях (тот же приём, что в
# старой версии gui_ctk.py, но цвет — из текущей палитры; без кислотных тонов).
PALETTES = {
    "dark": {
        "bg": BG_COLOR, "field": FIELD_BG, "panel": PANEL_BG,
        "panel_hover": PANEL_HOVER, "scrollbar": PANEL_HOVER,
        "scrollbar_hover": "#585b70",
        "accent": ACCENT, "accent_hover": ACCENT_HOVER, "accent_text": BG_COLOR,
        "text": TEXT_COLOR, "muted": MUTED_COLOR,
        "success": SUCCESS_COLOR, "error": ERROR_COLOR, "pending": PENDING_COLOR,
        "hl": "#3b4768",
    },
    "light": {
        # Контрастная светлая палитра: тёмный текст на светлых полях и
        # различимые состояния кнопок, статуса и подсветки.
        "bg": "#f5f7fb", "field": "#ffffff", "panel": "#e3e8f0",
        "panel_hover": "#d4dce8", "scrollbar": "#b6c2d2",
        "scrollbar_hover": "#9eacbf",
        "accent": "#1d4ed8", "accent_hover": "#1e40af", "accent_text": "#ffffff",
        "text": "#1f2937", "muted": "#4b5563",
        "success": "#166534", "error": "#b91c1c", "pending": "#b45309",
        "hl": "#dbeafe",
    },
}




def tk_text(widget):
    """The one compatibility boundary for CTkTextbox's underlying Tk Text."""
    text = getattr(widget, "_textbox", None)
    if text is None:
        raise RuntimeError("Unsupported CustomTkinter textbox implementation")
    return text
