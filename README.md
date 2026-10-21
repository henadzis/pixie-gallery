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
services:
  pixie-gallery:
    image: python:3.12-slim
    container_name: pixie-gallery
    restart: unless-stopped
    working_dir: /code
    command: ["sh", "/code/app/start.sh"]
    ports:
      - "8085:8000"
    volumes:
      - ./app:/code/app
      - /path/to/photos:/photos:ro
      - ./cache:/cache
