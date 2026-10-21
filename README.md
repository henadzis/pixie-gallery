# Pixie Gallery

Легковесная веб-галерея для просмотра локального фотоархива и клиентского отбора фотографий с поддержкой многопользовательского доступа.

## Возможности
- Двухуровневый кэш превью (400px для плитки, 1200px для лайтбокса).
- Умный Zoom с панорамированием оригинального кадра.
- Извлечение и кэширование EXIF-данных (камера, объектив, выдержка, ISO, разрешение).
- Скачивание альбомов и избранных кадров в ZIP.
- Разграничение доступа к папкам для клиентов и администратора.
- Двуязычный интерфейс (RU / EN).

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
      # Замените левую часть на фактический путь к вашей папке с фотографиями:
      - /DATA/Media/Photos:/photos:ro
      - /DATA/AppData/pixie-gallery/cache:/cache
    environment:
      - PHOTOS_DIR=/photos
      - CACHE_DIR=/cache
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
      icon: https://cdn-icons-png.flaticon.com/512/3342/3342137.png
      index: /
      port_map: "8085"
      scheme: http
      title:
        en_us: Pixie Gallery
        ru_ru: Pixie Галерея
