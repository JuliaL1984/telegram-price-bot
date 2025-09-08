import os
import re
import math
import time
import asyncio
import logging
from typing import Dict, Optional, List

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, ContentType, InputMediaPhoto

# ----------------- НАСТРОЙКИ -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# ГРУППА/КАНАЛ для публикации результата
TARGET_CHAT = int(os.getenv("TARGET_CHAT", "-1002973176038"))

# Сколько ждать текст после фото (строгая сцепка, чтобы не перемешивалось)
WAIT_SECS = int(os.getenv("WAIT_SECS", "10"))  # 5–10 сек можно ставить

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fashion-shop-bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Формулы по чатам (в памяти) и дефолт
FORMULAS: Dict[int, str] = {}
DEFAULT_FORMULA = "-%+10%+30€"

# Чтобы отвечать один раз на альбом (media group) и строго склеивать
# Буфер последнего медиа по чату:
# { chat_id: {"ts": float, "group": Optional[str], "files": [file_id,...], "task": asyncio.Task | None} }
LAST_MEDIA: Dict[int, Dict] = {}

# ----------------- РЕГЭКСЫ -----------------
PRICE_PAT = re.compile(
    r'(?s)(?:^|\n|\s)(?:цена[:\s]*)?'
    r'(?P<price>\d[\d\s.,]*)\s*(?:€|eur|euro)?'
    r'\s*[,.\s]*[-–]?\s*(?P<disc>\d+(?:[.,]\d+)?)\s*%',
    re.IGNORECASE
)
SIZES_PAT = re.compile(r'размер(?:ы)?[:\s]+(?P<sizes>[A-Za-zА-Яа-я0-9/ ,.\-]+)', re.IGNORECASE)
HASHTAG_PAT = re.compile(r'(^|\s)#\w+', re.UNICODE)

BRAND_WORDS = [
    'gucci','prada','valentino','balenciaga','jimmy choo','bally','stone island',
    'chopard','hermès','hermes','dior','lv','louis vuitton','versace','celine',
    'burberry','miumiu','bottega veneta','loewe','ysl','saint laurent','fendi'
]
BAN_WORDS = BRAND_WORDS + ['мужское','женское','в наличии в бутике']

STEP_PAT = re.compile(r'(?P<op>[+\-])\s*(?P<val>\d+(?:[.,]\d+)?)\s*(?P<unit>%|€)', re.IGNORECASE)

# ----------------- УТИЛИТЫ (цены/парсинг) -----------------
def to_float(num_str: str) -> float:
    s = num_str.replace('\u00A0','').replace(' ', '').replace(',', '.')
    return float(s)

def fmt_eur(x: float) -> str:
    return f"{math.ceil(x)}€"  # округление вверх, без евроцентов

def parse_post(text: str):
    m = PRICE_PAT.search(text)
    if not m:
        return None

    price = to_float(m.group('price'))
    disc = float(m.group('disc').replace(',', '.'))
    after_sale = price * (1 - disc/100.0)

    # Явные размеры "Размер ..."
    sizes: Optional[str] = None
    ms = SIZES_PAT.search(text)
    if ms:
        sizes = ms.group('sizes').strip()

    extra: List[str] = []
    fallback_sizes: List[str] = []
    for line in (ln.strip() for ln in text.splitlines() if ln.strip()):
        low = line.lower()

        if PRICE_PAT.search(line):
            continue
        if any(w in low for w in BAN_WORDS):
            continue
        if HASHTAG_PAT.search(low):
            continue
        if low.startswith('размер'):
            continue

        # Фоллбэк размеров: "40. 42", "38 40 42", "S/M", "Xs"
        if sizes is None and re.fullmatch(r'[A-Za-zА-Яа-я0-9/.\s]{1,20}', line) and any(ch.isdigit() or ch.isalpha() for ch in line):
            cleaned = re.sub(r'[,\.\s]+', '/', line).strip('/ ')
            if re.fullmatch(r'(?:[0-9]{1,2}|[XSMLxl]{1,3})(?:/(?:[0-9]{1,2}|[XSMLxl]{1,3}))*', cleaned, re.I):
                fallback_sizes.append(cleaned.upper())
                continue

        extra.append(line)

    if sizes is None and fallback_sizes:
        uniq = []
        for s in "/".join(fallback_sizes).split('/'):
            u = s.strip().upper()
            if u and u not in uniq:
                uniq.append(u)
        sizes = "/".join(uniq)

    return {
        "price": price,
        "discount": disc,
        "after_sale": after_sale,
        "sizes": sizes,
        "extra": extra,
    }

def apply_formula(base_after_sale: float, formula: str) -> float:
    f = formula.replace(' ', '').replace('-%', '')
    price = base_after_sale
    for m in STEP_PAT.finditer(f):
        op = m.group('op')
        val = float(m.group('val').replace(',', '.'))
        unit = m.group('unit')
        if unit == '%':
            price = price * (1 + val/100.0) if op == '+' else price * (1 - val/100.0)
        else:  # €
            price = price + val if op == '+' else price - val
    return price

def get_formula(chat_id: int) -> str:
    return FORMULAS.get(chat_id, DEFAULT_FORMULA)

def choose_formula_for_price(after_sale_eur: float, user_formula: str) -> str:
    """
    Пороговые правила:
      - цена-% ≤ 250 → -%+55€
      - 251–400 → -%+70€
      - > 400 → user_formula (по умолчанию -%+10%+30€)
    """
    if after_sale_eur <= 250:
        return "-%+55€"
    elif 251 <= after_sale_eur <= 400:
        return "-%+70€"
    else:
        return user_formula

