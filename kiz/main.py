import asyncio
import io
import logging
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path

import openpyxl
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.errors import UniqueViolation
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, db, service
from .auth import LoginRequired, require
from .badges import badge_pdf
from .labels import build_print_pdf, printable_code

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kiz")

HERE = Path(__file__).parent
STAFF = ("admin", "operator")
ANYONE = ("admin", "operator", "station")


async def _cleanup_loop():
    while True:
        try:
            await asyncio.to_thread(service.cleanup)
        except Exception:
            log.exception("cleanup failed")
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(app):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.init()
    auth.ensure_admin()
    service.recover_interrupted_uploads()
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()
    db.pool.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    session_cookie="kiz_session",
    max_age=60 * 60 * 24 * 30,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


def _local(value):
    if not value:
        return ""
    return value.astimezone(config.TIMEZONE).strftime("%d.%m.%Y %H:%M")


templates.env.filters["local"] = _local
templates.env.filters["code"] = printable_code
templates.env.globals["role_names"] = auth.ROLE_NAMES


@app.exception_handler(LoginRequired)
async def _login_required(request, exc):
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


def render(request, name, user=None, **context):
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(
        request, name, {"user": user, "flash": flash, **context}
    )


def redirect(url, request=None, message=None, error=False):
    if request is not None and message:
        request.session["flash"] = {"text": message, "error": error}
    return RedirectResponse(url, status_code=303)


def _date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _int(value):
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


# ---------- login ----------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    return render(request, "login.html", next=next)


@app.post("/login")
def login(request: Request, login: str = Form(...), password: str = Form(...), next: str = Form("/")):
    with db.pool.connection() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE login = %s AND active AND role <> 'packer'", (login.strip(),)
        ).fetchone()
    if not user or not auth.check_password(password, user["password_hash"]):
        return redirect(f"/login?next={next}", request, "Неверный логин или пароль", error=True)
    request.session.clear()
    request.session["user_id"] = user["id"]
    return redirect(next if next.startswith("/") and not next.startswith("//") else "/")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@app.get("/")
def home(request: Request):
    user = require(request)
    return redirect("/pack" if user["role"] == "station" else "/stock")


# ---------- packing station ----------

@app.get("/pack", response_class=HTMLResponse)
def pack(request: Request, q: str = ""):
    user = require(request, *ANYONE)
    q = q.strip()
    with db.pool.connection() as conn:
        if q.isdigit() and len(q) == 13:
            product = service.product_by_ean(conn, q)
            if product:
                return redirect(f"/pack/product/{product['id']}")
        products = service.find_products(conn, q)
    return render(request, "pack.html", user, q=q, products=products)


@app.get("/pack/product/{product_id}", response_class=HTMLResponse)
def pack_product(request: Request, product_id: int):
    user = require(request, *ANYONE)
    with db.pool.connection() as conn:
        product = service.get_product(conn, product_id)
        last_jobs = service.list_jobs(conn, query=product["ean"], limit=5) if product else []
    if not product:
        return redirect("/pack", request, "Товар не найден", error=True)
    return render(request, "product.html", user, product=product, last_jobs=last_jobs)


def _remember_job(request, job_id):
    jobs = [j for j in request.session.get("jobs", []) if time.time() - j[1] < config.PRINT_LINK_MINUTES * 60]
    jobs.append([job_id, time.time()])
    request.session["jobs"] = jobs[-20:]


def _job_allowed(request, job_id):
    return any(
        j[0] == job_id and time.time() - j[1] < config.PRINT_LINK_MINUTES * 60
        for j in request.session.get("jobs", [])
    )


@app.post("/print")
def do_print(request: Request, product_id: int = Form(...), qty: str = Form(...), personal_code: str = Form("")):
    require(request, *ANYONE)
    back = f"/pack/product/{product_id}"
    count = _int(qty)
    if not count or count < 1:
        return redirect(back, request, "Укажите количество цифрами", error=True)
    with db.pool.connection() as conn:
        employee = auth.user_by_code(conn, personal_code)
    if not employee:
        return redirect(back, request, "Личный код не найден. Отсканируйте бейдж ещё раз.", error=True)
    try:
        job_id = service.print_codes(product_id, count, employee)
    except service.ServiceError as e:
        return redirect(back, request, str(e), error=True)
    _remember_job(request, job_id)
    return redirect(f"/jobs/{job_id}/print")


