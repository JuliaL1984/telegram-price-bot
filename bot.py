# bot.py — альбомы, 30+ режимов, OCR ценника
import os
import re
import io
import asyncio
from typing import Dict, Callable, Optional, List
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, InputMediaPhoto
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command

# ====== НАСТРОЙКИ ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1002973176038"))
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

# окно «собрать альбом» (сек) — только чтобы поймать все фото из media_group
ALBUM_WINDOW_SECONDS = int(os.getenv("ALBUM_WINDOW_SECONDS", "30"))

# OCR ценника (Tesseract). 1/0 — включить/выключить.
OCR_ENABLED = os.getenv("OCR_ENABLED", "1") == "1"
# языки OCR (нужны соответствующие языковые пакеты в образе)
OCR_LANG = os.getenv("OCR_LANG", "ita+eng")

# ====== ИНИЦИАЛИЗАЦИЯ ======
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ====== ПАМЯТЬ ======
# по chat.id: {"ts": dt, "file_ids": [...], "caption": str, "mgid": media_group_id}
last_media: Dict[int, Dict] = {}
# активный режим по user_id
active_mode: Dict[int, str] = {}

# ====== ВСПОМОГАТЕЛЬНОЕ ======
def round_price(value: float) -> int:
    return int(round(value, 0))

def default_calc(price: float, discount: int) -> int:
    """Сначала скидка, затем наценка по диапазону."""
    discounted = price * (1 - discount / 100)
    if discounted <= 250:
        return round_price(discounted + 55)
    elif discounted <= 400:
        return round_price(discounted + 70)
    else:
        return round_price(discounted + 90)

def default_template(final_price: int, retail: float, sizes: str, season: str, mode_label: Optional[str] = None) -> str:
    tag = f"<i>({mode_label})</i>\n" if mode_label else ""
    return (
        f"{tag}"
        f"✅ <b>{final_price}€</b>\n"
        f"❌ <b>Retail price {round_price(retail)}€</b>\n"
        f"{sizes}\n"
        f"{season}"
    )

def cleanup_text_basic(text: str) -> str:
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"(?i)\b(Gucci|Prada|Louis\s*Vuitton|LV|Stone\s*Island|Balenciaga|Bally|Jimmy\s*Choo)\b", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def parse_input(text: str) -> Dict[str, Optional[str]]:
    """
    Достаём:
      - price: первое число перед символом €
      - discount: -NN%
      - retail: 'Retail price NNN'
      - sizes: строка, начинающаяся с XS/S/M/L/XL/XXL
      - season: FWxx/xx или SSxx
    """
    price_m   = re.search(r"(\d+)\s*€", text)
    discount_m= re.search(r"-(\d+)%", text)
    retail_m  = re.search(r"Retail\s*price\s*(\d+)", text, flags=re.I)
    sizes_m   = re.search(r"((?:XS|S|M|L|XL|XXL)[^\n]*)", text, flags=re.I)
    season_m  = re.search(r"(FW\d+\/\d+|SS\d+)", text)

    price   = float(price_m.group(1)) if price_m else None
    discount= int(discount_m.group(1)) if discount_m else 0
    retail  = float(retail_m.group(1)) if retail_m else (price if price is not None else 0.0)
    sizes   = sizes_m.group(1).strip() if sizes_m else ""
    season  = season_m.group(1) if season_m else ""
    return {"price": price, "discount": discount, "retail": retail, "sizes": sizes, "season": season}

def mk_mode(label: str,
            calc: Callable[[float, int], int] = default_calc,
            template: Callable[[int, float, str, str, Optional[str]], str] = default_template):
    return {"label": label, "calc": calc, "template": template}

