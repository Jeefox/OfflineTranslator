"""Shared palette, tooltips and the CustomTkinter text adapter."""
import tkinter as tk

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
            self.window.wm_overrideredirect(True)
            self.window.wm_geometry("+%d+%d" % (x, y))
            text = self.text() if callable(self.text) else self.text
            tk.Label(self.window, text=text, justify="left", wraplength=360,
                     bg="#11111b", fg="#f5f5f5", padx=8, pady=5).pack()
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
