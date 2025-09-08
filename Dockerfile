# Используем официальный Python образ
FROM python:3.11-slim

# Делаем рабочую папку внутри контейнера
WORKDIR /app

# Копируем список зависимостей
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект (включая bot.py)
COPY . .

# Запуск бота
CMD ["python", "bot.py"]