MODES: Dict[str, Dict] = {
    # БАЗОВЫЕ
    "sale":   mk_mode("SALE"),
    "lux":    mk_mode("LUX"),
    "outlet": mk_mode("OUTLET"),
    "stock":  mk_mode("STOCK"),
    "newfw":  mk_mode("NEW FW"),
    "newss":  mk_mode("NEW SS"),

    # СУМКИ
    "bags10": mk_mode("BAGS -10%"),
    "bags15": mk_mode("BAGS -15%"),
    "bags20": mk_mode("BAGS -20%"),
    "bags25": mk_mode("BAGS -25%"),
    "bags30": mk_mode("BAGS -30%"),
    "bags40": mk_mode("BAGS -40%"),

    # ОБУВЬ
    "shoes10": mk_mode("SHOES -10%"),
    "shoes20": mk_mode("SHOES -20%"),
    "shoes30": mk_mode("SHOES -30%"),
    "shoes40": mk_mode("SHOES -40%"),

    # ОДЕЖДА
    "rtw10": mk_mode("RTW -10%"),
    "rtw20": mk_mode("RTW -20%"),
    "rtw30": mk_mode("RTW -30%"),
    "rtw40": mk_mode("RTW -40%"),

    # АКСЕССУАРЫ
    "acc10": mk_mode("ACCESSORIES -10%"),
    "acc20": mk_mode("ACCESSORIES -20%"),
    "acc30": mk_mode("ACCESSORIES -30%"),

    # М/Ж
    "men": mk_mode("MEN"),
    "women": mk_mode("WOMEN"),

    # СПЕЦ/РЕЗЕРВ
    "vip": mk_mode("VIP"),
    "promo": mk_mode("PROMO"),
    "flash": mk_mode("FLASH"),
    "bundle": mk_mode("BUNDLE"),
    "limited": mk_mode("LIMITED"),
    "m1": mk_mode("M1"), "m2": mk_mode("M2"), "m3": mk_mode("M3"), "m4": mk_mode("M4"), "m5": mk_mode("M5"),
}

def is_admin(user_id: int) -> bool:
    return (not ADMINS) or (user_id in ADMINS)

# ====== ПУБЛИКАЦИЯ (фото/альбом) ======
async def publish_to_target(file_ids: List[str], caption: str):
    if not file_ids:
        return
    if len(file_ids) == 1:
        await bot.send_photo(TARGET_CHAT_ID, file_ids[0], caption=caption)
    else:
        media = []
        for i, fid in enumerate(file_ids):
            if i == 0:
                media.append(InputMediaPhoto(media=fid, caption=caption, parse_mode=ParseMode.HTML))
            else:
                media.append(InputMediaPhoto(media=fid))
        await bot.send_media_group(TARGET_CHAT_ID, media)

# ====== OCR ценника ======
if OCR_ENABLED:
    try:
        import pytesseract
        from PIL import Image
    except Exception as _:
        OCR_ENABLED = False  # на всякий случай, если библиотек нет

async def ocr_should_hide(file_id: str) -> bool:
    """
    True — если на фото есть символ '€' и слово из {'prezzo','price','retail'} (регистр не важен).
    """
    if not OCR_ENABLED:
        return False
    try:
        file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download(file, buf)
        buf.seek(0)
        img = Image.open(buf)
        txt = pytesseract.image_to_string(img, lang=OCR_LANG) or ""
        tl = txt.lower()
        has_euro = "€" in txt or "eur" in tl
        has_word = any(w in tl for w in ("prezzo", "price", "retail"))
        return bool(has_euro and has_word)
    except Exception:
        return False

async def filter_pricetag_photos(file_ids: List[str]) -> List[str]:
    res: List[str] = []
    for fid in file_ids:
        hide = await ocr_should_hide(fid)
        if not hide:
            res.append(fid)
    # если вдруг отфильтровали всё — вернём хотя бы первую, чтобы не потерять пост
    return res or (file_ids[:1])

# ====== КОМАНДЫ ======
@router.message(Command(commands=list(MODES.keys())))
async def set_mode(msg: Message):
    user_id = msg.from_user.id
    cmd = msg.text.lstrip("/").split()[0]
    if not is_admin(user_id):
        return await msg.answer("⛔ Доступно только администраторам.")
    active_mode[user_id] = cmd
    label = MODES[cmd]["label"]
    await msg.answer(f"✅ Режим <b>{label}</b> активирован.")

