"""Единая геометрия значков: без зависимости от шрифта и emoji ОС."""
import math
from pathlib import Path
import sys

from PIL import Image, ImageDraw


def asset_path(name):
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / name


def application_icon(size=256):
    image = Image.new("RGBA", (256, 256))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 248, 248), radius=54, fill="#89b4fa")
    draw.polygon(((52, 67), (162, 67), (162, 44), (212, 94),
                  (162, 144), (162, 121), (52, 121)), fill="#1e1e2e")
    draw.polygon(((204, 135), (94, 135), (94, 112), (44, 162),
                  (94, 212), (94, 189), (204, 189)), fill="#ffffff")
    return image.resize((size, size), Image.Resampling.LANCZOS)


def action_icon(name, color, size=24):
    scale = 4
    image = Image.new("RGBA", (24 * scale, 24 * scale))
    draw = ImageDraw.Draw(image)
    def points(coords):
        return [(round(x * scale), round(y * scale)) for x, y in coords]
    def line(coords):
        draw.line(points(coords), fill=color, width=2 * scale, joint="curve")
    def rect(box, fill=None):
        draw.rounded_rectangle(tuple(round(v * scale) for v in box), radius=2 * scale,
                               fill=fill, outline=color, width=2 * scale)
    if name == "settings":
        teeth = []
        for tooth in range(8):
            for angle, radius in ((0, 7), (.12, 10), (.55, 10), (.67, 7)):
                theta = (tooth + angle) * math.tau / 8
                teeth.append((12 + radius * math.cos(theta), 12 + radius * math.sin(theta)))
        draw.polygon(points(teeth), fill=color)
        draw.ellipse((8 * scale, 8 * scale, 16 * scale, 16 * scale), fill=(0, 0, 0, 0))
    elif name == "copy":
        rect((3, 3, 15, 17))
        rect((8, 7, 21, 22), fill=(0, 0, 0, 0))
    elif name == "clear":
        line(((4, 6), (20, 6)))
        line(((8, 6), (8, 3), (16, 3), (16, 6)))
        line(((6, 6), (7, 21), (17, 21), (18, 6)))
        line(((10, 10), (10, 17)))
        line(((14, 10), (14, 17)))
    elif name == "swap":
        line(((3, 7), (21, 7), (17, 3)))
        line(((21, 7), (17, 11)))
        line(((21, 17), (3, 17), (7, 13)))
        line(((3, 17), (7, 21)))
    else:
        raise ValueError("Unknown action icon: " + name)
    return image.resize((size, size), Image.Resampling.LANCZOS)
