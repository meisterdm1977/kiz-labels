"""58×40 stickers printed from the «Печать» page: a product barcode sticker and the packer's «Проверила упаковала».

Both are drawn from a layout: a list of elements placed in millimetres from the top-left corner, edited by the
admin in the sticker designer and kept in settings. DEFAULTS is the layout used until one is saved.
"""
import copy
import io
import json
import re
from pathlib import Path

from PIL import Image
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import code128
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from . import db

WIDTH_MM, HEIGHT_MM = 58, 40
WIDTH, HEIGHT = WIDTH_MM * mm, HEIGHT_MM * mm
SELLER = "ИП Малоземов Д.В."
SINGULAR = {
    "рубашки": "Рубашка", "платья": "Платье", "юбки": "Юбка", "костюмы": "Костюм", "корсеты": "Корсет",
    "пояса": "Пояс", "пиджаки": "Пиджак", "блузки": "Блузка", "жилеты": "Жилет", "брюки": "Брюки", "шорты": "Шорты",
}
# «138N/белый», «2409-2 бежевый», «150N- вишня»: a code with a digit, then the colour in Cyrillic.
_CODE_COLOUR = re.compile(r"^([^\s/]*\d[^\s/]*?)\s*-?\s*([А-Яа-яЁё].*)$")

# Font choices: name shown in the designer → regular and bold files (metric twins of the familiar fonts).
FONT_FILES = {
    "Calibri": ("crosextra/Carlito-Regular.ttf", "crosextra/Carlito-Bold.ttf"),
    "Arial": ("liberation/LiberationSans-Regular.ttf", "liberation/LiberationSans-Bold.ttf"),
    "Times New Roman": ("liberation/LiberationSerif-Regular.ttf", "liberation/LiberationSerif-Bold.ttf"),
    "Cambria": ("crosextra/Caladea-Regular.ttf", "crosextra/Caladea-Bold.ttf"),
    "Courier New": ("liberation/LiberationMono-Regular.ttf", "liberation/LiberationMono-Bold.ttf"),
    "DejaVu Sans": ("dejavu/DejaVuSans.ttf", "dejavu/DejaVuSans-Bold.ttf"),
}
DEFAULT_FONT = "Calibri"


def _register_fonts():
    fonts = {}
    for name, files in FONT_FILES.items():
        paths = [Path("/usr/share/fonts/truetype") / f for f in files]
        if all(p.exists() for p in paths):
            pdfmetrics.registerFont(TTFont(f"st-{name}", str(paths[0])))
            pdfmetrics.registerFont(TTFont(f"st-{name}-bold", str(paths[1])))
            fonts[name] = (f"st-{name}", f"st-{name}-bold")
    return fonts


FONTS = _register_fonts()
FONT_NAMES = [name for name in FONT_FILES if name in FONTS]

# What each sticker can show; the designer offers these to add back.
FIELDS = {
    "product": {"name": "Название", "article": "Артикул", "colour": "Цвет", "size": "Размер"},
    "packer": {"name": "Имя сотрудника", "date": "Дата"},
}
CODES = {"product": "barcode", "packer": "qr"}
KINDS = {"product": "Наклейка ШК", "packer": "Проверила упаковала"}


def _text(id, x, y, w, h, size, text="", field=None, bold=False, align="left", font=DEFAULT_FONT):
    return {"id": id, "type": "text", "x": x, "y": y, "w": w, "h": h, "text": text, "field": field,
            "font": font, "size": size, "bold": bold, "align": align}


DEFAULTS = {
    "product": [
        _text("name", 2, 1, 54, 5.5, 12, field="name", bold=True, align="center"),
        _text("article", 2, 7, 31, 5, 11, "Артикул: ", "article", bold=True),
        _text("colour", 2, 12, 31, 5, 11, "Цвет: ", "colour", bold=True),
        _text("size", 33, 7, 13, 10, 26, field="size", bold=True, align="right"),
        {"id": "eac", "type": "eac", "x": 47, "y": 9.5, "w": 9, "h": 4.2},
        _text("seller", 2, 17.5, 54, 3.5, 7.5, SELLER),
        {"id": "barcode", "type": "barcode", "x": 2, "y": 21.5, "w": 54, "h": 17, "digits": 9,
         "font": DEFAULT_FONT},
    ],
    "packer": [
        _text("t1", 2, 1.5, 54, 9, 24, "Проверила", align="center"),
        _text("t2", 2, 10.5, 54, 9, 24, "упаковала", align="center"),
        _text("name", 2, 23, 37, 7, 16, field="name", bold=True),
        _text("date", 2, 31, 37, 5, 10, field="date"),
        {"id": "qr", "type": "qr", "x": 41, "y": 23, "w": 15, "h": 15},
    ],
}
TYPES = {"text", "barcode", "qr", "eac", "line", "image"}


