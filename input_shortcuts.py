"""Physical keyboard shortcuts independent of the active input layout."""
import platform

_PHYSICAL_KEYCODES = {
    "Linux": {
        "a": 38, "b": 56, "c": 54, "d": 40, "e": 26, "f": 41,
        "g": 42, "h": 43, "i": 31, "j": 44, "k": 45, "l": 46,
        "m": 58, "n": 57, "o": 32, "p": 33, "q": 24, "r": 27,
        "s": 39, "t": 28, "u": 30, "v": 55, "w": 25, "x": 53,
        "y": 29, "z": 52,
        "comma": 59, "return": 36, "escape": 9,
    },
    "Windows": {
        "a": 65, "b": 66, "c": 67, "d": 68, "e": 69, "f": 70,
        "g": 71, "h": 72, "i": 73, "j": 74, "k": 75, "l": 76,
        "m": 77, "n": 78, "o": 79, "p": 80, "q": 81, "r": 82,
        "s": 83, "t": 84, "u": 85, "v": 86, "w": 87, "x": 88,
        "y": 89, "z": 90,
        "comma": 188, "return": 13, "escape": 27,
    },
    "Darwin": {
        "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3,
        "g": 5, "h": 4, "i": 34, "j": 38, "k": 40, "l": 37,
        "m": 46, "n": 45, "o": 31, "p": 35, "q": 12, "r": 15,
        "s": 1, "t": 17, "u": 32, "v": 9, "w": 13, "x": 7,
        "y": 16, "z": 6,
        "comma": 43, "return": 36, "escape": 53,
    },
}
_CONTROL_MASK = 0x0004


def _physical_key_name(event):
    """Имя системной клавиши по физическому Tk keycode."""
    codes = _PHYSICAL_KEYCODES.get(platform.system(), {})
    for name, code in codes.items():
        if event.keycode == code:
            return name
    return None

