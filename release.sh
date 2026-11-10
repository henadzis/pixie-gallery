#!/usr/bin/env bash
set -e

# Проверка указания версии
if [ -z "$1" ]; then
  echo "Использование: ./release.sh <версия> [сообщение коммита]"
  echo "Пример: ./release.sh 0.4 \"Добавлено переключение темы для всех\""
  exit 1
fi

VERSION="$1"
# Если версия начинается не с 'v', добавим для тега 'v', но сохраним и 'чистую' версию
TAG_NAME="${VERSION}"
if [[ "$VERSION" =~ ^[0-9] ]]; then
  TAG_NAME="v${VERSION}"
fi

MSG="${2:-Release $TAG_NAME}"

echo "==> 1. Добавление изменений в git..."
git add -A

# Коммитим, если есть что коммитить
if ! git diff-index --quiet HEAD --; then
  echo "==> 2. Создание коммита: $MSG"
  git commit -m "$MSG"
else
  echo "==> 2. Нет новых незакоммиченных изменений."
fi

echo "==> 3. Создание тега $TAG_NAME..."
# Проверяем, существует ли уже локальный тег
if git rev-parse "$TAG_NAME" >/dev/null 2>&1; then
  echo "Тег $TAG_NAME уже существует. Перезаписать? (Ctrl+C для отмены, Enter для продолжения)"
  read -r
  git tag -d "$TAG_NAME"
fi
git tag -a "$TAG_NAME" -m "$MSG"

echo "==> 4. Отправка ветки main и тега $TAG_NAME в GitHub..."
git push origin main
git push origin "$TAG_NAME"

echo ""
echo "✅ Успешно! Тег $TAG_NAME отправлен в GitHub."
echo "🚀 GitHub Actions автоматически начал сборку Docker-образа с версией $TAG_NAME и latest."
echo "Следить за сборкой: https://github.com/henadzis/pixie-gallery/actions"