class LayoutError(Exception):
    pass


# ---------- data for the bound fields ----------

def product_name(category):
    return SINGULAR.get(category.strip().lower(), category.strip())


def split_article(text):
    """Article and colour from «Артикул/цвет»; when no code can be told apart, the whole text is the article."""
    text = text.strip()
    if "/" in text:
        article, colour = text.split("/", 1)
    else:
        m = _CODE_COLOUR.match(text)
        article, colour = (m.group(1), m.group(2)) if m else (text, "")
    return article.strip(), colour.replace("_", " ").strip().upper()


def short_name(full_name):
    """«Иванова Анна Сергеевна» → «Анна И»; a name already written as «Светлана М.» or a single word stays."""
    words = full_name.replace(".", " ").split()
    if len(words) < 2:
        return " ".join(words)
    if len(words[1]) <= 2:  # already «Имя Ф»
        return f"{words[0]} {words[1][0].upper()}"
    return f"{words[1]} {words[0][0].upper()}"


def product_data(row):
    article, colour = split_article(row["article"])
    return {"name": product_name(row["category"]), "article": article, "colour": colour,
            "size": row["size"] or "", "code": row["barcode"]}


def packer_data(full_name, code, day):
    return {"name": short_name(full_name), "date": day.strftime("%d.%m.%Y"), "code": code}


# ---------- layouts: stored, checked, defaulted ----------

def _key(kind):
    return f"sticker_layout_{kind}"


def load(conn, kind):
    raw = db.get_setting(conn, _key(kind))
    return json.loads(raw) if raw else copy.deepcopy(DEFAULTS[kind])


def save(conn, kind, layout):
    db.set_setting(conn, _key(kind), json.dumps(clean(kind, layout), ensure_ascii=False))


def reset(conn, kind):
    conn.execute("DELETE FROM settings WHERE key = %s", (_key(kind),))


