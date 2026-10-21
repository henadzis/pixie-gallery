FROM python:3.12-slim

WORKDIR /code

# Установка зависимостей в образ
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Рабочая директория для монтирования кода
ENV PYTHONPATH=/code

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]