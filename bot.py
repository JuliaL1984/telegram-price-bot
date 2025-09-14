# bot.py — альбомы, 30+ режимов, OCR ценника, дебаунс 1.5с для альбомов,
# подсказки при отсутствии цены и поддержка "альбом/фото/видео → общий текст"
# + Гарантия порядка публикаций через FIFO-очередь
# Версия с 5-строчной подписью, точным парсингом размеров/сезона,
# и двумя режимами: /lux (OCR off) и /luxocr (OCR on), одинаковая формула.
# Округление всегда вверх; бренд из подписи удалён.

import os
import re
import io
import math
import asyncio
from typing import Dict, Callable, Optional, List, Tuple, Any
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, InputMediaPhoto, InputMediaVideo
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command

# ====== НАСТРОЙКИ ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1002973176038"))
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

ALBUM_SETTLE_MS = int(os.getenv("ALBUM_SETTLE_MS", "1500"))  # стабильнее собирает альбомы
ALBUM_WINDOW_SECONDS = int(os.getenv("ALBUM_WINDOW_SECONDS", "30"))

OCR_ENABLED = os.getenv("OCR_ENABLED", "1") == "1"
OCR_LANG = os.getenv("OCR_LANG", "ita+eng")
FILTER_PRICETAGS_IN_ALBUMS = os.getenv("FILTER_PRICETAGS_IN_ALBUMS", "1") == "1"

# ====== ИНИЦИАЛИЗАЦИЯ ======
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ====== ПАМЯТЬ ======
last_media: Dict[int, Dict[str, Any]] = {}
active_mode: Dict[int, str] = {}
album_buffers: Dict[Tuple[int, str], Dict[str, Any]] = {}
publish_queue: "asyncio.Queue[Tuple[int, List[Dict[str, Any]], str, bool]]" = asyncio.Queue()

def is_ocr_enabled_for(user_id: int) -> bool:
    mode = active_mode.get(user_id, "sale")
    if mode == "lux":
        return False
    if mode == "luxocr":
        return True
    return FILTER_PRICETAGS_IN_ALBUMS

async def _do_publish(user_id: int, items: List[Dict[str, Any]], caption: str, album_ocr_on: bool):
    if not items:
        return
    items = await filter_pricetag_media(items, album_ocr_on)
    if len(items) == 1:
        it = items[0]
        if it["kind"] == "video":
            await bot.send_video(TARGET_CHAT_ID, it["fid"], caption=caption)
        else:
            await bot.send_photo(TARGET_CHAT_ID, it["fid"], caption=caption)
        return
    first = items[0]
    media = []
    if first["kind"] == "video":
        media.append(InputMediaVideo(media=first["fid"], caption=caption, parse_mode=ParseMode.HTML))
    else:
        media.append(InputMediaPhoto(media=first["fid"], caption=caption, parse_mode=ParseMode.HTML))
    for it in items[1:]:
        if it["kind"] == "video":
            media.append(InputMediaVideo(media=it["fid"]))
        else:
            media.append(InputMediaPhoto(media=it["fid"]))
    await bot.send_media_group(TARGET_CHAT_ID, media)

async def publish_worker():
    while True:
        user_id, items, caption, album_ocr_on = await publish_queue.get()
        try:
            await _do_publish(user_id, items, caption, album_ocr_on)
        except Exception:
            pass
        finally:
            publish_queue.task_done()

async def publish_to_target(user_id: int, items: List[Dict[str, Any]], caption: str):
    album_ocr_on = is_ocr_enabled_for(user_id)
    await publish_queue.put((user_id, items, caption, album_ocr_on))

# ====== OCR ======
if OCR_ENABLED:
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        OCR_ENABLED = False

def _price_token_regex() -> str:
    return r"(?:€\s*\d{2,3}(?:[.,]\d{3})*|\d{2,3}(?:[.,]\d{3})*\s*€)"

async def ocr_should_hide(file_id: str) -> bool:
    if not OCR_ENABLED:
        return False
    try:
        file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download(file, buf)
        buf.seek(0)
        from PIL import Image
        import pytesseract
        img = Image.open(buf)
        txt = pytesseract.image_to_string(img, lang=OCR_LANG) or ""
        tl = txt.lower()
        has_price_token = bool(re.search(_price_token_regex(), txt))
        has_kw = ("retail" in tl) or ("price" in tl) or ("prezzo" in tl) or ("%" in txt)
        return bool(has_price_token and has_kw)
    except Exception:
        return False

