# bot.py — альбомы, 30+ режимов, OCR ценника, дебаунс 1.5с для альбомов,
# подсказки при отсутствии цены и поддержка "альбом/фото/видео → общий текст"
# + Строгая гарантия порядка публикаций через GLOBAL SEQ-барьер
# Версия с 5-строчной подписью, точным парсингом размеров/сезона,
# и двумя режимами: /lux (OCR off) и /luxocr (OCR on), одинаковая формула.

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
from aiogram.exceptions import TelegramConflictError

# ====== НАСТРОЙКИ ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1002973176038"))
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

ALBUM_SETTLE_MS = int(os.getenv("ALBUM_SETTLE_MS", "1500"))
ALBUM_WINDOW_SECONDS = int(os.getenv("ALBUM_WINDOW_SECONDS", "30"))

OCR_ENABLED = os.getenv("OCR_ENABLED", "1") == "1"
OCR_LANG = os.getenv("OCR_LANG", "ita+eng")
_env_flag = os.getenv("FILTER_PRICETAGS_IN_ALBUMS")
FILTER_PRICETAGS_IN_ALBUMS = (_env_flag == "1") if _env_flag is not None else True

# ====== ИНИЦИАЛИЗАЦИЯ ======
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ====== ПАМЯТЬ ======
last_media: Dict[int, Dict[str, Any]] = {}
active_mode: Dict[int, str] = {}
album_buffers: Dict[Tuple[int, str], Dict[str, Any]] = {}

# ====== STRICT GLOBAL SEQ ======
_next_seq = 0
_next_to_publish = 1
_pending: Dict[int, Tuple[int, int, int, List[Dict[str, Any]], str, bool]] = {}

def alloc_seq() -> int:
    global _next_seq
    _next_seq += 1
    return _next_seq

publish_queue: "asyncio.Queue[Tuple[int, int, int, List[Dict[str, Any]], str, bool]]" = asyncio.Queue()

def is_ocr_enabled_for(user_id: int) -> bool:
    mode = active_mode.get(user_id, "lux")
    if mode == "lux":
        return False
    if mode == "luxocr":
        return True
    return FILTER_PRICETAGS_IN_ALBUMS

async def _do_publish(user_id: int, items: List[Dict[str, Any]], caption: str, album_ocr_on: bool):
    if not items:
        return
    # текстовый «альбом»
    if items and items[0].get("kind") == "text":
        await bot.send_message(TARGET_CHAT_ID, caption or "")
        return
    # фильтр ценников (OCR)
    items = await filter_pricetag_media(items, album_ocr_on)
    # одиночка
    if len(items) == 1:
        it = items[0]
        if it["kind"] == "video":
            await bot.send_video(TARGET_CHAT_ID, it["fid"], caption=caption)
        else:
            await bot.send_photo(TARGET_CHAT_ID, it["fid"], caption=caption)
        await asyncio.sleep(0.2)
        return
    # медиагруппа
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
    await asyncio.sleep(0.3)

async def publish_worker():
    global _next_to_publish
    while True:
        payload = None
        try:
            payload = await publish_queue.get()
            seq = payload[0]
            _pending[seq] = payload
            while _next_to_publish in _pending:
                s, first_mid, user_id, items, caption, album_ocr_on = _pending.pop(_next_to_publish)
                try:
                    await _do_publish(user_id, items, caption, album_ocr_on)
                except Exception as e:
                    print(f"Ошибка при публикации: {e}")
                finally:
                    _next_to_publish += 1
        except Exception as e:
            print(f"Ошибка в publish_worker: {e}")
        finally:
            if payload is not None:
                try:
                    publish_queue.task_done()
                except Exception:
                    pass

async def publish_to_target(seq: int, first_mid: int, user_id: int, items: List[Dict[str, Any]], caption: str):
    album_ocr_on = is_ocr_enabled_for(user_id)
    await publish_queue.put((seq, first_mid, user_id, list(items), caption, album_ocr_on))

# ====== OCR ======
if OCR_ENABLED:
    try:
        import pytesseract
        from PIL import Image  # noqa: F401
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
    except Exception as e:
        print(f"OCR fail (skip): {e}")
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
    return kept if kept else items[:1]

# ====== КАЛЬКУЛЯТОРЫ ======
def ceil_price(value: float) -> int:
    return int(math.ceil(value - 1e-9))

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

