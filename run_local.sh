#!/bin/bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -d ".venv" ]; then
    echo "Виртуальное окружение .venv не найдено. Запуск первичной настройки..."
    ./setup_local.sh
fi

source .venv/bin/activate

export PHOTOS_DIR="${PHOTOS_DIR:-$DIR/test_photos}"
export CACHE_DIR="${CACHE_DIR:-$DIR/test_cache}"
export GALLERY_TITLE="${GALLERY_TITLE:-PIXIE LOCAL DEV}"
export ADMIN_USER="${ADMIN_USER:-admin}"
export ADMIN_PASS="${ADMIN_PASS:-admin123}"
export SECRET_KEY="${SECRET_KEY:-local-secret-key-12345}"
export PYTHONPATH="$DIR"

echo "=================================================="
echo " Pixie Gallery запущена локально!"
echo " URL:       http://localhost:8085"
echo " Логин:     $ADMIN_USER"
echo " Пароль:    $ADMIN_PASS"
echo " Фото:      $PHOTOS_DIR"
echo " Кэш:       $CACHE_DIR"
echo " Режим:     Live-reload (правки применяются на лету)"
echo "=================================================="

exec uvicorn app.main:app --host 127.0.0.1 --port 8085 --reload
