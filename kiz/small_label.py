"""The 58×40 label: drawn anew from the data of the 1С layout instead of copying its 50×104 page."""
import io
import json
import threading
from pathlib import Path

import openpyxl
from PIL import Image, ImageOps
from pylibdmtx.pylibdmtx import decode as dmtx_decode
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .labels import EAN_RE, TEXT_CODE_RE, _blocks

WIDTH_MM, HEIGHT_MM = 58, 40
DATAMATRIX_MM = 22
MARGIN_MM = 1.5
FILE = "small.json"
FIELD_NAMES = ("пол", "артикул", "размер", "цвет", "состав")


class SmallLabelError(Exception):
    pass


def _register_fonts():
    for name, files in {
        "Small": ["/usr/share/fonts/truetype/crosextra/Carlito-Regular.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
        "SmallBold": ["/usr/share/fonts/truetype/crosextra/Carlito-Bold.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    }.items():
        path = next((f for f in files if Path(f).exists()), None)
        if path:
            pdfmetrics.registerFont(TTFont(name, path))
    return ("Small", "SmallBold") if "Small" in pdfmetrics.getRegisteredFontNames() else ("Helvetica", "Helvetica-Bold")


FONT, BOLD = _register_fonts()


# ---------- reading the layout ----------

def datamatrix_modules(data):
    """The module grid of a DataMatrix picture, row by row."""
    img = Image.open(io.BytesIO(data)).convert("L")
    box = ImageOps.invert(img).point(lambda v: 255 if v > 127 else 0).getbbox()
    if not box:
        return None
    img = img.crop(box)  # a quiet zone, if the picture has one, is not part of the grid
    w, h = img.size
    # The top and right edges alternate black and white module by module: their runs count the grid.
    top = [img.getpixel((x, 1)) < 128 for x in range(w)]
    cols = 1 + sum(1 for a, b in zip(top, top[1:]) if a != b)
    right = [img.getpixel((w - 2, y)) < 128 for y in range(h)]
    rows = 1 + sum(1 for a, b in zip(right, right[1:]) if a != b)
    if cols < 10 or rows < 8:
        return None
    return ["".join("1" if img.getpixel((int((x + .5) * w / cols), int((y + .5) * h / rows))) < 128 else "0"
                    for x in range(cols)) for y in range(rows)]


def modules_decode(modules):
    """Reads a module grid back, to be sure the redrawn DataMatrix carries the same code."""
    scale = 6
    rows, cols = len(modules), len(modules[0])
    img = Image.new("L", (cols * scale, rows * scale), 255)
    for y, row in enumerate(modules):
        for x, bit in enumerate(row):
            if bit == "1":
                img.paste(0, (x * scale, y * scale, (x + 1) * scale, (y + 1) * scale))
    img = ImageOps.expand(img, border=4 * scale, fill=255)
    found = dmtx_decode(img, max_count=1, timeout=3000)
    return found[0].data.decode("latin-1") if found else None


def _block_fields(rows):
    """Texts of one label sorted into the parts the small layout places."""
    info = {"fields": [], "seller": [], "other": []}
    current, previous = None, None
    for row, cells in sorted(rows.items()):
        texts = [v.strip() for _, v in sorted(cells) if v.strip()]
        if previous is not None and row - previous > 1:
            current = None  # an empty row ends the manufacturer or seller block
        previous = row
        if not texts:
            continue
        line = " ".join(texts)
        key = texts[0].rstrip(":").strip().lower()
        if EAN_RE.match(line):
            info["ean"] = line
        elif TEXT_CODE_RE.search(line):
            info["code_text"] = [part.strip() for part in line.split("\n") if part.strip()]
        elif key.startswith("изготовитель"):
            current = "maker"
        elif key.startswith("продавец"):
            current = "seller"
            info["seller"].append(line)
        elif key in FIELD_NAMES and len(texts) > 1:
            info["fields"].append([texts[0].rstrip(":").strip() + ":", " ".join(texts[1:])])
            current = None
        elif line.lower().startswith("тр тс"):
            info["tr"] = line
        elif line.lower().startswith("дата производства"):
            info["date"] = [part.strip() for part in line.split("\n") if part.strip()]
        elif line.isdigit() and len(line) <= 6:
            info["number"] = line
        elif current == "seller":
            info["seller"].append(line)
        elif current == "maker":
            pass  # The manufacturer repeats the seller, so the small label leaves it out.
        elif "kind" not in info:
            info["kind"] = line
        else:
            info["other"].append(line)
    return info


def extract(xlsx_path, folder):
    """Writes small.json with every label's data and the badge pictures next to the upload."""
    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb.worksheets[0]
    images = {}
    for im in ws._images:
        images.setdefault(im.anchor._from.row + 1, []).append(im)
    labels, icons_saved = [], False
    for start, end in _blocks(ws):
        rows = {}
        for row in ws.iter_rows(min_row=start, max_row=end):
            for c in row:
                if isinstance(c.value, str) and c.value.strip():
                    rows.setdefault(c.row, []).append((c.column, c.value))
                elif isinstance(c.value, (int, float)) and c.value == int(c.value):
                    rows.setdefault(c.row, []).append((c.column, str(int(c.value))))
        block_images = [im for r in range(start, end + 1) for im in images.get(r, [])]
        if not rows and not block_images:
            continue
        info = _block_fields(rows)
        pictures = sorted(block_images, key=lambda im: -(im.width * im.height))
        squares = [im for im in pictures if 0.6 < im.width / im.height < 1.7]
        info["modules"] = datamatrix_modules(squares[0]._data()) if squares else None
        if not icons_saved and len(squares) >= 3:
            # Besides the DataMatrix: the ЕАС mark (larger) and the Честный знак mark (smaller).
            for name, im in (("icon_eac.png", squares[1]), ("icon_cz.png", squares[2])):
                Image.open(io.BytesIO(im._data())).save(Path(folder) / name)
            icons_saved = True
        labels.append(info)
    (Path(folder) / FILE).write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")
    return labels


_lock = threading.Lock()
_cache = {}


def load(folder):
    """The small-label data of an upload, made from its source file the first time it is needed."""
    folder = Path(folder)
    with _lock:
        if folder not in _cache:
            path = folder / FILE
            if not path.exists():
                if not (folder / "source.xlsx").exists():
                    raise SmallLabelError("исходный файл этой партии не сохранился")
                extract(folder / "source.xlsx", folder)
            _cache[folder] = json.loads(path.read_text(encoding="utf-8"))
            if len(_cache) > 50:
                _cache.pop(next(iter(_cache)))
        return _cache[folder]


# ---------- drawing ----------

def _draw_text_block(c, x, y, width, parts, sizes):
    """Draws (label, value, style) lines from y down; returns the y below them."""
    for label, value, style in parts:
        size = sizes[style]
        step = size * 0.37 * mm
        if label:
            c.setFont(BOLD, size)
            label_width = c.stringWidth(label + " ", BOLD, size)
            lines = simpleSplit(value, FONT, size, width - label_width) or [""]
            c.drawString(x, y, label)
            for i, line in enumerate(lines):
                c.setFont(FONT, size)
                c.drawString(x + label_width, y, line)
                y -= step
        else:
            font = BOLD if style == "kind" else FONT
            for line in simpleSplit(value, font, size, width) or [""]:
                c.setFont(font, size)
                c.drawString(x, y, line)
                y -= step
    return y


def _text_height(c, width, parts, sizes):
    total = 0
    for label, value, style in parts:
        size = sizes[style]
        if label:
            free = width - c.stringWidth(label + " ", BOLD, size)
            count = len(simpleSplit(value, FONT, size, free) or [""])
        else:
            count = len(simpleSplit(value, BOLD if style == "kind" else FONT, size, width) or [""])
        total += count * size * 0.37 * mm
    return total


def draw(c, info, folder):
    W, H, M = WIDTH_MM * mm, HEIGHT_MM * mm, MARGIN_MM * mm
    top = lambda y_mm: H - y_mm * mm  # noqa: E731

    modules = info.get("modules")
    if not modules:
        raise SmallLabelError("не удалось прочитать DataMatrix")
    rows, cols = len(modules), len(modules[0])
    cell = DATAMATRIX_MM * mm / max(rows, cols)
    x0, y0 = M, H - M - rows * cell
    c.setFillColorRGB(0, 0, 0)
    for y, row in enumerate(modules):
        for x, bit in enumerate(row):
            if bit == "1":
                c.rect(x0 + x * cell, y0 + (rows - 1 - y) * cell, cell, cell, stroke=0, fill=1)

    column = DATAMATRIX_MM * mm
    c.setFont(FONT, 5.4)
    for i, line in enumerate(info.get("code_text", [])[:2]):
        c.drawString(M, top(26.0 + i * 2.1), line)
    if info.get("number"):
        c.drawRightString(M + column, top(28.1), info["number"])
    eac, cz = Path(folder) / "icon_eac.png", Path(folder) / "icon_cz.png"
    if cz.exists():
        c.drawImage(ImageReader(str(cz)), M, top(34.0), width=4.4 * mm, height=3.7 * mm, mask="auto")
    if eac.exists():
        c.drawImage(ImageReader(str(eac)), M + 5.2 * mm, top(34.4), width=4.6 * mm, height=4.1 * mm, mask="auto")
    c.setFont(FONT, 4.8)
    if info.get("tr"):
        c.drawString(M + 10.6 * mm, top(32.9), info["tr"])
    for i, line in enumerate(info.get("date", [])[:2]):
        c.drawString(M, top(36.6 + i * 1.9), line)

    # Right column: texts from the top, the EAN barcode at the bottom.
    x = M + column + 1.8 * mm
    width = W - x - M
    barcode_top = H
    if info.get("ean"):
        try:
            bc = createBarcodeDrawing("EAN13", value=info["ean"][:12], barHeight=6 * mm,
                                      humanReadable=True, fontName=FONT, fontSize=5.5)
            scale = width / bc.width
            bc.scale(scale, 1)
            renderPDF.draw(bc, c, x, M - 0.3 * mm)
            barcode_top = M + bc.height + 0.8 * mm
        except Exception:
            c.setFont(FONT, 6)
            c.drawString(x, M + 1 * mm, info["ean"])
            barcode_top = M + 4 * mm

    parts = []
    if info.get("kind"):
        parts.append(("", info["kind"], "kind"))
    parts += [(label, value, "field") for label, value in info.get("fields", [])]
    parts += [("", line, "small") for line in info.get("other", [])]
    seller = " ".join(info.get("seller", []))
    if seller:
        parts.append(("", seller, "small"))
    start = top(MARGIN_MM) - 2.2 * mm
    room = start - barcode_top
    sizes = {"kind": 6.8, "field": 5.8, "small": 4.8}
    # Long texts are never cut: everything shrinks a little until it fits above the barcode.
    while _text_height(c, width, parts, sizes) > room and sizes["small"] > 3.6:
        sizes = {k: v * 0.96 for k, v in sizes.items()}
    _draw_text_block(c, x, start, width, parts, sizes)


def build_pdf(items, out):
    """items: (upload_folder, page_no) in print order."""
    c = canvas.Canvas(out, pagesize=(WIDTH_MM * mm, HEIGHT_MM * mm))
    for folder, page_no in items:
        labels = load(folder)
        if page_no >= len(labels):
            raise SmallLabelError("этикетка не найдена в исходном файле")
        draw(c, labels[page_no], folder)
        c.showPage()
    c.save()
