import re
import threading
import time

from pypdf import PdfReader

from kiz import db
from make_layout import make_layout


def login(client, login="admin", password="admin-password"):
    r = client.post("/login", data={"login": login, "password": password}, follow_redirects=False)
    assert r.status_code == 303 and "/login" not in r.headers["location"]


def wait_upload(client, upload_id):
    for _ in range(240):
        with db.pool.connection() as conn:
            status = conn.execute("SELECT status FROM uploads WHERE id = %s", (upload_id,)).fetchone()["status"]
        if status != "processing":
            return status
        time.sleep(0.5)
    raise AssertionError("upload still processing")


def upload(client, path, name="Шорты 02 коричневый - 40-6шт.xlsx"):
    with open(path, "rb") as f:
        r = client.post("/uploads", files={"files": (name, f.read())}, follow_redirects=False)
    upload_id = int(r.headers["location"].rsplit("/", 1)[1])
    return upload_id, wait_upload(client, upload_id)


def create_packer(client, name):
    client.post("/users", data={"name": name, "role": "packer"})
    page = client.get("/users").text
    client.post("/users/badge/done")
    return re.search(r'class="mono big">(\d{8})<', page).group(1)


def stock_of(ean):
    with db.pool.connection() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM codes c JOIN products p ON p.id = c.product_id "
            "WHERE p.ean = %s AND c.status = 'stock'", (ean,)
        ).fetchone()["n"]


def test_full_cycle(client, tmp_path):
    assert client.get("/pack", follow_redirects=False).status_code == 303
    login(client)
    anna = create_packer(client, "Анна")

    first = tmp_path / "a.xlsx"
    codes = make_layout(first, 6, seed=1)
    upload_id, status = upload(client, first)
    assert status == "ready"
    page = client.get(f"/uploads/{upload_id}").text
    assert "Новых: <b>6</b>" in page
    client.post(f"/uploads/{upload_id}/confirm")
    assert stock_of("2047362903387") == 6

    # The same file again: every code is a duplicate, nothing is added.
    upload_id2, _ = upload(client, first)
    assert "Новых: <b>0</b>" in client.get(f"/uploads/{upload_id2}").text
    client.post(f"/uploads/{upload_id2}/cancel")

    # A new batch of the same product adds to the stock.
    second = tmp_path / "b.xlsx"
    make_layout(second, 3, seed=2)
    upload_id3, _ = upload(client, second)
    client.post(f"/uploads/{upload_id3}/confirm")
    assert stock_of("2047362903387") == 9

    # Scanning the EAN opens the product, text search finds it by article and size.
    r = client.get("/pack", params={"q": "2047362903387"}, follow_redirects=False)
    product_url = r.headers["location"]
    product_id = int(product_url.rsplit("/", 1)[1])
    assert "Шорты 02 коричневый" in client.get("/pack", params={"q": "шорты 02 40"}).text

    # Wrong personal code prints nothing.
    client.post("/print", data={"product_id": product_id, "qty": "2", "personal_code": "000"})
    assert stock_of("2047362903387") == 9

    r = client.post("/print", data={"product_id": product_id, "qty": "4", "personal_code": anna},
                    follow_redirects=False)
    job_id = int(re.search(r"/jobs/(\d+)/print", r.headers["location"]).group(1))
    assert stock_of("2047362903387") == 5

    pdf = client.get(f"/jobs/{job_id}/pdf")
    assert pdf.status_code == 200
    pages = PdfReader(__import__("io").BytesIO(pdf.content)).pages
    assert len(pages) == 4
    assert round(float(pages[0].mediabox.height) / 72 * 25.4) == 104

    # Oldest codes go first.
    with db.pool.connection() as conn:
        printed = [r["code"] for r in conn.execute(
            "SELECT c.code FROM print_job_codes jc JOIN codes c ON c.id = jc.code_id "
            "WHERE jc.job_id = %s ORDER BY jc.position", (job_id,))]
    assert printed == codes[:4]

    # Cannot print more than the stock.
    client.post("/print", data={"product_id": product_id, "qty": "6", "personal_code": anna})
    assert stock_of("2047362903387") == 5

    # Archive: find a code by its scan without GS separators, reprint with personal code.
    scan = codes[1].replace("\x1d", "")
    archive = client.get("/archive", params={"code": scan}).text
    assert "напечатан" in archive
    with db.pool.connection() as conn:
        code_id = conn.execute("SELECT id FROM codes WHERE code = %s", (codes[1],)).fetchone()["id"]
    client.post("/reprint", data={"job_id": job_id, "code_id": code_id, "personal_code": "bad"})
    r = client.post("/reprint", data={"job_id": job_id, "code_id": code_id, "personal_code": anna},
                    follow_redirects=False)
    reprint_job = int(re.search(r"/jobs/(\d+)/print", r.headers["location"]).group(1))
    assert len(PdfReader(__import__("io").BytesIO(client.get(f"/jobs/{reprint_job}/pdf").content)).pages) == 1
    assert stock_of("2047362903387") == 5

    journal = client.get("/journal").text
    assert "Анна" in journal and "Повторная печать" in journal
    assert "Напечатано: <b>4</b>" in journal and "Перепечатано: <b>1</b>" in journal
    assert client.get("/journal.xlsx").status_code == 200
    assert client.get("/stock.xlsx").status_code == 200


def test_print_link_expires_for_other_sessions(client):
    with db.pool.connection() as conn:
        job = conn.execute("SELECT id FROM print_jobs ORDER BY id LIMIT 1").fetchone()
    from fastapi.testclient import TestClient
    from kiz.main import app
    other = TestClient(app)
    login(other)
    assert other.get(f"/jobs/{job['id']}/pdf").status_code == 403


def test_parallel_printing_never_shares_codes(client, tmp_path):
    path = tmp_path / "c.xlsx"
    make_layout(path, 20, ean="2047000000001", size="42", seed=3)
    upload_id, _ = upload(client, path)
    client.post(f"/uploads/{upload_id}/confirm")
    from kiz import service
    with db.pool.connection() as conn:
        product_id = conn.execute("SELECT id FROM products WHERE ean = '2047000000001'").fetchone()["id"]
        user = conn.execute("SELECT * FROM users WHERE role = 'packer' LIMIT 1").fetchone()

    results, errors = [], []

    def worker():
        try:
            results.append(service.print_codes(product_id, 3, user))
        except service.ServiceError as e:
            errors.append(str(e))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with db.pool.connection() as conn:
        rows = conn.execute(
            "SELECT jc.code_id FROM print_job_codes jc WHERE jc.job_id = ANY(%s)", (results,)
        ).fetchall()
    ids = [r["code_id"] for r in rows]
    assert len(ids) == len(set(ids)) == 3 * len(results)
    assert len(results) == 6 and len(errors) == 2
    assert stock_of("2047000000001") == 2
