FROM python:3.11-slim

# Системные зависимости для Tesseract
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-ita \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
CMD ["python", "bot.py"]