@app.get("/jobs/{job_id}/print", response_class=HTMLResponse)
def job_print_page(request: Request, job_id: int):
    user = require(request, *ANYONE)
    with db.pool.connection() as conn:
        job = service.get_job(conn, job_id)
    if not job:
        return redirect("/pack", request, "Печать не найдена", error=True)
    return render(request, "print.html", user, job=job, allowed=_job_allowed(request, job_id),
                  minutes=config.PRINT_LINK_MINUTES)


@app.get("/jobs/{job_id}/pdf")
def job_pdf(request: Request, job_id: int):
    require(request, *ANYONE)
    if not _job_allowed(request, job_id):
        return Response("Ссылка на печать устарела. Перепечатайте из архива по личному коду.",
                        status_code=403, media_type="text/plain; charset=utf-8")
    with db.pool.connection() as conn:
        items = service.job_pdf_items(conn, job_id)
    buf = io.BytesIO()
    build_print_pdf(items, buf)
    return Response(buf.getvalue(), media_type="application/pdf",
                    headers={"Cache-Control": "no-store",
                             "Content-Disposition": f'inline; filename="print-{job_id}.pdf"'})


# ---------- archive ----------

@app.get("/archive", response_class=HTMLResponse)
def archive(request: Request, q: str = "", code: str = "", user_id: str = "",
            date_from: str = "", date_to: str = ""):
    user = require(request, *ANYONE)
    with db.pool.connection() as conn:
        found_codes = service.find_code(conn, code) if code.strip() else None
        jobs = [] if found_codes is not None else service.list_jobs(
            conn, q, _int(user_id), _date(date_from), _date(date_to), limit=100
        )
        users = conn.execute("SELECT id, name FROM users WHERE code_hash IS NOT NULL ORDER BY name").fetchall()
    return render(request, "archive.html", user, q=q, code=code, user_id=_int(user_id),
                  date_from=date_from, date_to=date_to, jobs=jobs, found_codes=found_codes, users=users)


@app.get("/archive/job/{job_id}", response_class=HTMLResponse)
def archive_job(request: Request, job_id: int, highlight: int = 0):
    user = require(request, *ANYONE)
    with db.pool.connection() as conn:
        job = service.get_job(conn, job_id)
        codes = service.job_codes(conn, job_id) if job else []
    if not job:
        return redirect("/archive", request, "Печать не найдена", error=True)
    return render(request, "archive_job.html", user, job=job, codes=codes, highlight=highlight)


@app.post("/reprint")
async def reprint(request: Request):
    require(request, *ANYONE)
    form = await request.form()
    job_id = _int(form.get("job_id"))
    back = f"/archive/job/{job_id}" if job_id else "/archive"
    code_ids = [int(v) for v in form.getlist("code_id") if str(v).isdigit()]
    with db.pool.connection() as conn:
        employee = auth.user_by_code(conn, form.get("personal_code", ""))
    if not employee:
        return redirect(back, request, "Личный код не найден. Повторная печать только по личному коду.", error=True)
    try:
        new_job = service.reprint_codes(code_ids, employee)
    except service.ServiceError as e:
        return redirect(back, request, str(e), error=True)
    _remember_job(request, new_job)
    return redirect(f"/jobs/{new_job}/print")


# ---------- uploads ----------

@app.get("/uploads", response_class=HTMLResponse)
def uploads(request: Request):
    user = require(request, *STAFF)
    with db.pool.connection() as conn:
        items = service.list_uploads(conn, limit=50)
    return render(request, "uploads.html", user, uploads=items)


@app.post("/uploads")
async def upload_files(request: Request, files: list[UploadFile] = File(...)):
    user = require(request, *STAFF)
    ids = []
    for f in files:
        if not f.filename:
            continue
        if not f.filename.lower().endswith(".xlsx"):
            return redirect("/uploads", request, f"{f.filename}: нужен файл Excel (.xlsx)", error=True)
        content = await f.read()
        ids.append(service.create_upload(f.filename, content, user["id"]))
    if not ids:
        return redirect("/uploads", request, "Выберите файл", error=True)
    if len(ids) == 1:
        return redirect(f"/uploads/{ids[0]}")
    return redirect("/uploads", request, f"Файлов принято: {len(ids)}. Они обрабатываются по очереди.")


