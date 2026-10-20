from pypdf import PdfReader

from kiz.labels import code_prefix, read_workbook, render_pdf
from make_layout import make_layout


def test_reads_codes_and_fields(tmp_path):
    path = tmp_path / "layout.xlsx"
    codes = make_layout(path, 3)
    labels = read_workbook(path)
    assert [l.code for l in labels] == codes
    assert all(not l.errors for l in labels)
    first = labels[0]
    assert (first.article, first.size, first.ean, first.color) == (
        "Шорты 02 коричневый", "40", "2047362903387", "КОРИЧНЕВЫЙ")
    assert first.code_prefix == code_prefix(codes[0]) and len(first.code_prefix) == 31


def test_unreadable_datamatrix_is_reported(tmp_path):
    path = tmp_path / "layout.xlsx"
    make_layout(path, 3, broken={1})
    labels = read_workbook(path)
    assert labels[1].errors == ["не удалось прочитать DataMatrix"]
    assert not labels[0].errors and not labels[2].errors


def test_renders_one_label_sized_page_per_code(tmp_path):
    path = tmp_path / "layout.xlsx"
    make_layout(path, 4)
    out = tmp_path / "labels.pdf"
    render_pdf(path, out, 4)
    pages = PdfReader(out).pages
    assert len(pages) == 4
    box = pages[0].mediabox
    assert round(float(box.width) / 72 * 25.4) == 50
    assert round(float(box.height) / 72 * 25.4) == 104
