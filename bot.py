# bot.py
import os
import re
import math
import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InputMediaPhoto
# from aiogram.exceptions import RetryAfter   # ✗ удалено: в aiogram 3.7 этого класса нет

# === НАСТРОЙКИ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")   # берём токен из Render → Environment
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1002973176038"))  # ID группы
WAIT_TEXT_SECONDS = 8  # окно ожидания текста после фото

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# Память для склейки "фото → подпись"
last_media: dict[int, dict] = {}

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def round_price(value: float) -> int:
    return int(round(value, 0))

def apply_formula(price: float, discount: int) -> int:
    """
    Считаем по правилам:
    - Если до 250 → цена -% + 55€
    - Если 251–400 → цена -% + 70€
    - Если выше 400 → цена -% + 90€
    """
    discounted = price * (1 - discount / 100)
    if discounted <= 250:
        return round_price(discounted + 55)
    elif discounted <= 400:
        return round_price(discounted + 70)
    else:
        return round_price(discounted + 90)

def format_message(price: float, discount: int, retail: float, sizes: str, season: str) -> str:
    final_price = apply_formula(price, discount)
    return (
        f"✅ <b>{final_price}€</b>\n"
        f"❌ <b>Retail price {round_price(retail)}€</b>\n"
        f"{sizes}\n"
        f"{season}"
    )

# === ХЕНДЛЕРЫ ===
@dp.message(F.photo)
async def handle_photo(msg: Message):
    last_media[msg.chat.id] = {
        "ts": datetime.now(),
        "file_ids": [msg.photo[-1].file_id],
        "caption": msg.caption or "",
    }

@dp.message(F.text)
async def handle_text(msg: Message):
    chat_id = msg.chat.id
    if chat_id not in last_media:
        return

    # Проверяем окно времени
    if datetime.now() - last_media[chat_id]["ts"] > timedelta(seconds=WAIT_TEXT_SECONDS):
        return

    text = msg.text.strip()

    # Ищем данные в тексте
    price_match = re.search(r"(\d+)[€]", text)
    discount_match = re.search(r"-(\d+)%", text)
    retail_match = re.search(r"Retail price (\d+)", text, re.IGNORECASE)
    sizes_match = re.search(r"(S\/M\/L.*|XS.*|XL.*)", text)
    season_match = re.search(r"(FW\d+\/\d+|SS\d+)", text)

    if price_match and discount_match:
        price = float(price_match.group(1))
        discount = int(discount_match.group(1))
        retail = float(retail_match.group(1)) if retail_match else price
        sizes = sizes_match.group(1) if sizes_match else ""
        season = season_match.group(1) if season_match else ""

        result = format_message(price, discount, retail, sizes, season)

        # Сообщение с результатом
        await bot.send_message(TARGET_CHAT_ID, result)

        # Отправляем фото с подписью (НЕ media_group, т.к. фото всего одно)
        if last_media[chat_id]["file_ids"]:
            await bot.send_photo(
                TARGET_CHAT_ID,
                last_media[chat_id]["file_ids"][0],
                caption=result
            )

    # Чистим память
    del last_media[chat_id]

# === ЗАПУСК ===
async def main():
    # Без RetryAfter: aiogram 3.7 его не отдаёт; Render сам перезапустит при падении
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
