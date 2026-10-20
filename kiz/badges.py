"""Employee badge with the personal code as a Code 128 barcode, sized like a label."""
import io
from pathlib import Path

from reportlab.graphics.barcode import code128
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from . import config

_FONT = "Helvetica"
for candidate in (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
):
    if Path(candidate).exists():
        pdfmetrics.registerFont(TTFont("DejaVu", candidate))
        _FONT = "DejaVu"
        break


def badge_pdf(name, code):
    width, height = config.LABEL_WIDTH_MM * mm, config.LABEL_HEIGHT_MM * mm
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    c.setFont(_FONT, 11)
    c.drawCentredString(width / 2, height - 15 * mm, name[:28])
    c.setFont(_FONT, 8)
    c.drawCentredString(width / 2, height - 21 * mm, "Личный код сотрудника")
    barcode = code128.Code128(code, barHeight=22 * mm, barWidth=0.42 * mm, quiet=False)
    barcode.drawOn(c, (width - barcode.width) / 2, height / 2 - 11 * mm)
    c.setFont(_FONT, 12)
    c.drawCentredString(width / 2, height / 2 - 18 * mm, code)
    c.showPage()
    c.save()
    return buf.getvalue()
