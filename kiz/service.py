"""Business operations: uploads, printing, archive, journal and retention."""
import json
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from . import config, db, small_label
from .labels import GS, LAYOUT_VERSION, Label, LayoutError, code_prefix, from_ru_layout, read_workbook, render_pdf

log = logging.getLogger("kiz")

# LibreOffice is heavy, so files are processed one at a time.
_worker = ThreadPoolExecutor(max_workers=1)


class ServiceError(Exception):
    pass


def upload_dir(upload_id):
    return config.DATA_DIR / "uploads" / str(upload_id)


def labels_pdf(upload_id):
    return upload_dir(upload_id) / "labels.pdf"


# ---------- uploads ----------

def create_upload(filename, content, user_id):
    with db.tx() as conn:
        upload_id = conn.execute(
            "INSERT INTO uploads (filename, user_id) VALUES (%s, %s) RETURNING id",
            (filename, user_id),
        ).fetchone()["id"]
    folder = upload_dir(upload_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "source.xlsx").write_bytes(content)
    _worker.submit(process_upload, upload_id)
    return upload_id


def _summarize(conn, labels):
    known = {
        r["code"]
        for r in conn.execute(
            "SELECT code FROM codes WHERE code = ANY(%s)", ([l.code for l in labels if l.code],)
        )
    }
    groups, seen = {}, set()
    for label in labels:
        key = label.ean or "?"
        g = groups.setdefault(key, {
            "ean": label.ean, "article": label.article, "size": label.size,
            "color": label.color, "new": 0, "duplicates": 0, "errors": 0,
        })
        if label.errors:
            g["errors"] += 1
        elif label.code in known or label.code in seen:
            g["duplicates"] += 1
        else:
            g["new"] += 1
            seen.add(label.code)
    errors = [
        {"index": l.index + 1, "text": ", ".join(l.errors)} for l in labels if l.errors
    ][:50]
    return {
        "total": len(labels),
        "new": sum(g["new"] for g in groups.values()),
        "duplicates": sum(g["duplicates"] for g in groups.values()),
        "errors": sum(g["errors"] for g in groups.values()),
        "groups": list(groups.values()),
        "error_list": errors,
    }


def process_upload(upload_id):
    folder = upload_dir(upload_id)
    try:
        labels = read_workbook(folder / "source.xlsx")
        render_pdf(folder / "source.xlsx", folder / "labels.pdf", len(labels))
        (folder / "layout").write_text(LAYOUT_VERSION)
        (folder / "labels.json").write_text(
            json.dumps([asdict(l) for l in labels], ensure_ascii=False), encoding="utf-8"
        )
        try:
            small_label.extract(folder / "source.xlsx", folder)
        except Exception:
            # The 58×40 data is made again from the source file when it is first needed.
            log.exception("upload %s: small label data not prepared", upload_id)
        with db.tx() as conn:
            summary = _summarize(conn, labels)
            conn.execute(
                "UPDATE uploads SET status = 'ready', summary = %s WHERE id = %s AND status = 'processing'",
                (Jsonb(summary), upload_id),
            )
    except LayoutError as e:
        _fail_upload(upload_id, str(e))
    except Exception:
        log.exception("upload %s failed", upload_id)
        _fail_upload(upload_id, "Файл не удалось разобрать. Проверьте, что это макет этикеток из 1С.")


def _fail_upload(upload_id, message):
    with db.tx() as conn:
        conn.execute(
            "UPDATE uploads SET status = 'failed', error = %s WHERE id = %s", (message, upload_id)
        )


