# bot.py — альбомы, 30+ режимов, OCR ценника, дебаунс 0.8с для альбомов,
# подсказки при отсутствии цены и поддержка "альбом/фото/видео → общий текст"
# + Гарантия порядка публикаций через FIFO-очередь

import os
import re
import io
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

ALBUM_SETTLE_MS = int(os.getenv("ALBUM_SETTLE_MS", "800"))
ALBUM_WINDOW_SECONDS = int(os.getenv("ALBUM_WINDOW_SECONDS", "30"))

OCR_ENABLED = os.getenv("OCR_ENABLED", "1") == "1"
OCR_LANG = os.getenv("OCR_LANG", "ita+eng")
# В альбомах убирать кадры-ценники (1 — да; 0 — пересылать как есть)
FILTER_PRICETAGS_IN_ALBUMS = os.getenv("FILTER_PRICETAGS_IN_ALBUMS", "1") == "1"

# ====== ИНИЦИАЛИЗАЦИЯ ======
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ====== ПАМЯТЬ ======
# MediaItem: {"kind": "photo"|"video", "fid": str, "mid": int, "cap": bool}
last_media: Dict[int, Dict[str, Any]] = {}
active_mode: Dict[int, str] = {}
album_buffers: Dict[Tuple[int, str], Dict[str, Any]] = {}

# ====== ОЧЕРЕДЬ ПУБЛИКАЦИЙ (FIFO) ======
publish_queue: "asyncio.Queue[Tuple[List[Dict[str, Any]], str]]" = asyncio.Queue()

async def _do_publish(items: List[Dict[str, Any]], caption: str):
    """Реальная отправка сообщений (фото/видео/альбомы)."""
    if not items:
        return

    # OCR-фильтрация (логика различается для альбомов и одиночных)
    items = await filter_pricetag_media(items)

    if len(items) == 1:
        it = items[0]
        if it["kind"] == "video":
            await bot.send_video(TARGET_CHAT_ID, it["fid"], caption=caption)
        else:
            await bot.send_photo(TARGET_CHAT_ID, it["fid"], caption=caption)
        return

    # Альбом: подпись ставим к первому элементу (фото или видео)
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
        items, caption = await publish_queue.get()
        try:
            await _do_publish(items, caption)
        except Exception:
            # не блокируем очередь из-за ошибки одного сообщения
            pass
        finally:
            publish_queue.task_done()

async def publish_to_target(items: List[Dict[str, Any]], caption: str):
    await publish_queue.put((items, caption))

# ====== OCR ======
if OCR_ENABLED:
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        OCR_ENABLED = False

async def ocr_should_hide(file_id: str) -> bool:
    """Прятать ли фото-ценник (видео не трогаем). Строго: видим € и число РЯДОМ + (% или retail/price/prezzo)."""
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

        # точный токен цены: "€123" или "123€"
        has_price_token = bool(re.search(r"(?:€\s*\d{2,6}|\d{2,6}\s*€)", txt))
        has_kw = ("retail" in tl) or ("price" in tl) or ("prezzo" in tl) or ("%" in txt)

        return bool(has_price_token and has_kw)
    except Exception:
        return False