def build_caption_from_text(text: str) -> Optional[str]:
    """Строим оформленную подпись из исходного текста. Если не распознали — None."""
    parsed = parse_post(text)
    if not parsed:
        return None
    user_formula = DEFAULT_FORMULA  # можно взять по чату, если надо: get_formula(chat_id)
    formula = choose_formula_for_price(parsed["after_sale"], user_formula)
    final_price = apply_formula(parsed["after_sale"], formula)

    lines = [
        f"<b>{fmt_eur(final_price)} ✅</b>",
        f"<b>Retail price {fmt_eur(parsed['price'])} ❌</b>",
    ]
    if parsed.get("sizes"):
        lines.append(f"<b>Размеры: {parsed['sizes']}</b>")
    for line in parsed.get("extra", []):
        lines.append(f"<b>{line}</b>")
    return "\n".join(lines)

# ----------------- КОМАНДЫ -----------------
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    await msg.answer(
        "Бот онлайн.\n"
        "• Пороговые правила: ≤250 → -%+55€; 251–400 → -%+70€; иначе — -%+10%+30€.\n"
        "• Шли фото с подписью или фото→затем текст (в течение ~10 сек).\n"
        "• Поддерживаются форматы: 5200€.-35%, 5010-35%, Цена 5010€ -35%.",
        parse_mode=ParseMode.HTML
    )

@dp.message(Command("formula"))
async def cmd_formula(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) == 1:
        return await msg.answer(
            "Формула по умолчанию: <code>-%%+10%%+30€</code> (для цен >400€).\n"
            "Пороговые: ≤250 → -%+55€; 251–400 → -%+70€.\n"
            "Можно задать свою для >400€: <code>/formula -%+12%+25€</code>",
            parse_mode=ParseMode.HTML
        )
    formula = parts[1].strip()
    if not ('%' in formula or '€' in formula):
        return await msg.answer("Формула должна содержать % или €.", parse_mode=ParseMode.HTML)
    FORMULAS[msg.chat.id] = formula
    await msg.answer(f"Формула для >400€ сохранена: <b>{formula}</b>", parse_mode=ParseMode.HTML)

# ----------------- ХЭНДЛЕРЫ: фото/альбомы/текст -----------------
@dp.message(F.content_type == ContentType.PHOTO)
async def handle_photo(msg: Message):
    """Фото/альбом: если есть подпись — публикуем сразу. Иначе ждём текст (строгая сцепка)."""
    chat_id = msg.chat.id
    file_id = msg.photo[-1].file_id
    now = time.time()

    # Сбросить прежний буфер и таймер (чтобы не перемешивалось)
    buf = LAST_MEDIA.get(chat_id)
    if buf and buf.get("task"):
        buf["task"].cancel()

    if msg.media_group_id:
        # Альбом
        group = msg.media_group_id
        buf = LAST_MEDIA.get(chat_id)
        if not buf or buf.get("group") != group:
            # Новый альбом — начинаем буфер
            LAST_MEDIA[chat_id] = {"ts": now, "group": group, "files": [file_id], "task": None}
        else:
            # Тот же альбом — дописываем
            buf["files"].append(file_id)
            buf["ts"] = now
    else:
        # Одиночное фото
        LAST_MEDIA[chat_id] = {"ts": now, "group": None, "files": [file_id], "task": None}

    # Если подпись есть — сразу публикуем и чистим буфер
    if msg.caption:
        caption = build_caption_from_text(msg.caption) or msg.caption
        files = LAST_MEDIA[chat_id]["files"]
        await _publish_media(files, caption)
        LAST_MEDIA.pop(chat_id, None)
        return

    # Иначе — ждём текст WAIT_SECS и публикуем без подписи, если не пришёл
    task = asyncio.create_task(_wait_and_publish_without_caption(chat_id))
    LAST_MEDIA[chat_id]["task"] = task

async def _wait_and_publish_without_caption(chat_id: int):
    try:
        await asyncio.sleep(WAIT_SECS)
        buf = LAST_MEDIA.get(chat_id)
        if not buf:
            return
        files = buf.get("files") or []
        if files:
            await _publish_media(files, caption=None)
    except asyncio.CancelledError:
        pass
    finally:
        LAST_MEDIA.pop(chat_id, None)

async def _publish_media(files: List[str], caption: Optional[str]):
    """Публикация в TARGET_CHAT: одиночное фото или альбом. Подпись — на первом фото."""
    if not files:
        return
    if len(files) == 1:
        await bot.send_photo(TARGET_CHAT, photo=files[0], caption=caption, parse_mode=ParseMode.HTML)
    else:
        media = []
        for i, fid in enumerate(files):
            if i == 0 and caption:
                media.append(InputMediaPhoto(media=fid, caption=caption, parse_mode=ParseMode.HTML))
            else:
                media.append(InputMediaPhoto(media=fid))
        await bot.send_media_group(TARGET_CHAT, media=media)

@dp.message(F.content_type == ContentType.TEXT)
async def handle_text(msg: Message):
    """Текст: если есть свежий буфер медиа — прикрепляем как подпись к нему; иначе публикуем текстом."""
    chat_id = msg.chat.id
    text = (msg.text or "").strip()
    if not text:
        return

    buf = LAST_MEDIA.get(chat_id)
    if buf:
        # Отменяем таймер «публикации без подписи»
        if buf.get("task"):
            buf["task"].cancel()

        files = buf.get("files") or []
        caption = build_caption_from_text(text) or text
        await _publish_media(files, caption=caption)
        LAST_MEDIA.pop(chat_id, None)
    else:
        # Нет медиа в ожидании — просто сообщение
        caption = build_caption_from_text(text)
        if caption:
            await bot.send_message(TARGET_CHAT, caption, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(TARGET_CHAT, text)

# ----------------- ЗАПУСК -----------------
async def main():
    log.info("Bot is starting…")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