@router.message(Command("mode"))
async def show_mode(msg: Message):
    user_id = msg.from_user.id
    mode = active_mode.get(user_id, "sale")
    label = MODES.get(mode, {}).get("label", mode)
    await msg.answer(f"Текущий режим: <b>{label}</b>\nСменить: /" + " /".join(list(MODES.keys())[:10]) + " …")

@router.message(Command("help"))
async def show_help(msg: Message):
    await msg.answer(
        "<b>Как работать:</b>\n"
        "— Отправь фото товара (одиночное или альбом).\n"
        "— Следом пришли текст с ценой/скидкой/размерами/сезоном.\n"
        "— Одиночное фото с подписью «650€ -35%» публикуется сразу.\n"
        "— Бот посчитает и опубликует в целевой группе одним постом."
    )

@router.message(Command("ping"))
async def ping(msg: Message):
    await msg.answer("pong")

# ====== ХЕНДЛЕРЫ КОНТЕНТА ======
@router.message(F.photo)
async def handle_photo(msg: Message):
    """
    1) Одиночное фото + подпись «650€ -35%» → публикация сразу.
    2) Альбом (media_group) или фото без подписи → складываем в память, ждём текст.
    """
    chat_id = msg.chat.id
    mgid = msg.media_group_id  # None для одиночной

    # кейс 1: одиночное фото С подписью, подпись парсится → публикуем сразу
    if mgid is None and msg.caption:
        cleaned = cleanup_text_basic(msg.caption)
        data = parse_input(cleaned)
        price = data.get("price")
        if price is not None:
            user_id = msg.from_user.id
            mode_key = active_mode.get(user_id, "sale")
            mode = MODES.get(mode_key, MODES["sale"])
            label = mode["label"]
            calc_fn = mode["calc"]
            tpl_fn = mode["template"]

            final_price = calc_fn(price, data.get("discount", 0))
            result_msg = tpl_fn(final_price, data.get("retail", 0.0), data.get("sizes", ""), data.get("season", ""), mode_label=label)

            fids = [msg.photo[-1].file_id]
            # фильтруем ценники (если включен OCR)
            fids = await filter_pricetag_photos(fids)
            await publish_to_target(fids, result_msg)
            return  # ничего в память не кладём

    # кейс 2: альбом/без подписи → буферизуем
    bucket = last_media.get(chat_id)
    now = datetime.now()
    if not bucket or (now - bucket["ts"]).total_seconds() > ALBUM_WINDOW_SECONDS:
        last_media[chat_id] = {
            "ts": now,
            "file_ids": [msg.photo[-1].file_id],
            "caption": msg.caption or "",
            "mgid": mgid,
        }
    else:
        bucket["file_ids"].append(msg.photo[-1].file_id)
        if msg.caption and not bucket.get("caption"):
            bucket["caption"] = msg.caption

@router.message(F.text)
async def handle_text(msg: Message):
    chat_id = msg.chat.id
    user_id = msg.from_user.id

    bucket = last_media.get(chat_id)
    if not bucket:
        return  # текст сам по себе нам не нужен

    if datetime.now() - bucket["ts"] > timedelta(seconds=ALBUM_WINDOW_SECONDS):
        del last_media[chat_id]
        return

    mode_key = active_mode.get(user_id, "sale")
    mode = MODES.get(mode_key, MODES["sale"])
    label = mode["label"]
    calc_fn = mode["calc"]
    tpl_fn = mode["template"]

    raw_text = (bucket.get("caption") or "") + "\n" + (msg.text or "")
    cleaned = cleanup_text_basic(raw_text)
    data = parse_input(cleaned)

    price = data.get("price")
    if price is None:
        del last_media[chat_id]
        return

    final_price = calc_fn(price, data.get("discount", 0))
    result_msg = tpl_fn(final_price, data.get("retail", 0.0), data.get("sizes", ""), data.get("season", ""), mode_label=label)

    fids = bucket.get("file_ids") or []
    fids = await filter_pricetag_photos(fids)
    await publish_to_target(fids, result_msg)

    del last_media[chat_id]

# ====== ЗАПУСК ======
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