def confirm_upload(upload_id):
    with db.tx() as conn:
        upload = conn.execute(
            "SELECT * FROM uploads WHERE id = %s FOR UPDATE", (upload_id,)
        ).fetchone()
        if not upload or upload["status"] != "ready":
            raise ServiceError("Эту загрузку уже нельзя добавить.")
        raw = json.loads((upload_dir(upload_id) / "labels.json").read_text(encoding="utf-8"))
        labels = [Label(**item) for item in raw]

        product_ids, added = {}, 0
        for label in labels:
            if label.errors:
                continue
            if label.ean not in product_ids:
                search = " ".join([label.article, label.size, label.color, label.ean]).lower()
                product_ids[label.ean] = conn.execute(
                    """
                    INSERT INTO products (ean, article, size, color, search)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (ean) DO UPDATE SET article = EXCLUDED.article,
                        size = EXCLUDED.size, color = EXCLUDED.color, search = EXCLUDED.search
                    RETURNING id
                    """,
                    (label.ean, label.article, label.size, label.color, search),
                ).fetchone()["id"]
            cur = conn.execute(
                """
                INSERT INTO codes (product_id, upload_id, page_no, code, code_prefix)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT (code) DO NOTHING
                """,
                (product_ids[label.ean], upload_id, label.index, label.code, label.code_prefix),
            )
            added += cur.rowcount
        summary = dict(upload["summary"] or {}, added=added)
        conn.execute(
            "UPDATE uploads SET status = 'added', added_at = now(), summary = %s WHERE id = %s",
            (Jsonb(summary), upload_id),
        )
    return added


def cancel_upload(upload_id):
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE uploads SET status = 'cancelled' WHERE id = %s AND status IN ('ready', 'failed')",
            (upload_id,),
        )
    if cur.rowcount:
        shutil.rmtree(upload_dir(upload_id), ignore_errors=True)


def upload_usage(conn, upload_id):
    """How many codes of an upload are still in stock and how many were already printed."""
    return conn.execute(
        """
        SELECT count(*) FILTER (WHERE status = 'stock') AS stock,
               count(*) FILTER (WHERE status = 'printed') AS printed
        FROM codes WHERE upload_id = %s
        """,
        (upload_id,),
    ).fetchone()


def delete_upload(upload_id, admin_id, allow_printed=False):
    """Removes everything an upload brought: its codes, their prints in the archive and journal,
    products left without codes, and its files. The upload itself stays as a «deleted» record."""
    with db.tx() as conn:
        upload = conn.execute("SELECT * FROM uploads WHERE id = %s FOR UPDATE", (upload_id,)).fetchone()
        if not upload or upload["status"] == "deleted":
            raise ServiceError("Загрузка уже удалена.")
        if upload["status"] == "processing":
            raise ServiceError("Файл ещё обрабатывается. Удалить можно, когда обработка закончится.")
        usage = upload_usage(conn, upload_id)
        if usage["printed"] and not allow_printed:
            raise ServiceError("Из этой загрузки уже печатали. Для удаления нужен пароль администратора.")
        products = [r["product_id"] for r in conn.execute(
            "SELECT DISTINCT product_id FROM codes WHERE upload_id = %s", (upload_id,))]
        jobs = [r["job_id"] for r in conn.execute(
            "SELECT DISTINCT jc.job_id FROM print_job_codes jc JOIN codes c ON c.id = jc.code_id "
            "WHERE c.upload_id = %s", (upload_id,))]
        conn.execute("DELETE FROM codes WHERE upload_id = %s", (upload_id,))
        if jobs:
            # A print made only of these codes disappears; a mixed reprint keeps the other codes.
            conn.execute(
                "DELETE FROM print_jobs j WHERE j.id = ANY(%s) "
                "AND NOT EXISTS (SELECT 1 FROM print_job_codes jc WHERE jc.job_id = j.id)", (jobs,))
            conn.execute(
                "UPDATE print_jobs j SET qty = (SELECT count(*) FROM print_job_codes jc WHERE jc.job_id = j.id) "
                "WHERE j.id = ANY(%s)", (jobs,))
        if products:
            conn.execute(
                "DELETE FROM products p WHERE p.id = ANY(%s) "
                "AND NOT EXISTS (SELECT 1 FROM codes c WHERE c.product_id = p.id) "
                "AND NOT EXISTS (SELECT 1 FROM print_jobs j WHERE j.product_id = p.id)", (products,))
        summary = dict(upload["summary"] or {}, deleted_stock=usage["stock"], deleted_printed=usage["printed"],
                       status_before=upload["status"])
        conn.execute(
            "UPDATE uploads SET status = 'deleted', deleted_at = now(), deleted_by = %s, summary = %s WHERE id = %s",
            (admin_id, Jsonb(summary), upload_id),
        )
    shutil.rmtree(upload_dir(upload_id), ignore_errors=True)
    return usage


