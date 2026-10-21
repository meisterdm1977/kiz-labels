#!/bin/bash
# Установка или обновление сервиса на чистом сервере Ubuntu 24.04.
# Запуск от root:  curl -fsSL https://raw.githubusercontent.com/meisterdm1977/kiz-labels/main/install.sh | bash
# Свой домен:      ... | DOMAIN=kiz.example.ru bash
set -euo pipefail

REPO=https://github.com/meisterdm1977/kiz-labels.git
DIR=/opt/kiz

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите от пользователя root." >&2
  exit 1
fi

echo "== Устанавливаю необходимые программы"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git curl openssl cron
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi

echo "== Защищаю сервер"
apt-get install -y -q ufw fail2ban python3-systemd unattended-upgrades
# Открыты только SSH и сайт; база данных наружу не смотрит.
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
# Блокировка IP после 5 неудачных входов по SSH за 10 минут, на 1 час.
cat > /etc/fail2ban/jail.d/kiz.local <<'EOF2'
[sshd]
enabled = true
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
EOF2
systemctl enable --now fail2ban >/dev/null 2>&1 || true
systemctl restart fail2ban || true
# Автоматическая установка обновлений безопасности системы.
echo 'APT::Periodic::Update-Package-Lists "1"; APT::Periodic::Unattended-Upgrade "1";' > /etc/apt/apt.conf.d/20auto-upgrades

echo "== Скачиваю сервис"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone "$REPO" "$DIR"
fi
cd "$DIR"

FIRST_RUN=0
if [ ! -f .env ]; then
  FIRST_RUN=1
  if [ -z "${DOMAIN:-}" ]; then
    IP=$(curl -fsS https://api.ipify.org || hostname -I | awk '{print $1}')
    # Бесплатный адрес вида 1.2.3.4.sslip.io ведёт на этот сервер; позже его можно заменить своим доменом.
    DOMAIN="$IP.sslip.io"
  fi
  ADMIN_PASSWORD=$(openssl rand -base64 12 | tr -d '/+=' | cut -c1-12)
  cat > .env <<EOF
DOMAIN=$DOMAIN
DB_PASSWORD=$(openssl rand -hex 24)
SECRET_KEY=$(openssl rand -hex 32)
ADMIN_LOGIN=admin
ADMIN_PASSWORD=$ADMIN_PASSWORD
EOF
  chmod 600 .env
fi

echo "== Собираю и запускаю (первый раз это занимает несколько минут)"
docker compose up -d --build

if ! crontab -l 2>/dev/null | grep -q "$DIR/backup.sh"; then
  (crontab -l 2>/dev/null; echo "0 3 * * * $DIR/backup.sh >> $DIR/backups.log 2>&1") | crontab -
fi

DOMAIN=$(grep '^DOMAIN=' .env | cut -d= -f2)
echo
echo "=============================================="
echo " Готово. Адрес сервиса: https://$DOMAIN"
if [ "$FIRST_RUN" = 1 ]; then
  echo " Логин:  admin"
  echo " Пароль: $(grep '^ADMIN_PASSWORD=' .env | cut -d= -f2)"
  echo " Запишите пароль. Увидеть его снова: grep ADMIN_PASSWORD $DIR/.env"
fi
echo " Сайт может открыться через 1-2 минуты, пока выпускается сертификат."
echo "=============================================="