async def filter_pricetag_media(items: List[Dict[str, Any]], album_ocr_on: bool) -> List[Dict[str, Any]]:
    if len(items) == 1 or not album_ocr_on:
        return items
    kept: List[Dict[str, Any]] = []
    for it in items:
        if it["kind"] == "photo":
            if not await ocr_should_hide(it["fid"]):
                kept.append(it)
        else:
            kept.append(it)
    return kept or items[:1]

# ====== КАЛЬКУЛЯТОРЫ ======
def ceil_price(value: float) -> int:
    return int(math.ceil(value))

def default_calc(price: float, discount: int) -> int:
    discounted = price * (1 - discount / 100)
    if discounted <= 250:
        return ceil_price(discounted + 55)
    elif discounted <= 400:
        return ceil_price(discounted + 70)
    else:
        return ceil_price(discounted + 90)

def lux_calc(price: float, discount: int) -> int:
    discounted = price * (1 - discount / 100)
    if discounted <= 250:
        final = discounted + 55
    elif discounted <= 400:
        final = discounted + 70
    else:
        final = discounted * 1.10 + 30
    return ceil_price(final)

# ====== РАЗБОР И ФОРМИРОВАНИЕ 5-СТРОЧНОЙ ПОДПИСИ ======
def cleanup_text_basic(text: str) -> str:
    text = re.sub(r"#\S+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

SIZE_TOKEN = r"(?:XXS|XS|S|M|L|XL|XXL|[2-5]\d)"

def _strip_seasons_for_size_scan(text: str) -> str:
    return re.sub(r"\b(?:NEW\s+)?(?:FW|SS)\d+(?:/\d+)?\b", " ", text, flags=re.I)

def _strip_discounts_and_prices(text: str) -> str:
    text = re.sub(r"-\s?\d{1,2}\s?%", " ", text)
    text = re.sub(_price_token_regex(), " ", text)
    return text

def extract_sizes_anywhere(text: str) -> str:
    work = _strip_seasons_for_size_scan(text)
    work = _strip_discounts_and_prices(work)
    ranges = re.findall(r"\b([2-5]\d)\s*[-–—]\s*([2-5]\d)\b", work)
    singles_num = re.findall(r"\b([2-5]\d)\b", work)
    singles_alpha = re.findall(r"\b(XXS|XS|S|M|L|XL|XXL)\b", work, flags=re.I)
    parts: List[str] = []
    used = set()
    for a, b in ranges:
        token = f"{a}-{b}"
        if token not in used:
            parts.append(token); used.add(token)
    for t in singles_alpha:
        token = t.upper()
        if token not in used:
            parts.append(token); used.add(token)
    covered_nums = set()
    for a, b in ranges:
        lo, hi = min(int(a), int(b)), max(int(a), int(b))
        covered_nums.update(str(x) for x in range(lo, hi + 1))
    for t in singles_num:
        if t in covered_nums or t in used:
            continue
        parts.append(t); used.add(t)
    return ", ".join(parts)

def pick_sizes_line(lines: List[str]) -> str:
    for line in lines:
        l = line.strip()
        if not l:
            continue
        if re.search(r"(€|%|\bretail\b|\bprice\b)", l, flags=re.I):
            continue
        if re.search(fr"\b{SIZE_TOKEN}\b", l, flags=re.I):
            return l
        if re.search(r"\b[2-5]\d\b(?:\s*\([^)]+\))?(?:\s*,\s*\b[2-5]\d\b(?:\s*\([^)]+\))?)*", l):
            return l
    return ""

def pick_season_line(lines: List[str]) -> str:
    for line in lines:
        if re.search(r"\bNEW\s+(?:FW|SS)\d+(?:/\d+)?\b", line, flags=re.I):
            return line.strip()
    for line in lines:
        if re.search(r"\b(?:FW|SS)\d+(?:/\d+)?\b", line, flags=re.I):
            return line.strip()
    return ""

def parse_number_token(token: Optional[str]) -> Optional[float]:
    if not token:
        return None
    return float(token.replace('.', '').replace(',', ''))

def parse_input(raw_text: str) -> Dict[str, Optional[str]]:
    text = cleanup_text_basic(raw_text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    price_m = re.search(r"(\d+(?:[.,]\d{3})*)\s*€", text)
    discount_m = re.search(r"-(\d+)%", text)
    retail_m = re.search(r"Retail\s*price\s*(\d+(?:[.,]\d{3})*)", text, flags=re.I)
    price = parse_number_token(price_m.group(1)) if price_m else None
    discount = int(discount_m.group(1)) if discount_m else 0
    retail = parse_number_token(retail_m.group(1)) if retail_m else (price if price is not None else 0.0)
    sizes_line = pick_sizes_line(lines) or extract_sizes_anywhere(text)
    season_line = pick_season_line(lines)
    return {
        "price": price,
        "discount": discount,
        "retail": retail,
        "sizes_line": sizes_line,
        "season_line": season_line,
        "brand_line": "",  # бренд всегда пустой
        "cleaned_text": text,
    }

def template_five_lines(final_price: int, retail: float, sizes_line: str, season_line: str, brand_line: str) -> str:
    line1 = f"✅ <b>{ceil_price(final_price)}€</b>"
    line2 = f"❌ <b>Retail price {ceil_price(retail)}€</b>"
    line3 = sizes_line or ""
    line4 = season_line or ""
    line5 = ""  # бренд убран
    lines = [line1, line2, line3, line4, line5]
    cleaned = []
    for s in lines:
        if cleaned and s and s == cleaned[-1]:
            continue
        cleaned.append(s)
    while len(cleaned) < 5:
        cleaned.append("")
    return "\n".join(cleaned[:5])

def mk_mode(label: str,
            calc: Callable[[float, int], int] = default_calc,
            template: Callable[[int, float, str, str, str], str] = template_five_lines):
    return {"label": label, "calc": calc, "template": template}

# ====== РЕЖИМЫ ======
MODES: Dict[str, Dict] = {
    "sale": mk_mode("SALE"),
    "lux": mk_mode("LUX", calc=lux_calc),          # OCR off
    "luxocr": mk_mode("LUX OCR", calc=lux_calc),   # OCR on
    # ... остальные как есть
}

def is_admin(user_id: int) -> bool:
    return (not ADMINS) or (user_id in ADMINS)

# ====== КОМАНДЫ ======
@router.message(Command(commands=list(MODES.keys())))
async def set_mode(msg: Message):
    user_id = msg.from_user.id
    cmd = msg.text.lstrip("/").split()[0]
    if not is_admin(user_id):
        return await msg.answer("⛔ Только для админов.")
    active_mode[user_id] = cmd
    await msg.answer(f"✅ Режим <b>{MODES[cmd]['label']}</b> активирован.")

@router.message(Command("help"))
async def show_help(msg: Message):
    await msg.answer(
        "Бот принимает фото/видео (альбомы) и текст с ценой.\n"
        "• /lux — OCR выключен, /luxocr — OCR включен.\n"
        "• Формула: ≤250€ +55€; 251–400€ +70€; >400€ → +10% и +30€. Всё округляем вверх."
    )

@router.message(Command("ping"))
async def ping(msg: Message):
    await msg.answer("pong")

# ====== СБОРКА ПОДПИСИ ======
def build_result_text(user_id: int, caption: str) -> Optional[str]:
    data = parse_input(caption)
    price = data.get("price")
    if price is None:
        return None
    mode = MODES.get(active_mode.get(user_id, "sale"), MODES["sale"])
    calc_fn, tpl_fn, _label = mode["calc"], mode["template"], mode["label"]
    final_price = calc_fn(float(price), int(data.get("discount", 0)))
    return tpl_fn(
        final_price=final_price,
        retail=float(data.get("retail", 0.0) or 0.0),
        sizes_line=data.get("sizes_line", "") or "",
        season_line=data.get("season_line", "") or "",
        brand_line="",
    )

# ====== ХЕНДЛЕРЫ (single, album, text) ======
# ... (остались без изменений, как у тебя)

# ====== ЗАПУСК ======
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(publish_worker())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