def rerender_old_layouts():
    """Uploads whose print pages were made by an older version get them made again, one by one."""
    with db.pool.connection() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT DISTINCT upload_id AS id FROM codes WHERE status = 'stock' "
            "UNION SELECT id FROM uploads WHERE status = 'ready'")]
    for upload_id in sorted(ids):
        folder = upload_dir(upload_id)
        marker = folder / "layout"
        if (folder / "source.xlsx").exists() and (not marker.exists() or marker.read_text() != LAYOUT_VERSION):
            _worker.submit(_rerender, upload_id)


def _rerender(upload_id):
    folder = upload_dir(upload_id)
    try:
        pages = len(json.loads((folder / "labels.json").read_text(encoding="utf-8")))
        tmp = folder / "labels.new.pdf"
        render_pdf(folder / "source.xlsx", tmp, pages)
        tmp.replace(folder / "labels.pdf")  # printing never sees a half-written file
        (folder / "layout").write_text(LAYOUT_VERSION)
        log.info("upload %s: print pages made again", upload_id)
    except Exception:
        log.exception("upload %s: print pages not made again, the old ones stay", upload_id)


def recover_interrupted_uploads():
    with db.tx() as conn:
        conn.execute(
            "UPDATE uploads SET status = 'failed', error = %s WHERE status = 'processing'",
            ("Обработка прервалась при перезапуске сервиса. Загрузите файл ещё раз.",),
        )


# ---------- products ----------

def find_products(conn, query, limit=50):
    query = (query or "").strip()
    if not query:
        return []
    tokens = query.lower().split()
    where = " AND ".join(["p.search LIKE %s"] * len(tokens))
    return conn.execute(
        f"""
        SELECT p.*, (SELECT count(*) FROM codes c WHERE c.product_id = p.id AND c.status = 'stock') AS stock
        FROM products p WHERE {where}
        ORDER BY p.article, p.size LIMIT %s
        """,
        [f"%{t}%" for t in tokens] + [limit],
    ).fetchall()


