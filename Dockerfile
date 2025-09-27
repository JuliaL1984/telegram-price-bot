FROM python:3.11-slim

# базовые настройки Python
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OCR_ENABLED=0

WORKDIR /app

# сначала зависимости — для кэширования слоёв
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# затем код
COPY . .

# команда запуска для Render (Background Worker)
CMD ["python", "-u", "bot.py"]
