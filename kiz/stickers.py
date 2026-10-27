"""58×40 stickers printed from the «Печать» page: a product barcode sticker and the packer's «Проверила упаковала»."""
import io
import re

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import code128
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from .small_label import BOLD, FONT

WIDTH, HEIGHT = 58 * mm, 40 * mm
SELLER = "ИП Малоземов Д.В."
SINGULAR = {
    "рубашки": "Рубашка", "платья": "Платье", "юбки": "Юбка", "костюмы": "Костюм", "корсеты": "Корсет",
    "пояса": "Пояс", "пиджаки": "Пиджак", "блузки": "Блузка", "жилеты": "Жилет", "брюки": "Брюки", "шорты": "Шорты",
}
# «138N/белый», «2409-2 бежевый», «150N- вишня»: a code with a digit, then the colour in Cyrillic.
_CODE_COLOUR = re.compile(r"^([^\s/]*\d[^\s/]*?)\s*-?\s*([А-Яа-яЁё].*)$")


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


def _fit(c, text, font, size, width, smallest=6):
    while size > smallest and c.stringWidth(text, font, size) > width:
        size -= 0.5
    return size


def _eac(c, x, y, h):
    """The EAC mark: three square letters drawn with strokes, `h` high, bottom-left at (x, y)."""
    w, t, gap = h * 0.62, h * 0.16, h * 0.22
    c.setLineWidth(0)
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
    return 3 * w + 2 * gap


def _new_canvas(buf):
    c = canvas.Canvas(buf, pagesize=(WIDTH, HEIGHT))
    c.setTitle("Наклейка 58×40")
    return c


def product_pdf(row):
    """One barcode sticker from a nomenclature row (category, article, size, barcode)."""
    buf = io.BytesIO()
    c = _new_canvas(buf)
    margin = 2 * mm
    article, colour = split_article(row["article"])

    name = product_name(row["category"])
    if name:
        size = _fit(c, name, BOLD, 12, WIDTH - 2 * margin)
        c.setFont(BOLD, size)
        c.drawCentredString(WIDTH / 2, HEIGHT - margin - 3.6 * mm, name)

    eac_h = 4.2 * mm
    eac_w = _eac(c, WIDTH - margin - 3 * 0.62 * eac_h - 2 * 0.22 * eac_h, HEIGHT - 15 * mm, eac_h)
    size_text = row["size"] or ""
    size_right = WIDTH - margin - eac_w - 1.5 * mm
    size_pt = _fit(c, size_text, BOLD, 26, 15 * mm, 12) if size_text else 0
    size_w = c.stringWidth(size_text, BOLD, size_pt) if size_text else 0
    if size_text:
        c.setFont(BOLD, size_pt)
        c.drawRightString(size_right, HEIGHT - 16 * mm, size_text)

    text_w = size_right - size_w - 1.5 * mm - margin
    lines = [f"Артикул: {article}"] + ([f"Цвет: {colour}"] if colour else [])
    top = HEIGHT - 10 * mm if len(lines) == 2 else HEIGHT - 12.5 * mm
    for i, line in enumerate(lines):
        c.setFont(BOLD, _fit(c, line, BOLD, 11, text_w))
        c.drawString(margin, top - i * 5 * mm, line)

    c.setFont(FONT, 7.5)
    c.drawString(margin, HEIGHT - 21 * mm, SELLER)

    code = row["barcode"]
    bar_w = 0.33 * mm
    barcode = code128.Code128(code, barHeight=10 * mm, barWidth=bar_w, quiet=False)
    while barcode.width > WIDTH - 2 * margin and bar_w > 0.2 * mm:
        bar_w -= 0.01 * mm
        barcode = code128.Code128(code, barHeight=10 * mm, barWidth=bar_w, quiet=False)
    barcode.drawOn(c, (WIDTH - barcode.width) / 2, 5.5 * mm)
    c.setFont(FONT, 9)
    c.drawCentredString(WIDTH / 2, 2 * mm, code)
    c.showPage()
    c.save()
    return buf.getvalue()


def short_name(full_name):
    """«Иванова Анна Сергеевна» → «Анна И»; a name already written as «Светлана М.» or a single word stays."""
    words = full_name.replace(".", " ").split()
    if len(words) < 2:
        return " ".join(words)
    if len(words[1]) <= 2:  # already «Имя Ф»
        return f"{words[0]} {words[1][0].upper()}"
    return f"{words[1]} {words[0][0].upper()}"


def packer_pdf(full_name, code, day):
    """«Проверила упаковала», short name, the date and a QR code with the personal code."""
    buf = io.BytesIO()
    c = _new_canvas(buf)
    margin = 2 * mm
    for i, word in enumerate(("Проверила", "упаковала")):
        size = _fit(c, word, FONT, 24, WIDTH - 2 * margin)
        c.setFont(FONT, size)
        c.drawCentredString(WIDTH / 2, HEIGHT - 9 * mm - i * 9 * mm, word)

    qr_size = 15 * mm
    widget = QrCodeWidget(code, barLevel="M")
    x1, y1, x2, y2 = widget.getBounds()
    drawing = Drawing(qr_size, qr_size, transform=[qr_size / (x2 - x1), 0, 0, qr_size / (y2 - y1), 0, 0])
    drawing.add(widget)
    renderPDF.draw(drawing, c, WIDTH - margin - qr_size, 1 * mm)

    name = short_name(full_name)
    text_w = WIDTH - 2 * margin - qr_size - 1 * mm
    size = _fit(c, name, BOLD, 16, text_w)
    c.setFont(BOLD, size)
    c.drawString(margin, 8.5 * mm, name)
    c.setFont(FONT, 10)
    c.drawString(margin, 3 * mm, day.strftime("%d.%m.%Y"))
    c.showPage()
    c.save()
    return buf.getvalue()