@app.get("/uploads/{upload_id}", response_class=HTMLResponse)
def upload_detail(request: Request, upload_id: int):
    user = require(request, *STAFF)
    with db.pool.connection() as conn:
        upload = conn.execute(
            "SELECT up.*, u.name AS user_name FROM uploads up LEFT JOIN users u ON u.id = up.user_id WHERE up.id = %s",
            (upload_id,),
        ).fetchone()
    if not upload:
        return redirect("/uploads", request, "Загрузка не найдена", error=True)
    return render(request, "upload.html", user, upload=upload)


@app.post("/uploads/{upload_id}/confirm")
def upload_confirm(request: Request, upload_id: int):
    require(request, *STAFF)
    try:
        added = service.confirm_upload(upload_id)
    except service.ServiceError as e:
        return redirect(f"/uploads/{upload_id}", request, str(e), error=True)
    return redirect(f"/uploads/{upload_id}", request, f"Добавлено в хранилище: {added} шт.")


@app.post("/uploads/{upload_id}/cancel")
def upload_cancel(request: Request, upload_id: int):
    require(request, *STAFF)
    service.cancel_upload(upload_id)
    return redirect("/uploads", request, "Загрузка отменена")


# ---------- stock and journal ----------

@app.get("/stock", response_class=HTMLResponse)
def stock(request: Request, q: str = "", low: int = 0):
    user = require(request, *STAFF)
    with db.pool.connection() as conn:
        rows = service.stock_report(conn)
        threshold = service.min_stock(conn)
    tokens = q.lower().split()
    rows = [r for r in rows if all(t in r["search"] for t in tokens)]
    if low:
        rows = [r for r in rows if r["stock"] < threshold]
    low_count = sum(1 for r in rows if r["stock"] < threshold)
    return render(request, "stock.html", user, rows=rows, q=q, low=low, threshold=threshold, low_count=low_count)


@app.post("/stock/threshold")
def stock_threshold(request: Request, value: str = Form(...)):
    require(request, *STAFF)
    number = _int(value)
    if number is None or number < 0:
        return redirect("/stock", request, "Порог должен быть числом", error=True)
    with db.tx() as conn:
        db.set_setting(conn, "min_stock", number)
    return redirect("/stock", request, "Порог сохранён")


