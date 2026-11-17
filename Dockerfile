FROM python:3.12-slim

WORKDIR /code

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код в образ
COPY . /code

ENV PYTHONPATH=/code
ENV TMPDIR=/tmp
ARG APP_VERSION=v0.9.1
ENV APP_VERSION=$APP_VERSION

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
