import os
from pathlib import Path
from zoneinfo import ZoneInfo


def _env(name, default=None):
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


DATABASE_URL = _env("DATABASE_URL", "postgresql://kiz:kiz@localhost:5432/kiz")
SECRET_KEY = _env("SECRET_KEY", "dev-secret-change-me")
DATA_DIR = Path(_env("DATA_DIR", "./data")).resolve()

ADMIN_LOGIN = os.environ.get("ADMIN_LOGIN", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

TIMEZONE = ZoneInfo(_env("TZ_NAME", "Europe/Moscow"))

# Label size in the source layout, cut from the top-left corner of each page.
LABEL_WIDTH_MM = float(_env("LABEL_WIDTH_MM", "50"))
LABEL_HEIGHT_MM = float(_env("LABEL_HEIGHT_MM", "104"))

ARCHIVE_DAYS = int(_env("ARCHIVE_DAYS", "183"))
# How long a freshly created print job's PDF can be opened without a personal code.
PRINT_LINK_MINUTES = int(_env("PRINT_LINK_MINUTES", "10"))
ADMIN_IDLE_MINUTES = int(_env("ADMIN_IDLE_MINUTES", "15"))
DEFAULT_MIN_STOCK = int(_env("DEFAULT_MIN_STOCK", "50"))

SOFFICE = _env("SOFFICE", "soffice")
