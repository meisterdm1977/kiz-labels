FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv libreoffice-calc-nogui libdmtx0b \
      fonts-crosextra-carlito fonts-crosextra-caladea fonts-dejavu-core fonts-liberation \
      poppler-utils ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /venv
ENV PATH=/venv/bin:$PATH
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY kiz ./kiz

ENV DATA_DIR=/data PYTHONUNBUFFERED=1 HOME=/tmp
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "kiz.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
