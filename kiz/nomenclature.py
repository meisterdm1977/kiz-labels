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
    with db.tx() as conn:
        conn.execute("DELETE FROM nomenclature")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO nomenclature (position, category, article, size, barcode) VALUES (%s, %s, %s, %s, %s)",
                [(i, it["category"], it["article"], it["size"], it["barcode"]) for i, it in enumerate(items)],
            )
        db.set_setting(conn, LOADED_KEY, json.dumps({
            "at": datetime.now(timezone.utc).isoformat(), "file": filename, "by": admin_name, "count": len(items),
        }, ensure_ascii=False))


def listing(conn, q="", category=""):
    sql, args = ["SELECT * FROM nomenclature WHERE true"], []
    if category:
        sql.append("AND category = %s")
        args.append(category)
    for token in q.lower().split():
        sql.append("AND lower(category || ' ' || article || ' ' || size || ' ' || barcode) LIKE %s")
        args.append(f"%{token}%")
    sql.append("ORDER BY position")
    rows = conn.execute(" ".join(sql), args).fetchall()
    categories = [r["category"] for r in conn.execute(
        "SELECT category, min(position) AS p FROM nomenclature WHERE category <> '' GROUP BY category ORDER BY p")]
    raw = db.get_setting(conn, LOADED_KEY)
    loaded = json.loads(raw) if raw else None
    if loaded:
        loaded["at"] = datetime.fromisoformat(loaded["at"])
    return rows, categories, loaded