def _num(value, low, high, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return round(min(max(value, low), high), 2)


def clean(kind, layout):
    """Only known element types and settings, with sizes kept on the sticker; the designer posts it as JSON."""
    if kind not in KINDS or not isinstance(layout, list):
        raise LayoutError("Неверный макет")
    out = []
    for i, el in enumerate(layout[:80]):
        if not isinstance(el, dict) or el.get("type") not in TYPES:
            continue
        item = {"id": str(el.get("id") or f"e{i}")[:20], "type": el["type"],
                "x": _num(el.get("x"), -5, WIDTH_MM, 0), "y": _num(el.get("y"), -5, HEIGHT_MM, 0),
                "w": _num(el.get("w"), 0.1, WIDTH_MM + 5, 10), "h": _num(el.get("h"), 0.1, HEIGHT_MM + 5, 5)}
        if el["type"] in ("text", "barcode"):
            item["font"] = el.get("font") if el.get("font") in FONT_FILES else DEFAULT_FONT
        if el["type"] == "text":
            field = el.get("field")
            item.update({
                "text": str(el.get("text") or "")[:200],
                "field": field if field in FIELDS[kind] else None,
                "size": _num(el.get("size"), 4, 72, 10), "bold": bool(el.get("bold")),
                "align": el.get("align") if el.get("align") in ("left", "center", "right") else "left",
            })
        elif el["type"] == "barcode":
            item["digits"] = _num(el.get("digits"), 0, 30, 9)
        elif el["type"] == "image":
            try:
                item["image"] = int(el.get("image"))
            except (TypeError, ValueError):
                continue
        out.append(item)
    return out


# ---------- pictures ----------

def store_image(conn, content):
    """A logo turned black and white (the printer has no grey) and kept in the database."""
    try:
        img = Image.open(io.BytesIO(content))
        img.load()
    except Exception:
        raise LayoutError("Не удалось открыть картинку. Нужен файл PNG или JPG.")
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGBA", img.size, "white")
        img = Image.alpha_composite(background, img)
    img = img.convert("L")
    img.thumbnail((1200, 1200))
    img = img.point(lambda v: 255 if v > 140 else 0).convert("1")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    row = conn.execute("INSERT INTO sticker_images (data) VALUES (%s) RETURNING id, %s AS w, %s AS h",
                       (buf.getvalue(), img.width, img.height)).fetchone()
    return row


def images_for(conn, layout):
    ids = [el["image"] for el in layout if el["type"] == "image"]
    if not ids:
        return {}
    return {r["id"]: bytes(r["data"]) for r in conn.execute(
        "SELECT id, data FROM sticker_images WHERE id = ANY(%s)", (ids,))}


# ---------- drawing ----------

def _fit(text, font, size, width, smallest=4):
    while size > smallest and pdfmetrics.stringWidth(text, font, size) > width:
        size -= 0.25
    return size


def _eac(c, x, y, h):
    """The EAC mark: three square letters drawn with strokes, `h` high, bottom-left at (x, y)."""
    w, t, gap = h * 0.62, h * 0.16, h * 0.22
    for i, letter in enumerate("EAC"):
        lx = x + i * (w + gap)
        c.rect(lx, y, t, h, stroke=0, fill=1)                      # left stem
        c.rect(lx, y + h - t, w, t, stroke=0, fill=1)              # top
        if letter == "E":
            c.rect(lx, y + (h - t) / 2, w * 0.85, t, stroke=0, fill=1)
            c.rect(lx, y, w, t, stroke=0, fill=1)
        elif letter == "A":
            c.rect(lx + w - t, y, t, h, stroke=0, fill=1)
            c.rect(lx, y + (h - t) / 2, w, t, stroke=0, fill=1)
        else:
            c.rect(lx, y, w, t, stroke=0, fill=1)


EAC_RATIO = 3 * 0.62 + 2 * 0.22  # width of the mark per unit of height


def _draw_text(c, text, el, x, y, w, h):
    regular, bold = FONTS.get(el["font"]) or FONTS[FONT_NAMES[0]]
    font = bold if el["bold"] else regular
    size = _fit(text, font, el["size"], w)
    ascent, descent = pdfmetrics.getAscentDescent(font, size)
    baseline = y + (h - (ascent - descent)) / 2 - descent
    c.setFont(font, size)
    if el["align"] == "center":
        c.drawCentredString(x + w / 2, baseline, text)
    elif el["align"] == "right":
        c.drawRightString(x + w, baseline, text)
    else:
        c.drawString(x, baseline, text)


def _draw_barcode(c, value, el, x, y, w, h):
    digits = el.get("digits") or 0
    digits_h = digits * 1.15 if digits else 0
    bar_h = max(h - digits_h, 2 * mm)
    probe = code128.Code128(value, barWidth=1, barHeight=bar_h, quiet=False)
    barcode = code128.Code128(value, barWidth=w / probe.width, barHeight=bar_h, quiet=False)
    barcode.drawOn(c, x + (w - barcode.width) / 2, y + digits_h)
    if digits:
        regular = (FONTS.get(el["font"]) or FONTS[FONT_NAMES[0]])[0]
        size = _fit(value, regular, digits, w)
        c.setFont(regular, size)
        c.drawCentredString(x + w / 2, y + digits_h - size * 0.9, value)


def _draw_qr(c, value, x, y, w, h):
    side = min(w, h)
    widget = QrCodeWidget(value, barLevel="M", barBorder=0)
    x1, y1, x2, y2 = widget.getBounds()
    drawing = Drawing(side, side, transform=[side / (x2 - x1), 0, 0, side / (y2 - y1), 0, 0])
    drawing.add(widget)
    renderPDF.draw(drawing, c, x + (w - side) / 2, y + (h - side) / 2)


def render(kind, layout, data, images=None):
    """The sticker as a one-page 58×40 PDF."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(WIDTH, HEIGHT))
    c.setTitle(f"{KINDS[kind]} 58×40")
    for el in layout:
        x, w, h = el["x"] * mm, el["w"] * mm, el["h"] * mm
        y = HEIGHT - el["y"] * mm - h  # stored from the top, drawn from the bottom
        kind_ = el["type"]
        if kind_ == "text":
            value = data.get(el["field"], "") if el.get("field") else ""
            if el.get("field") and not value:
                continue  # e.g. no colour: the whole line goes, prefix too
            text = el["text"] + value
            if text.strip():
                _draw_text(c, text, el, x, y, w, h)
        elif kind_ == "barcode" and data.get("code"):
            _draw_barcode(c, data["code"], el, x, y, w, h)
        elif kind_ == "qr" and data.get("code"):
            _draw_qr(c, data["code"], x, y, w, h)
        elif kind_ == "eac":
            mark_h = min(h, w / EAC_RATIO)
            _eac(c, x + (w - mark_h * EAC_RATIO) / 2, y + (h - mark_h) / 2, mark_h)
        elif kind_ == "line":
            c.rect(x, y, w, h, stroke=0, fill=1)
        elif kind_ == "image" and images and el["image"] in images:
            c.drawImage(ImageReader(io.BytesIO(images[el["image"]])), x, y, w, h,
                        preserveAspectRatio=True, anchor="c", mask="auto")
    c.showPage()
    c.save()
    return buf.getvalue()


def product_pdf(conn, row):
    layout = load(conn, "product")
    return render("product", layout, product_data(row), images_for(conn, layout))


def packer_pdf(conn, full_name, code, day):
    layout = load(conn, "packer")
    return render("packer", layout, packer_data(full_name, code, day), images_for(conn, layout))
