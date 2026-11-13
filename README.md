# Pixie Gallery

Легковесная веб-галерея для просмотра локального фотоархива и клиентского отбора фотографий с поддержкой многопользовательского доступа.

## Возможности
- Двухуровневый кэш превью (400px для плитки, 1200px для лайтбокса).
- Умный Zoom с панорамированием оригинального кадра.
- Извлечение и кэширование EXIF-данных (камера, объектив, выдержка, ISO, разрешение).
- Скачивание альбомов и избранных кадров в ZIP.
- Поддержка нескольких независимых библиотек (папок с разных дисков).
- Разграничение доступа к библиотекам и папкам для пользователей.
- Двуязычный интерфейс (RU / EN).

## Документация и руководства
Подробные инструкции находятся в папке [`docs/`](docs/README.md):
- [Локальный запуск и разработка на Mac](docs/LOCAL_DEVELOPMENT.md) (Live-reload без Docker и без коммитов в GitHub).
- [Настройка нескольких фотобиблиотек (папок)](docs/MULTIPLE_LIBRARIES.md) (в Docker и через веб-интерфейс).
- [Недавние оптимизации](docs/RECENT_CHANGES.md) (исправление зависания превью при лимите памяти 512 МБ и др.).

## Быстрый старт (Docker Compose)
```yaml
name: pixie-gallery
services:
  pixie-gallery:
    image: ghcr.io/henadzis/pixie-gallery:latest
    container_name: pixie-gallery
    restart: unless-stopped
    ports:
      - "8085:8000"
    volumes:
      # Кэш миниатюр, базы настроек и перекодированные видео
      - /DATA/AppData/pixie-gallery/cache:/cache
      # Временные файлы
      - /DATA/AppData/pixie-gallery/tmp:/tmp
      # Замените левую часть на фактический путь к вашей папке с фотографиями:
      - /DATA/Media/Photos:/photos:ro
      # Общий каталог медиа для подключения дополнительных библиотек через настройки (/media/...)
      - /DATA/Media:/media:ro
    environment:
      - PHOTOS_DIR=/photos
      - CACHE_DIR=/cache
      - TMPDIR=/tmp
      - GALLERY_TITLE=PORTFOLIO & ARCHIVE
      - ADMIN_USER=admin
      - ADMIN_PASS=admin123
      - SECRET_KEY=my-local-nas-secret-key-2026
      - DEFAULT_EXCLUDE_EXTS=.cr2,.cr3,.nef,.arw,.dng,.orf,.rw2,.pef,.raf,.tif,.tiff,.psd,.xmp
      - DEFAULT_EXCLUDE_MASKS=*_small.*, *_smal.*, *_thumb.*, ._*
    x-casaos:
      author: henadzis
      category: Gallery
      hostname: ""
      icon: https://raw.githubusercontent.com/henadzis/pixie-gallery/main/pixie_gallery.svg
      index: /
      port_map: "8085"
      scheme: http
      title:
        en_us: Pixie Gallery
        ru_ru: Pixie Галерея