# ====== РАЗБОР И 5-СТРОЧНАЯ ПОДПИСЬ ======
def cleanup_text_basic(text: str) -> str:
    text = re.sub(r"#\S+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

SIZE_ALPHA   = r"(?:XXS|XS|S|M|L|XL|XXL)"
SIZE_NUM_EU  = r"(?:[3-5]\d|60)((?:[.,]5)?"
SIZE_NUM_US  = r"(?:[5-9]|1[0-2])(?:[.,]5)?"
SIZE_NUM_ANY = rf"(?:{SIZE_NUM_EU}|{SIZE_NUM_US})"

def _strip_seasons_for_size_scan(text: str) -> str:
    return re.sub(r"\b(?:NEW\s+)?(?:FW|SS)\d+(?:/\d+)?\b", " ", text, flags=re.I)

def _strip_discounts_and_prices(text: str) -> str:
    text = re.sub(r"[-−–—]?\s?\d{1,3}\s?%", " ", text)
    text = re.sub(_price_token_regex(), " ", text)
    return text

def extract_sizes_anywhere(text: str) -> str:
    work = _strip_seasons_for_size_scan(text)
    work = _strip_discounts_and_prices(work)
    ranges_dash  = re.findall(rf"(?<!\d)({SIZE_NUM_ANY})\s*[-–—]\s*({SIZE_NUM_ANY})(?!\d)", work)
    ranges_slash = re.findall(rf"(?<!\d)({SIZE_NUM_ANY})\s*/\s*({SIZE_NUM_ANY})(?!\d)", work)
    singles_num   = re.findall(rf"(?<!\d)({SIZE_NUM_ANY})(?!\d)", work)
    singles_alpha = re.findall(rf"\b(XXS|XS|S|M|L|XL|XXL)\b", work, flags=re.I)

    parts: List[str] = []
    used = set()
    def add(tok: str):
        tok = tok.replace(".5", ",5")
        if tok not in used:
            parts.append(tok); used.add(tok)

    for a, b in (ranges_dash + ranges_slash):
        add(f"{a.replace('.5', ',5')}-{b.replace('.5', ',5')}")
    for t in singles_alpha:
        add(t.upper())

    covered_nums = set()
    def _expand(n1: str, n2: str):
        try:
            a = float(n1.replace(",", "."))
            b = float(n2.replace(",", "."))
            lo, hi = sorted((a, b))
            x = lo
            while x <= hi + 1e-9:
                s = ("{:.1f}".format(x)).replace(".5", ",5").rstrip("0").rstrip(",")
                covered_nums.add(s); x += 0.5
        except Exception:
            pass
    for a, b in (ranges_dash + ranges_slash):
        _expand(a, b)

    for t in singles_num:
        norm = t.replace(".5", ",5")
        if norm not in covered_nums:
            add(norm)

    nums_only = [p for p in parts if re.fullmatch(r"\d+(?:,\d)?", p)]
    if not ranges_dash and not ranges_slash and not singles_alpha and len(nums_only) == 1:
        return ""
    return ", ".join(parts)

def pick_sizes_line(lines: List[str]) -> str:
    for line in lines:
        l = line.strip()
        if not l: continue
        if re.search(r"(€|%|\bretail\b|\bprice\b)", l, flags=re.I):
            continue
        if re.search(r"\b(XXS|XS|S|M|L|XL|XXL)\b", l, flags=re.I) or \
           re.search(rf"(?<!\d){SIZE_NUM_ANY}(?:\s*(?:[,/]\s*{SIZE_NUM_ANY}))+?(?!\d)", l):
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

MATERIAL_RE = re.compile(
    r"(?:(\d{1,3})\s*%\s*)?(?:"
    r"(silk|wool|cotton|cashmere|linen|leather|suede|denim|canvas|viscose|rayon|polyester|nylon|polyamide|acrylic|acetate|elastane|spandex|mohair|alpaca|angora|down|feather|goose\s+down|merino|rubber|lyocell|tencel)"
    r")",
    flags=re.I
)

def extract_materials_line(text: str) -> str:
    parts: List[str] = []; used = set()
    for m in MATERIAL_RE.finditer(text):
        pct, mat = m.group(1), m.group(2)
        mat = re.sub(r"\s+", " ", mat.strip()).lower()
        token = f"{pct}% {mat}" if pct else mat
        if token not in used:
            parts.append(token); used.add(token)
    return ", ".join(parts)

def parse_number_token(token: Optional[str]) -> Optional[float]:
    if not token: return None
    return float(token.replace('.', '').replace(',', ''))

def parse_input(raw_text: str) -> Dict[str, Optional[str]]:
    text = cleanup_text_basic(raw_text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    price_m    = re.search(r"(\d+(?:[.,]\d{3})*)\s*€", text)
    discount_m = re.search(r"[-−–—]\s*(\d{1,2})\s*%(?=\D|$)", text)
    retail_m   = re.search(r"Retail\s*price\s*(\d+(?:[.,]\d{3})*)", text, flags=re.I)
    price    = parse_number_token(price_m.group(1)) if price_m else None
    discount = int(discount_m.group(1)) if discount_m else 0
    retail   = parse_number_token(retail_m.group(1)) if retail_m else (price if price is not None else 0.0)
    sizes_line     = pick_sizes_line(lines) or extract_sizes_anywhere(text)
    season_line    = pick_season_line(lines)
    materials_line = extract_materials_line(text)
    return {
        "price": price, "discount": discount, "retail": retail,
        "sizes_line": sizes_line, "season_line": season_line,
        "materials_line": materials_line, "brand_line": "",
        "cleaned_text": text,
    }

def ceil_price(value: float) -> int:
    return int(math.ceil(value - 1e-9))

def template_five_lines(final_price: int, retail: float,
                        sizes_line: str, season_line: str,
                        brand_line: str, materials_line: str = "") -> str:
    line1 = f"✅ <b>{ceil_price(final_price)}€</b>"
    line2 = f"❌ <b>Retail price {ceil_price(retail)}€</b>"
    line3 = sizes_line or ""
    if materials_line:
        line4 = materials_line; line5 = season_line or ""
    else:
        line4 = season_line or ""; line5 = ""
    lines = [line1, line2, line3, line4, line5]
    cleaned = []
    for s in lines:
        if cleaned and s and s == cleaned[-1]: continue
        cleaned.append(s)
    while len(cleaned) < 5: cleaned.append("")
    return "\n".join(cleaned[:5])

def mk_mode(label: str,
            calc: Callable[[float, int], int] = default_calc,
            template: Callable[[int, float, str, str, str], str] = template_five_lines):
    return {"label": label, "calc": calc, "template": template}

MODES: Dict[str, Dict] = {
    "lux": mk_mode("LUX", calc=lux_calc),
    "luxocr": mk_mode("LUX OCR", calc=lux_calc),
    # остальные режимы опущены ради компактности — оставь как у тебя
}

def is_admin(user_id: int) -> bool:
    return (not ADMINS) or (user_id in ADMINS)

@router.message(Command(commands=list(MODES.keys())))
async def set_mode(msg: Message):
    user_id = msg.from_user.id
    cmd = msg.text.lstrip("/").split()[0]
    if not is_admin(user_id):
        return await msg.answer("⛔ Только для админов.")
    active_mode[user_id] = cmd
    await msg.answer(f"✅ Режим <b>{MODES[cmd]['label']}</b> активирован.")

@router.message(Command("mode"))
async def show_mode(msg: Message):
    user_id = msg.from_user.id
    mode_key = active_mode.get(user_id, "lux")
    label = MODES.get(mode_key, MODES["lux"])["label"]
    ocr_state = "ON" if is_ocr_enabled_for(user_id) else "OFF"
    await msg.answer(f"Текущий режим: <b>{label}</b>\nOCR в альбомах: <b>{ocr_state}</b>")

@router.message(Command("help"))
async def show_help(msg: Message):
    await msg.answer("Бот принимает фото/видео (альбомы) и текст с ценой...")

@router.message(Command("ping"))
async def ping(msg: Message):
    await msg.answer("pong")

def build_result_text(user_id: int, caption: str) -> Optional[str]:
    data = parse_input(caption)
    price = data.get("price")
    if price is None:
        return None
    mode = MODES.get(active_mode.get(user_id, "lux"), MODES["lux"])
    calc_fn, tpl_fn, _label = mode["calc"], mode["template"], mode["label"]
    final_price = calc_fn(float(price), int(data.get("discount", 0)))
    return tpl_fn(final_price=final_price,
                  retail=float(data.get("retail", 0.0) or 0.0),
                  sizes_line=data.get("sizes_line", "") or "",
                  season_line=data.get("season_line", "") or "",
                  brand_line="",
                  materials_line=data.get("materials_line", "") or "")

async def _remember_media_for_text(chat_id: int, user_id: int, items: List[Dict[str, Any]], mgid: Optional[str]=None, caption: str="", seq: Optional[int]=None):
    last_media[chat_id] = {
        "ts": datetime.now(), "items": list(items),
        "caption": caption or "", "mgid": mgid or "",
        "user_id": user_id, "seq": seq if seq is not None else alloc_seq(),
    }

# Хэндлеры фото/видео/альбомов/текста — оставь как в своей последней версии (они корректны)

# ====== ХУКИ ЗАПУСКА/ОСТАНОВКИ ======
_publish_task: Optional[asyncio.Task] = None

@dp.startup()
async def _on_startup():
    global _publish_task
    print("[STARTUP] delete webhook & start worker")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print(f"delete_webhook error: {e}")
    await asyncio.sleep(0.5)
    _publish_task = asyncio.create_task(publish_worker())

@dp.shutdown()
async def _on_shutdown():
    global _publish_task
    print("[SHUTDOWN] stopping worker")
    try:
        if _publish_task and not _publish_task.done():
            _publish_task.cancel()
            try:
                await _publish_task
            except asyncio.CancelledError:
                pass
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass

# ====== ЗАПУСК ======
async def main():
    attempt = 0
    while True:
        try:
            attempt += 1
            print(f"Запуск бота, попытка {attempt}...")
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            break
        except TelegramConflictError as e:
            wait_time = min(5 + attempt * 0.5, 15)
            print(f"TelegramConflictError: {e}. Ждём {wait_time} секунд...")
            await asyncio.sleep(wait_time)
            continue
        except KeyboardInterrupt:
            print("Остановка...")
            break
        except Exception as e:
            print(f"Неожиданная ошибка: {e}")
            await asyncio.sleep(5)
            continue

if __name__ == "__main__":
    asyncio.run(main())
