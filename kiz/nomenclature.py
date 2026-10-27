"""The product list from an Excel file: Категория | Артикул/цвет | Размер | Основной баркод."""
import io
import json
import re
from datetime import datetime, timezone

import openpyxl

from . import db

COLUMNS = (
    ("category", ("категория", "предмет")),
    ("article", ("артикул",)),
    ("size", ("размер",)),
    ("barcode", ("баркод", "штрихкод", "штрих-код")),
)
LOADED_KEY = "nomenclature_loaded"


class NomenclatureError(Exception):
    pass


def _text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def read_file(content):
    """Rows as dicts from the first sheet; the header row may be anywhere among the first ten rows."""
    try:
        ws = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True).worksheets[0]
        rows = [[_text(v) for v in r] for r in ws.iter_rows(values_only=True)]
    except Exception:
        raise NomenclatureError("Не удалось прочитать файл. Нужен файл Excel (.xlsx).")
    for start, header in enumerate(rows[:10]):
        lower = [h.lower() for h in header]
        found = {}
        for key, names in COLUMNS:
            col = next((i for i, h in enumerate(lower) if any(n in h for n in names)), None)
            if col is not None:
                found[key] = col
        if "article" in found and "barcode" in found:
            break
    else:
        raise NomenclatureError("В файле нет столбцов «Артикул/цвет» и «Основной баркод».")
    items = []
    for r in rows[start + 1:]:
        item = {key: (r[col] if col < len(r) else "") for key, col in found.items()}
        if not item.get("article"):
            continue
        item["barcode"] = re.sub(r"\s", "", item.get("barcode", ""))
        items.append({key: item.get(key, "") for key, _ in COLUMNS})
    if not items:
        raise NomenclatureError("В файле нет ни одной строки с артикулом.")
    return items


def replace(items, filename, admin_name):
    """Swap the rows from the previous file for the new ones; manual rows stay unless the file has their barcode."""
    with db.tx() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(4243)")
        conn.execute("DELETE FROM nomenclature WHERE NOT manual")
        barcodes = [it["barcode"] for it in items if it["barcode"]]
        conn.execute("DELETE FROM nomenclature WHERE manual AND barcode = ANY(%s)", (barcodes,))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO nomenclature (position, category, article, size, barcode) VALUES (%s, %s, %s, %s, %s)",
                [(i, it["category"], it["article"], it["size"], it["barcode"]) for i, it in enumerate(items)],
            )
        db.set_setting(conn, LOADED_KEY, json.dumps({
            "at": datetime.now(timezone.utc).isoformat(), "file": filename, "by": admin_name, "count": len(items),
        }, ensure_ascii=False))


def save_manual(form, admin_name, row_id=None):
    """Add a row by hand (or change a hand-added one); returns the saved row's article for the message."""
    item = {key: _text(form.get(key)) for key, _ in COLUMNS}
    item["barcode"] = re.sub(r"\s", "", item["barcode"])
    if not item["article"] or not item["barcode"]:
        raise NomenclatureError("Заполните «Артикул/цвет» и «Основной баркод».")
    if not item["barcode"].isdigit():
        raise NomenclatureError("Баркод должен состоять только из цифр.")
    with db.tx() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(4243)")
        if row_id is not None:
            row = conn.execute("SELECT manual FROM nomenclature WHERE id = %s", (row_id,)).fetchone()
            if not row or not row["manual"]:
                raise NomenclatureError("Изменять можно только строки, добавленные вручную.")
        other = conn.execute(
            "SELECT article, size FROM nomenclature WHERE barcode = %s AND id IS DISTINCT FROM %s LIMIT 1",
            (item["barcode"], row_id)).fetchone()
        if other:
            raise NomenclatureError(
                f"Баркод {item['barcode']} уже есть: {other['article']}" + (f", размер {other['size']}" if other["size"] else "") + ".")
        if row_id is None:
            conn.execute(
                "INSERT INTO nomenclature (position, category, article, size, barcode, manual, added_by) "
                "SELECT coalesce(max(position), 0) + 1, %s, %s, %s, %s, true, %s FROM nomenclature",
                (item["category"], item["article"], item["size"], item["barcode"], admin_name))
        else:
            conn.execute(
                "UPDATE nomenclature SET category = %s, article = %s, size = %s, barcode = %s WHERE id = %s",
                (item["category"], item["article"], item["size"], item["barcode"], row_id))
    return item


def get_row(conn, row_id):
    return conn.execute("SELECT * FROM nomenclature WHERE id = %s", (row_id,)).fetchone()


def listing(conn, q="", category=""):
    sql, args = ["SELECT * FROM nomenclature WHERE true"], []
    if category:
        sql.append("AND category = %s")
        args.append(category)
    for token in q.lower().split():
        sql.append("AND lower(category || ' ' || article || ' ' || size || ' ' || barcode) LIKE %s")
        args.append(f"%{token}%")
    sql.append("ORDER BY manual DESC, position")
    rows = conn.execute(" ".join(sql), args).fetchall()
    categories = [r["category"] for r in conn.execute(
        "SELECT category, min(position) AS p FROM nomenclature WHERE category <> '' GROUP BY category ORDER BY p")]
    raw = db.get_setting(conn, LOADED_KEY)
    loaded = json.loads(raw) if raw else None
    if loaded:
        loaded["at"] = datetime.fromisoformat(loaded["at"])
    return rows, categories, loaded