async def filter_pricetag_media(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Одиночные: ничего не удаляем.
    Альбомы: при FILTER_PRICETAGS_IN_ALBUMS=1 — вырезаем ТОЛЬКО кадры-ценники (видео никогда не режем).
    Если всё вырезалось — оставляем первый исходный, чтобы не «съесть» публикацию.
    """
    if len(items) == 1 or not FILTER_PRICETAGS_IN_ALBUMS:
        return items

    kept: List[Dict[str, Any]] = []
    for it in items:
        if it["kind"] == "photo":
            if not await ocr_should_hide(it["fid"]):
                kept.append(it)
        else:
            kept.append(it)  # видео всегда оставляем

    return kept or items[:1]

# ====== ВСПОМОГАТЕЛЬНОЕ ======
def round_price(value: float) -> int:
    return int(round(value, 0))

def default_calc(price: float, discount: int) -> int:
    discounted = price * (1 - discount / 100)
    if discounted <= 250:
        return round_price(discounted + 55)
    elif discounted <= 400:
        return round_price(discounted + 70)
    else:
        return round_price(discounted + 90)

# ====== РАЗБОР И ФОРМИРОВАНИЕ 5-СТРОЧНОЙ ПОДПИСИ ======

# Удаляем только хештеги
def cleanup_text_basic(text: str) -> str:
    text = re.sub(r"#\S+", "", text)  # убрать #теги
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# Поддержка размеров: буквы и/или цифры (диапазоны и списки)
SIZE_TOKEN = r"(?:XXS|XS|S|M|L|XL|XXL|[2-5]\d)"

def pick_sizes_line(lines: List[str]) -> str:
    for line in lines:
        l = line.strip()
        if not l:
            continue
        # не брать строки с ценой/скидкой/ретейлом/валютой
        if re.search(r"(€|%|\bretail\b|\bprice\b)", l, flags=re.I):
            continue
        if re.search(fr"\b{SIZE_TOKEN}\b", l, flags=re.I):
            return l
        # варианты вида "40( на мне), 44"
        if re.search(r"\b[2-5]\d\b(?:\s*\([^)]+\))?(?:\s*,\s*\b[2-5]\d\b(?:\s*\([^)]+\))?)*", l):
            return l
    return ""

def pick_season_line(lines: List[str]) -> str:
    # сначала ищем строку с NEW FW/SS..., если нет — просто FW/SS...
    for line in lines:
        if re.search(r"\bNEW\s+(?:FW|SS)\d+(?:/\d+)?\b", line, flags=re.I):
            return line.strip()
    for line in lines:
        if re.search(r"\b(?:FW|SS)\d+(?:/\d+)?\b", line, flags=re.I):
            return line.strip()
    return ""

def pick_brand_line(lines: List[str], used: List[str]) -> str:
    """
    Берём первую строку, которая не используется под цену/ретейл/размер/сезон
    и не выглядит как строка сезона (даже без NEW).
    """
    used_set = set(used)
    for line in lines:
        l = line.strip()
        if not l or l in used_set:
            continue
        if re.search(r"(€|%|\bretail\b|\bprice\b)", l, flags=re.I):
            continue
        if re.search(fr"\b{SIZE_TOKEN}\b", l, flags=re.I):
            continue
        # исключаем любые FW/SS-строки (чтобы не дублировать сезон)
        if re.search(r"\b(?:NEW\s+)?(?:FW|SS)\d+(?:/\d+)?\b", l, flags=re.I):
            continue
        if re.search(r"[A-Za-zА-Яа-яЁё]", l):
            return l
    return ""

def parse_number_token(token: Optional[str]) -> Optional[float]:
    if not token:
        return None
    # поддержка 2.950 / 2,950
    return float(token.replace('.', '').replace(',', ''))

def parse_input(raw_text: str) -> Dict[str, Optional[str]]:
    text = cleanup_text_basic(raw_text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # 1) цена/скидка/ретейл
    price_m   = re.search(r"(\d+(?:[.,]\d{3})*)\s*€", text)
    discount_m= re.search(r"-(\d+)%", text)
    retail_m  = re.search(r"Retail\s*price\s*(\d+(?:[.,]\d{3})*)", text, flags=re.I)

    price   = parse_number_token(price_m.group(1)) if price_m else None
    discount= int(discount_m.group(1)) if discount_m else 0
    retail  = parse_number_token(retail_m.group(1)) if retail_m else (price if price is not None else 0.0)

    # 2) размеры/сезон/бренд — строками
    sizes_line  = pick_sizes_line(lines)
    season_line = pick_season_line(lines)
    brand_line  = pick_brand_line(lines, used=[sizes_line, season_line])

    return {
        "price": price,
        "discount": discount,
        "retail": retail,
        "sizes_line": sizes_line,
        "season_line": season_line,
        "brand_line": brand_line,
        "cleaned_text": text,
    }

def template_five_lines(final_price: int,
                        retail: float,
                        sizes_line: str,
                        season_line: str,
                        brand_line: str) -> str:
    # Строго 5 строк; если чего-то нет — оставляем пустую строку на своём месте
    line1 = f"✅ <b>{final_price}€</b>"
    line2 = f"❌ <b>Retail price {round_price(retail)}€</b>"
    line3 = sizes_line or ""
    line4 = season_line or ""
    line5 = brand_line or ""
    return "\n".join([line1, line2, line3, line4, line5])

def mk_mode(label: str,
            calc: Callable[[float, int], int] = default_calc,
            template: Callable[[int, float, str, str, str], str] = template_five_lines):
    return {"label": label, "calc": calc, "template": template}

MODES: Dict[str, Dict] = {
    "sale": mk_mode("SALE"),
    "lux": mk_mode("LUX"),
    "outlet": mk_mode("OUTLET"),
    "stock": mk_mode("STOCK"),
    "newfw": mk_mode("NEW FW"),
    "newss": mk_mode("NEW SS"),
    "bags10": mk_mode("BAGS -10%"),
    "bags15": mk_mode("BAGS -15%"),
    "bags20": mk_mode("BAGS -20%"),
    "bags25": mk_mode("BAGS -25%"),
    "bags30": mk_mode("BAGS -30%"),
    "bags40": mk_mode("BAGS -40%"),
    "shoes10": mk_mode("SHOES -10%"),
    "shoes20": mk_mode("SHOES -20%"),
    "shoes30": mk_mode("SHOES -30%"),
    "shoes40": mk_mode("SHOES -40%"),
    "rtw10": mk_mode("RTW -10%"),
    "rtw20": mk_mode("RTW -20%"),
    "rtw30": mk_mode("RTW -30%"),
    "rtw40": mk_mode("RTW -40%"),
    "acc10": mk_mode("ACCESSORIES -10%"),
    "acc20": mk_mode("ACCESSORIES -20%"),
    "acc30": mk_mode("ACCESSORIES -30%"),
    "men": mk_mode("MEN"),
    "women": mk_mode("WOMEN"),
    "vip": mk_mode("VIP"),
    "promo": mk_mode("PROMO"),
    "flash": mk_mode("FLASH"),
    "bundle": mk_mode("BUNDLE"),
    "limited": mk_mode("LIMITED"),
    "m1": mk_mode("M1"), "m2": mk_mode("M2"), "m3": mk_mode("M3"), "m4": mk_mode("M4"), "m5": mk_mode("M5"),
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
        "Бот принимает фото/видео (в т.ч. альбомы) и текст с ценой.\n"
        "• Без цены — ждём твой текст (до 30с) и публикуем единым постом."
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
        brand_line=data.get("brand_line", "") or "",
    )

# ====== ХЕЛПЕРЫ ======
async def _remember_media_for_text(chat_id: int, items: List[Dict[str, Any]], mgid: Optional[str] = None, caption: str = ""):
    last_media[chat_id] = {
        "ts": datetime.now(),
        "items": items,
        "caption": caption or "",
        "mgid": mgid or "",
    }

# ====== ХЕНДЛЕРЫ ======
# Одиночное фото
@router.message(F.photo & (F.media_group_id == None))
async def handle_single_photo(msg: Message):
    item = {"kind": "photo", "fid": msg.photo[-1].file_id, "mid": msg.message_id, "cap": bool(msg.caption)}
    caption = (msg.caption or "").strip()
    if caption:
        result = build_result_text(msg.from_user.id, caption)
        if result:
            await publish_to_target([item], result)
            return
    await _remember_media_for_text(msg.chat.id, [item], caption=caption)
    try:
        await msg.answer("Добавь текст с ценой/скидкой (например: 650€ -35%) — опубликую одним постом.")
    except Exception:
        pass

# Одиночное видео
@router.message(F.video & (F.media_group_id == None))
async def handle_single_video(msg: Message):
    item = {"kind": "video", "fid": msg.video.file_id, "mid": msg.message_id, "cap": bool(msg.caption)}
    caption = (msg.caption or "").strip()
    if caption:
        result = build_result_text(msg.from_user.id, caption)
        if result:
            await publish_to_target([item], result)
            return
    await _remember_media_for_text(msg.chat.id, [item], caption=caption)
    try:
        await msg.answer("Добавь текст с ценой/скидкой (например: 650€ -35%) — опубликую одним постом.")
    except Exception:
        pass

# Альбом (фото/видео вперемешку)
@router.message(F.media_group_id)
async def handle_album_any(msg: Message):
    chat_id, mgid = msg.chat.id, str(msg.media_group_id)
    key = (chat_id, mgid)

    # Определяем тип и file_id
    if msg.photo:
        fid = msg.photo[-1].file_id
        kind = "photo"
    elif msg.video:
        fid = msg.video.file_id
        kind = "video"
    else:
        return

    cap_text = (msg.caption or "").strip()
    has_cap = bool(cap_text)

    buf = album_buffers.get(key)
    if not buf:
        buf = {"items": [], "caption": "", "task": None, "user_id": msg.from_user.id}
        album_buffers[key] = buf

    buf["items"].append({"kind": kind, "fid": fid, "mid": msg.message_id, "cap": has_cap})
    if has_cap and not buf["caption"]:
        buf["caption"] = cap_text

    if buf["task"]:
        buf["task"].cancel()

    async def _flush_album():
        await asyncio.sleep(ALBUM_SETTLE_MS / 1000)

        data = album_buffers.pop(key, None)
        if not data:
            return

        items: List[Dict[str, Any]] = data["items"]
        caption = data["caption"]
        user_id = data["user_id"]

        # Сохраняем порядок и переносим элемент с подписью в начало
        items.sort(key=lambda x: x["mid"])
        idx_cap = next((i for i, it in enumerate(items) if it["cap"]), None)
        if idx_cap not in (None, 0):
            items.insert(0, items.pop(idx_cap))

        if caption:
            result = build_result_text(user_id, caption)
            if result:
                await publish_to_target(items, result)
                return

        await _remember_media_for_text(chat_id, items, mgid=mgid, caption=caption)
        lm = last_media.get(chat_id)
        if not (lm and (datetime.now() - lm["ts"] <= timedelta(seconds=ALBUM_WINDOW_SECONDS))):
            try:
                await bot.send_message(chat_id, "Добавь текст с ценой/скидкой (например: 650€ -35%) — опубликую альбом одним постом.")
            except Exception:
                pass

    buf["task"] = asyncio.create_task(_flush_album())

@router.message(F.text)
async def handle_text(msg: Message):
    chat_id, user_id = msg.chat.id, msg.from_user.id
    bucket = last_media.get(chat_id)

    if bucket and (datetime.now() - bucket["ts"] <= timedelta(seconds=ALBUM_WINDOW_SECONDS)):
        raw_text = (bucket.get("caption") or "")
        if raw_text:
            raw_text += "\n"
        raw_text += (msg.text or "")

        result = build_result_text(user_id, raw_text)
        items: List[Dict[str, Any]] = bucket.get("items") or []

        if result:
            await publish_to_target(items, result)
        else:
            await publish_to_target(items, f"⚠️ Не нашла цену в тексте. Пример: 650€ -35%\n\n{msg.text}")

        del last_media[chat_id]
        return

    # Текст после альбома
    cand = [(k, v) for (k, v) in album_buffers.items() if k[0] == chat_id and v.get("items")]
    if cand:
        def last_mid(buf): return max(it["mid"] for it in buf["items"])
        cand.sort(key=lambda kv: last_mid(kv[1]))
        eligible = [kv for kv in cand if last_mid(kv[1]) <= msg.message_id]
        key, data = (eligible[-1] if eligible else cand[-1])

        items: List[Dict[str, Any]] = data["items"]
        caption = (data.get("caption") or "")
        if caption:
            caption += "\n"
        caption += (msg.text or "")

        items.sort(key=lambda x: x["mid"])
        idx_cap = next((i for i, it in enumerate(items) if it["cap"]), None)
        if idx_cap not in (None, 0):
            items.insert(0, items.pop(idx_cap))

        result = build_result_text(user_id, caption)

        if data.get("task"):
            data["task"].cancel()
        album_buffers.pop(key, None)

        if result:
            await publish_to_target(items, result)
        else:
            await publish_to_target(items, f"⚠️ Не нашла цену в тексте. Пример: 650€ -35%\n\n{msg.text}")
        return

    return

# ====== ЗАПУСК ======
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(publish_worker())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
