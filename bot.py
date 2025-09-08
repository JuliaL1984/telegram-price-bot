import asyncio
import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.middlewares.request_logging import logger
from aiogram.exceptions import TelegramRetryAfter, TelegramMigrateToChat
from aiogram.types import Message, InputMediaPhoto

# === НАСТРОЙКИ ===
BOT_TOKEN = "8409460554:AAGIRPd4hD8dvCVbM1BGVU4JWkGTXTsn9gE"
TARGET_CHAT_ID = -1002973176038           # Сюда бот будет публиковать результат
WAIT_TEXT_SECONDS = 8                     # Ждём текст к фото (5–10 сек)
PAUSE_BETWEEN_SENDS = 2.0                 # Пауза между отправками для анти-флуда

# === ВСПОМОГАТЕЛЬНЫЕ ===
price_re = re.compile(r"(\d[\d\s]*)\s*€?", re.IGNORECASE)
disc_re  = re.compile(r"-\s*(\d{1,2})\s*%")
sizes_re = re.compile(r"\b(\d{2}(?:[./]\d{2})*|XS|S|M|L|XL|XXL)\b", re.IGNORECASE)

REMOVE_PHRASES = [
    "в наличии в бутике",
]
REMOVE_HASHTAG_LINE = re.compile(r"^\s*#.+$", re.IGNORECASE)

def tidy(text: str) -> str:
    """Чистим лишнее: хэштеги, бренды-строки, служебные фразы."""
    lines = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if REMOVE_HASHTAG_LINE.match(s):
            continue
        if any(p in s.lower() for p in REMOVE_PHRASES):
            continue
        lines.append(s)
    return "\n".join(lines)

def parse_price_and_discount(text: str) -> Optional[tuple]:
    text = text.replace(" ", " ").replace("\xa0", " ")
    # Берём первую цену как розничную
    m_price = price_re.search(text)
    m_disc  = disc_re.search(text)
    if not m_price or not m_disc:
        return None
    retail = int(m_price.group(1).replace(" ", ""))
    discount = int(m_disc.group(1))
    return retail, discount

def parse_sizes(text: str) -> Optional[str]:
    # собираем все в одну строку по порядку
    found = sizes_re.findall(text)
    if not found:
        return None
    # нормализуем разделители
    s = " / ".join([f.replace(".", "/") for f in found])
    return s

def calc_final_price(retail: int, discount_percent: int) -> int:
    """Округление вверх до целого евро без центов."""
    discounted = retail * (100 - discount_percent) / 100.0
    if discounted <= 250:
        final = discounted + 55
    elif discounted <= 400:
        final = discounted + 70
    else:
        final = discounted * 1.10 + 30
    return math.ceil(final)

def build_caption(text: str) -> Optional[str]:
    parsed = parse_price_and_discount(text)
    if not parsed:
        return None
    retail, disc = parsed
    final_price = calc_final_price(retail, disc)
    sizes = parse_sizes(text)

    retail_str = f"{retail}€"
    final_str = f"{final_price}€"

    cap_lines = [
        f"**{final_str}** ✅",
        f"Retail price **{retail_str}** ❌",
    ]
    if sizes:
        cap_lines.append(f"Размеры: **{sizes}**")
    return "\n".join(cap_lines)

# === Буферы на склейку фото+текста ===
@dataclass
class Pending:
    photos: List[str] = field(default_factory=list)  # file_id
    text: Optional[str] = None
    timer: Optional[asyncio.Task] = None

buffers: Dict[int, Pending] = {}
send_lock = asyncio.Lock()   # последовательная отправка (анти-миксовка)

async def safe_send(bot: Bot, coro_func, *args, **kwargs):
    """Отправка с анти-флуд ретраями и паузами."""
    while True:
        try:
            res = await coro_func(*args, **kwargs)
            await asyncio.sleep(PAUSE_BETWEEN_SENDS)
            return res
        except TelegramRetryAfter as e:
            logger.warning(f"Flood control: waiting {e.retry_after}s")
            await asyncio.sleep(e.retry_after + 1)
        except TelegramMigrateToChat as e:
            # если вдруг канал переехал, обновим
            global TARGET_CHAT_ID
            TARGET_CHAT_ID = e.migrate_to_chat_id
        except Exception as e:
            logger.exception(f"Send error: {e}")
            # небольшая пауза и ещё попытка
            await asyncio.sleep(2)

async def flush_chat(bot: Bot, chat_id: int):
    """Отправляем накопленное (если что-то есть) и очищаем буфер."""
    p = buffers.get(chat_id)
    if not p:
        return
    text_for_parse = tidy(p.text or "")
    caption = build_caption(text_for_parse) if text_for_parse else None

    async with send_lock:
        if p.photos:
            # если несколько фото -> альбом, подпись только у первого
            if len(p.photos) == 1:
                await safe_send(bot, bot.send_photo,
                                chat_id=TARGET_CHAT_ID,
                                photo=p.photos[0],
                                caption=caption or None,
                                parse_mode="Markdown")
            else:
                media = []
                for idx, file_id in enumerate(p.photos):
                    if idx == 0 and caption:
                        media.append(InputMediaPhoto(type="photo", media=file_id,
                                                     caption=caption, parse_mode="Markdown"))
                    else:
                        media.append(InputMediaPhoto(type="photo", media=file_id))
                await safe_send(bot, bot.send_media_group,
                                chat_id=TARGET_CHAT_ID, media=media)
        elif caption:
            await safe_send(bot, bot.send_message,
                            chat_id=TARGET_CHAT_ID,
                            text=caption,
                            parse_mode="Markdown")

    # очищаем
    if p.timer:
        p.timer.cancel()
    buffers.pop(chat_id, None)

def ensure_buffer(chat_id: int) -> Pending:
    if chat_id not in buffers:
        buffers[chat_id] = Pending()
    return buffers[chat_id]

async def start_timer(bot: Bot, chat_id: int):
    """Стартуем/перезапускаем таймер ожидания подписи/фото."""
    p = ensure_buffer(chat_id)
    if p.timer:
        p.timer.cancel()

    async def _task():
        await asyncio.sleep(WAIT_TEXT_SECONDS)
        await flush_chat(bot, chat_id)

    p.timer = asyncio.create_task(_task())

# === Aiogram ===
bot = Bot(BOT_TOKEN, parse_mode="Markdown")
dp = Dispatcher()

@dp.message(F.text == "/chatid")
async def cmd_chatid(msg: Message):
    await msg.answer(f"Chat ID: `{msg.chat.id}`", parse_mode="Markdown")

@dp.message(F.photo)
async def on_photo(msg: Message):
    p = ensure_buffer(msg.chat.id)
    # самое большое качество — последняя фотография в массиве
    file_id = msg.photo[-1].file_id
    p.photos.append(file_id)
    # если текст пришёл раньше и уже лежит в буфере — просто ждём таймер
    await start_timer(bot, msg.chat.id)

@dp.message(F.text)
async def on_text(msg: Message):
    txt = msg.text or ""
    # /formula больше не требуется — считаем всегда, если распарсили цену и %
    p = ensure_buffer(msg.chat.id)
    p.text = txt
    await start_timer(bot, msg.chat.id)

async def main():
    logger.info("Bot starting…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