def _xlsx(headers, rows, filename):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    for i, h in enumerate(headers, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = max(12, len(h) + 4)
    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/stock.xlsx")
def stock_xlsx(request: Request):
    require(request, *STAFF)
    with db.pool.connection() as conn:
        rows = service.stock_report(conn)
    return _xlsx(
        ["Артикул", "Размер", "Цвет", "EAN", "В остатке", "В архиве"],
        [[r["article"], r["size"], r["color"], r["ean"], r["stock"], r["printed"]] for r in rows],
        "ostatki.xlsx",
    )


@app.get("/journal", response_class=HTMLResponse)
def journal(request: Request, q: str = "", user_id: str = "", date_from: str = "", date_to: str = "",
            kind: str = ""):
    user = require(request, *STAFF)
    with db.pool.connection() as conn:
        jobs = service.list_jobs(conn, q, _int(user_id), _date(date_from), _date(date_to),
                                 kind or None, limit=500)
        uploads = [] if (q or user_id or kind) else service.list_uploads(conn, _date(date_from), _date(date_to))
        users = conn.execute("SELECT id, name FROM users WHERE code_hash IS NOT NULL ORDER BY name").fetchall()
    entries = [{"at": j["created_at"], "type": j["kind"], "row": j} for j in jobs]
    entries += [{"at": u["created_at"], "type": "upload", "row": u} for u in uploads]
    entries.sort(key=lambda e: e["at"], reverse=True)
    total_printed = sum(j["qty"] for j in jobs if j["kind"] == "print")
    total_reprinted = sum(j["qty"] for j in jobs if j["kind"] == "reprint")
    return render(request, "journal.html", user, entries=entries, q=q, user_id=_int(user_id),
                  date_from=date_from, date_to=date_to, kind=kind, users=users,
                  total_printed=total_printed, total_reprinted=total_reprinted)


@app.get("/journal.xlsx")
def journal_xlsx(request: Request, q: str = "", user_id: str = "", date_from: str = "", date_to: str = "",
                 kind: str = ""):
    require(request, *STAFF)
    with db.pool.connection() as conn:
        jobs = service.list_jobs(conn, q, _int(user_id), _date(date_from), _date(date_to),
                                 kind or None, limit=100000)
    names = {"print": "Печать", "reprint": "Повторная печать"}
    return _xlsx(
        ["Дата и время", "Действие", "Сотрудник", "Артикул", "Размер", "EAN", "Количество", "Номер операции"],
        [[_local(j["created_at"]), names[j["kind"]], j["user_name"], j["article"] or "", j["size"] or "",
          j["ean"] or "", j["qty"], j["id"]] for j in jobs],
        "zhurnal.xlsx",
    )


# ---------- employees ----------

@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    user = require(request, "admin")
    with db.pool.connection() as conn:
        users = conn.execute("SELECT * FROM users ORDER BY active DESC, role, name").fetchall()
    new_code = request.session.get("new_code")
    return render(request, "users.html", user, users=users, new_code=new_code)


@app.post("/users")
def create_user(request: Request, name: str = Form(...), role: str = Form(...),
                login: str = Form(""), password: str = Form("")):
    require(request, "admin")
    name, login = name.strip(), login.strip()
    if role not in auth.ROLE_NAMES or not name:
        return redirect("/users", request, "Укажите имя и роль", error=True)
    needs_login = role in ("admin", "operator", "station")
    if needs_login and (not login or len(password) < 8):
        return redirect("/users", request, "Для этой роли нужен логин и пароль не короче 8 символов", error=True)
    code = auth.new_personal_code() if role != "station" else None
    try:
        with db.tx() as conn:
            user_id = conn.execute(
                "INSERT INTO users (name, role, login, password_hash, code_hash) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (name, role, login or None, auth.hash_password(password) if needs_login else None,
                 auth.hash_code(code) if code else None),
            ).fetchone()["id"]
    except UniqueViolation:
        return redirect("/users", request, "Такой логин уже есть", error=True)
    if code:
        request.session["new_code"] = {"user_id": user_id, "name": name, "code": code}
    return redirect("/users", request, f"Сотрудник «{name}» добавлен")


@app.post("/users/{user_id}/code")
def reset_code(request: Request, user_id: int):
    require(request, "admin")
    code = auth.new_personal_code()
    with db.tx() as conn:
        row = conn.execute(
            "UPDATE users SET code_hash = %s WHERE id = %s AND role <> 'station' RETURNING name",
            (auth.hash_code(code), user_id),
        ).fetchone()
    if not row:
        return redirect("/users", request, "Сотрудник не найден", error=True)
    request.session["new_code"] = {"user_id": user_id, "name": row["name"], "code": code}
    return redirect("/users", request, "Выдан новый личный код, старый больше не действует")


@app.post("/users/{user_id}/toggle")
def toggle_user(request: Request, user_id: int, field: str = Form(...)):
    current = require(request, "admin")
    if field not in ("active", "can_reprint"):
        return redirect("/users")
    if field == "active" and user_id == current["id"]:
        return redirect("/users", request, "Нельзя отключить самого себя", error=True)
    with db.tx() as conn:
        conn.execute(f"UPDATE users SET {field} = NOT {field} WHERE id = %s", (user_id,))
    return redirect("/users")


@app.post("/users/{user_id}/password")
def set_password(request: Request, user_id: int, password: str = Form(...)):
    require(request, "admin")
    if len(password) < 8:
        return redirect("/users", request, "Пароль не короче 8 символов", error=True)
    with db.tx() as conn:
        conn.execute("UPDATE users SET password_hash = %s WHERE id = %s AND login IS NOT NULL",
                     (auth.hash_password(password), user_id))
    return redirect("/users", request, "Пароль изменён")


@app.get("/users/badge.pdf")
def badge(request: Request):
    require(request, "admin")
    new_code = request.session.get("new_code")
    if not new_code:
        return redirect("/users", request, "Бейдж можно напечатать сразу после выдачи кода", error=True)
    return Response(badge_pdf(new_code["name"], new_code["code"]), media_type="application/pdf",
                    headers={"Cache-Control": "no-store"})


@app.post("/users/badge/done")
def badge_done(request: Request):
    require(request, "admin")
    request.session.pop("new_code", None)
    return redirect("/users")


@app.get("/health")
def health():
    with db.pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}
