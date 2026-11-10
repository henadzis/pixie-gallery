#!/bin/bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PYTHON_CMD="python3"
if ! command -v python3 >/dev/null 2>&1 && [ -x "/usr/bin/python3" ]; then
    PYTHON_CMD="/usr/bin/python3"
fi

echo "=== [1/3] Создание виртуального окружения Python (.venv) ==="
if [ ! -d ".venv" ]; then
    $PYTHON_CMD -m venv .venv
    echo "Виртуальное окружение создано."
else
    echo "Виртуальное окружение .venv уже существует."
fi

echo "=== [2/3] Установка зависимостей из requirements.txt ==="
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "=== [3/3] Подготовка тестовых директорий ==="
mkdir -p test_photos/2026/Sample_Album
mkdir -p test_cache

if [ -z "$(ls -A test_photos/2026/Sample_Album 2>/dev/null)" ]; then
    if [ -f "app/ico.jpg" ]; then
        cp app/ico.jpg test_photos/2026/Sample_Album/sample_photo_1.jpg
        cp app/ico.jpg test_photos/2026/Sample_Album/sample_photo_2.jpg
        echo "Созданы тестовые фото в test_photos/2026/Sample_Album"
    fi
fi

echo ""
echo "Настройка завершена! Для запуска выполните:"
echo "  ./run_local.sh"
echo ""