def _edit_distance(a, b, limit):
    """Edits (insert, delete, replace, swap of neighbours) to turn a into b, or limit + 1 if more."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if prev2 is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def _token_cost(token, words):
    """0 when the token is part of a word, the number of typos when it is close to one, None otherwise."""
    if any(token in w for w in words):
        return 0
    if token.isdigit() or len(token) < 4:
        return None
    limit = 1 if len(token) <= 5 else 2
    best = min(
        min(_edit_distance(token, w, limit), _edit_distance(token, w[:len(token)], limit)) for w in words
    ) if words else limit + 1
    return best if best <= limit else None


def suggest_products(conn, query, limit=10):
    """Products for the search suggestions: exact matches first, then ones within a typo or two."""
    tokens = (query or "").lower().replace("ё", "е").split()
    if not tokens:
        return []
    exact = find_products(conn, query, limit)
    if len(exact) >= limit:
        return exact
    scored = []
    seen = {p["id"] for p in exact}
    for p in conn.execute("SELECT id, search FROM products").fetchall():
        if p["id"] in seen:
            continue
        words = p["search"].replace("ё", "е").split()
        costs = [_token_cost(t, words) for t in tokens]
        if None not in costs:
            scored.append((sum(costs), p["id"]))
    scored.sort()
    ids = [pid for _, pid in scored[: limit - len(exact)]]
    if not ids:
        return exact
    fuzzy = conn.execute(
        """
        SELECT p.*, (SELECT count(*) FROM codes c WHERE c.product_id = p.id AND c.status = 'stock') AS stock
        FROM products p WHERE p.id = ANY(%s)
        """,
        (ids,),
    ).fetchall()
    fuzzy.sort(key=lambda p: ids.index(p["id"]))
    return exact + fuzzy


def product_by_ean(conn, ean):
    return conn.execute("SELECT * FROM products WHERE ean = %s", (ean.strip(),)).fetchone()


def get_product(conn, product_id):
    return conn.execute(
        """
        SELECT p.*, (SELECT count(*) FROM codes c WHERE c.product_id = p.id AND c.status = 'stock') AS stock
        FROM products p WHERE p.id = %s
        """,
        (product_id,),
    ).fetchone()


def stock_report(conn):
    return conn.execute(
        """
        SELECT p.*, count(c.id) FILTER (WHERE c.status = 'stock') AS stock,
               count(c.id) FILTER (WHERE c.status = 'printed') AS printed
        FROM products p LEFT JOIN codes c ON c.product_id = p.id
        GROUP BY p.id ORDER BY p.article, p.size
        """
    ).fetchall()


def min_stock(conn):
    return int(db.get_setting(conn, "min_stock", config.DEFAULT_MIN_STOCK))


# ---------- printing ----------

def print_codes(product_id, qty, user):
    if qty < 1:
        raise ServiceError("Укажите количество больше нуля.")
    with db.tx() as conn:
        # Prints of one product queue up on its row, so two people never get the same codes.
        conn.execute("SELECT id FROM products WHERE id = %s FOR UPDATE", (product_id,))
        rows = conn.execute(
            """
            SELECT id FROM codes WHERE product_id = %s AND status = 'stock'
            ORDER BY loaded_at, id LIMIT %s
            """,
            (product_id, qty),
        ).fetchall()
        if len(rows) < qty:
            raise ServiceError(f"В остатке только {len(rows)} шт., напечатать {qty} нельзя.")
        job_id = conn.execute(
            "INSERT INTO print_jobs (kind, product_id, user_id, qty) VALUES ('print', %s, %s, %s) RETURNING id",
            (product_id, user["id"], qty),
        ).fetchone()["id"]
        ids = [r["id"] for r in rows]
        conn.execute(
            """
            UPDATE codes SET status = 'printed', printed_at = now(), printed_by = %s, print_job_id = %s
            WHERE id = ANY(%s)
            """,
            (user["id"], job_id, ids),
        )
        _link_codes(conn, job_id, ids)
    return job_id


def reprint_codes(code_ids, user):
    if not code_ids:
        raise ServiceError("Выберите коды для повторной печати.")
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT id, product_id FROM codes WHERE id = ANY(%s) AND status = 'printed' ORDER BY id",
            (list(code_ids),),
        ).fetchall()
        if len(rows) != len(set(code_ids)):
            raise ServiceError("Часть выбранных кодов не найдена в архиве.")
        products = {r["product_id"] for r in rows}
        job_id = conn.execute(
            "INSERT INTO print_jobs (kind, product_id, user_id, qty) VALUES ('reprint', %s, %s, %s) RETURNING id",
            (products.pop() if len(products) == 1 else None, user["id"], len(rows)),
        ).fetchone()["id"]
        ids = [r["id"] for r in rows]
        conn.execute("UPDATE codes SET reprint_count = reprint_count + 1 WHERE id = ANY(%s)", (ids,))
        _link_codes(conn, job_id, ids)
    return job_id


def _link_codes(conn, job_id, ids):
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO print_job_codes (job_id, code_id, position) VALUES (%s, %s, %s)",
            [(job_id, code_id, pos) for pos, code_id in enumerate(ids)],
        )


def get_job(conn, job_id):
    return conn.execute(
        """
        SELECT j.*, u.name AS user_name, p.article, p.size, p.ean
        FROM print_jobs j JOIN users u ON u.id = j.user_id
        LEFT JOIN products p ON p.id = j.product_id WHERE j.id = %s
        """,
        (job_id,),
    ).fetchone()


def job_codes(conn, job_id):
    return conn.execute(
        """
        SELECT c.*, jc.position, p.article, p.size, p.ean
        FROM print_job_codes jc JOIN codes c ON c.id = jc.code_id
        JOIN products p ON p.id = c.product_id
        WHERE jc.job_id = %s ORDER BY jc.position
        """,
        (job_id,),
    ).fetchall()


def job_pdf_items(conn, job_id, size=None):
    """What build_print_pdf (50×104 pages of the 1С layout) or small_label.build_pdf (58×40) prints."""
    if size == "58x40":
        return [(upload_dir(c["upload_id"]), c["page_no"]) for c in job_codes(conn, job_id)]
    return [(labels_pdf(c["upload_id"]), c["page_no"]) for c in job_codes(conn, job_id)]


def find_code(conn, scanned):
    """A scanned DataMatrix arrives with or without GS separators; match on GTIN + serial."""
    text = from_ru_layout(scanned.strip().replace("\r", "").replace("\n", ""))
    for sep in ("<GS>", "^]", "\\x1d", "~1"):
        text = text.replace(sep, GS)
    if text.startswith("01") and len(text) >= 31:
        prefix = code_prefix(text) if GS in text else text[:31]
    else:
        prefix = text
    return conn.execute(
        """
        SELECT c.*, p.article, p.size, p.ean FROM codes c JOIN products p ON p.id = c.product_id
        WHERE c.code_prefix = %s OR c.code_prefix LIKE %s ORDER BY c.id LIMIT 20
        """,
        (prefix, f"%{prefix}%" if len(prefix) >= 6 else prefix),
    ).fetchall()


# ---------- archive and journal ----------

def _day_bounds(date_from, date_to):
    start = end = None
    if date_from:
        start = datetime.combine(date_from, datetime.min.time(), config.TIMEZONE)
    if date_to:
        end = datetime.combine(date_to, datetime.min.time(), config.TIMEZONE) + timedelta(days=1)
    return start, end


def list_jobs(conn, query="", user_id=None, date_from=None, date_to=None, kind=None, limit=200):
    start, end = _day_bounds(date_from, date_to)
    sql = [
        """
        SELECT j.*, u.name AS user_name, p.article, p.size, p.ean
        FROM print_jobs j JOIN users u ON u.id = j.user_id
        LEFT JOIN products p ON p.id = j.product_id WHERE true
        """
    ]
    args = []
    for token in (query or "").lower().split():
        sql.append("AND p.search LIKE %s")
        args.append(f"%{token}%")
    if user_id:
        sql.append("AND j.user_id = %s")
        args.append(user_id)
    if kind:
        sql.append("AND j.kind = %s")
        args.append(kind)
    if start:
        sql.append("AND j.created_at >= %s")
        args.append(start)
    if end:
        sql.append("AND j.created_at < %s")
        args.append(end)
    sql.append("ORDER BY j.created_at DESC LIMIT %s")
    args.append(limit)
    return conn.execute(" ".join(sql), args).fetchall()


def list_uploads(conn, date_from=None, date_to=None, limit=200):
    start, end = _day_bounds(date_from, date_to)
    sql = ["SELECT up.*, u.name AS user_name, d.name AS deleted_by_name FROM uploads up "
           "LEFT JOIN users u ON u.id = up.user_id LEFT JOIN users d ON d.id = up.deleted_by WHERE true"]
    args = []
    if start:
        sql.append("AND up.created_at >= %s")
        args.append(start)
    if end:
        sql.append("AND up.created_at < %s")
        args.append(end)
    sql.append("ORDER BY up.created_at DESC LIMIT %s")
    args.append(limit)
    return conn.execute(" ".join(sql), args).fetchall()


# ---------- retention ----------

def cleanup():
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.ARCHIVE_DAYS)
    with db.tx() as conn:
        codes = conn.execute(
            "DELETE FROM codes WHERE status = 'printed' AND printed_at < %s", (cutoff,)
        ).rowcount
        jobs = conn.execute("DELETE FROM print_jobs WHERE created_at < %s", (cutoff,)).rowcount
        finished = conn.execute(
            """
            SELECT id FROM uploads up WHERE status IN ('added', 'cancelled', 'failed')
              AND created_at < now() - interval '1 day'
              AND NOT EXISTS (SELECT 1 FROM codes c WHERE c.upload_id = up.id)
            """
        ).fetchall()
        conn.execute(
            """
            DELETE FROM uploads up WHERE created_at < %s
              AND NOT EXISTS (SELECT 1 FROM codes c WHERE c.upload_id = up.id)
            """,
            (cutoff,),
        )
    for row in finished:
        shutil.rmtree(upload_dir(row["id"]), ignore_errors=True)
    if codes or jobs:
        log.info("cleanup: removed %s archived codes and %s print jobs", codes, jobs)
