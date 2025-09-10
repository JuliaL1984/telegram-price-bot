# bot.py — 30+ режимов + Tesseract OCR + строгое удаление ценника + уведомления
import os
import re
import asyncio
import tempfile
from typing import Dict, Callable, Optional
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command

# OCR
import pytesseract
from PIL import Image

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1002973176038"))  # куда публикуем
WAIT_TEXT_SECONDS = int(os.getenv("WAIT_TEXT_SECONDS", "8"))         # окно ожидания текста после фото (сек)
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

# Язык Tesseract (лучше eng+ita для 'prezzo')
TESS_LANG = os.getenv("TESS_LANG", "eng")
# Писать ли предупреждение, если не удаляем ценник
PRICETAG_NOTICE = os.getenv("PRICETAG_NOTICE", "1") == "1"

# ===================== ИНИЦИАЛИЗАЦИЯ =================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Память
last_media: Dict[int, Dict] = {}    # chat_id -> {...}
active_mode: Dict[int, str] = {}    # user_id -> mode_key

# ===================== ВСПОМОГАТЕЛЬНОЕ =================
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

def default_template(final_price: int, retail: float, sizes: str, season: str, mode_label: Optional[str] = None) -> str:
    tag = f"<i>({mode_label})</i>\n" if mode_label else ""
    sizes_str = sizes or ""
    season_str = season or ""
    return (
        f"{tag}"
        f"✅ <b>{final_price}€</b>\n"
        f"❌ <b>Retail price {round_price(retail)}€</b>\n"
        f"{sizes_str}\n"
        f"{season_str}"
    ).strip()

def cleanup_text_basic(text: str) -> str:
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"(?i)\b(Gucci|Prada|Louis\s*Vuitton|LV|Stone\s*Island|Balenciaga|Bally|Jimmy\s*Choo)\b", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def parse_input(text: str) -> Dict[str, Optional[str]]:
    price_m   = re.search(r"(\d+)\s*€", text)
    discount_m= re.search(r"-(\d+)%", text)
    retail_m  = re.search(r"Retail\s*price\s*(\d+)", text, re.IGNORECASE)
    sizes_m   = re.search(r"((?:XS|S|M|L|XL|XXL)[^\n]*)", text, flags=re.I)
    season_m  = re.search(r"(FW\d+\/\d+|SS\d+)", text)

    price    = float(price_m.group(1)) if price_m else None
    discount = int(discount_m.group(1)) if discount_m else 0
    retail   = float(retail_m.group(1)) if retail_m else (price if price is not None else 0.0)
    sizes    = sizes_m.group(1).strip() if sizes_m else ""
    season   = season_m.group(1) if season_m else ""

    return {"price": price, "discount": discount, "retail": retail, "sizes": sizes, "season": season}

# ---------- OCR (Tesseract) ----------
async def ocr_text_from_photo_tesseract(message: Message) -> str:
    file_id = message.photo[-1].file_id
    tg_file = await bot.get_file(file_id)
    with tempfile.TemporaryDirectory() as td:
        local = os.path.join(td, "img.jpg")
        await bot.download_file(tg_file.file_path, destination=local)
        img = Image.open(local)
        text = pytesseract.image_to_string(img, lang=TESS_LANG)
        return text.strip()

def parse_price_from_text(txt: str) -> Optional[float]:
    if not txt:
        return None
    one_line = txt.replace("\n", " ")

    def to_num(s: str) -> float:
        return float(s.replace(" ", "").replace(".", "").replace(",", ""))

    with_currency = re.findall(r"(?:EUR\s*)?(\d{1,3}(?:[\s.,]\d{3})*|\d+)\s*(?:€|EUR)", one_line, flags=re.I)
    if with_currency:
        try:
            return max(to_num(x) for x in with_currency)
        except Exception:
            pass

    plain = re.findall(r"(?<![\d.,])(\d{1,3}(?:[\s.,]\d{3})*|\d+)(?![\d.,])", one_line)
    if plain:
        try:
            return max(to_num(x) for x in plain)
        except Exception:
            pass
    return None

def looks_like_pricetag(txt: str) -> bool:
    if not txt:
        return False
    t = txt.lower()
    has_euro = "€" in t
    has_keyword = ("prezzo" in t) or ("price" in t) or ("retail" in t)
    return has_euro and has_keyword

# ===================== РЕЖИМЫ =================
def mk_mode(label: str,
            calc: Callable[[float, int], int] = default_calc,
            template: Callable[[int, float, str, str, Optional[str]], str] = default_template):
    return {"label": label, "calc": calc, "template": template}

MODES: Dict[str, Dict] = {
    # базовые
    "sale": mk_mode("SALE"),
    "lux": mk_mode("LUX"),
    "outlet": mk_mode("OUTLET"),
    "stock": mk_mode("STOCK"),
    "newfw": mk_mode("NEW FW"),
    "newss": mk_mode("NEW SS"),
    # сумки
    "bags10": mk_mode("BAGS -10%"),
    "bags15": mk_mode("BAGS -15%"),
    "bags20": mk_mode("BAGS -20%"),
    "bags25": mk_mode("BAGS -25%"),
    "bags30": mk_mode("BAGS -30%"),
    "bags40": mk_mode("BAGS -40%"),
    # обувь
    "shoes10": mk_mode("SHOES -10%"),
    "shoes20": mk_mode("SHOES -20%"),
    "shoes30": mk_mode("SHOES -30%"),
    "shoes40": mk_mode("SHOES -40%"),
    # одежда
    "rtw10": mk_mode("RTW -10%"),
    "rtw20": mk_mode("RTW -20%"),
    "rtw30": mk_mode("RTW -30%"),
    "rtw40": mk_mode("RTW -40%"),
    # аксессуары
    "acc10": mk_mode("ACCESSORIES -10%"),
    "acc20": mk_mode("ACCESSORIES -20%"),
    "acc30": mk_mode("ACCESSORIES -30%"),
    # муж/жен
    "men": mk_mode("MEN"),
    "women": mk_mode("WOMEN"),
    # спец
    "vip": mk_mode("VIP"),
    "promo": mk_mode("PROMO"),
    "flash": mk_mode("FLASH"),
    "bundle": mk_mode("BUNDLE"),
    "limited": mk_mode("LIMITED"),
    # резерв
    "m1": mk_mode("M1"),
    "m2": mk_mode("M2"),
    "m3": mk_mode("M3"),
    "m4": mk_mode("M4"),
    "m5": mk_mode("M5"),
}

def is_admin(user_id: int) -> bool:
    return (not ADMINS) or (user_id in ADMINS)

# ===================== КОМАНДЫ =================
@router.message(Command(commands=list(MODES.keys())))
async def set_mode(msg: Message):
    user_id = msg.from_user.id
    cmd = msg.text.lstrip("/").split()[0]
    if not is_admin(user_id):
        return await msg.answer("⛔ Доступно только администраторам.")
    active_mode[user_id] = cmd
    label = MODES[cmd]["label"]
    await msg.answer(f"✅ Режим <b>{label}</b> активирован.\nПришлите загрузку (фото + текст).")

@router.message(Command("mode"))
async def show_mode(msg: Message):
    user_id = msg.from_user.id
    mode = active_mode.get(user_id, "sale")
    label = MODES.get(mode, {}).get("label", mode)
    await msg.answer(f"Текущий режим: <b>{label}</b>\nСменить: /" + " /".join(list(MODES.keys())[:10]) + " …")

@router.message(Command("help"))
async def show_help(msg: Message):
    txt = (
        "<b>Как работать:</b>\n"
        "1) Отправь фото (товара или ценника).\n"
        f"2) В течение {WAIT_TEXT_SECONDS} сек пришли текст со скидкой/размерами/сезоном.\n"
        "3) Бот посчитает и опубликует в целевой группе.\n\n"
        "<b>Режимы:</b>\n"
        "— Включай командой /sale, /lux, /bags30, /shoes20 и т.д.\n"
        "— Посмотреть текущий: /mode\n"
    )
    await msg.answer(txt)

# ===================== ХЕНДЛЕРЫ =================
@router.message(F.photo)
async def handle_photo(msg: Message):
    """
    На любое фото запускаем OCR.
    Если OCR содержит символ '€' и слово prezzo/price/retail — считаем это ценником.
    """
    ocr_text = ""
    retail_from_ocr = None
    is_pricetag = False
    euro_present = False
    keyword_present = False

    try:
        ocr_text = await ocr_text_from_photo_tesseract(msg)
        t = (ocr_text or "").lower()
        euro_present = "€" in t
        keyword_present = ("prezzo" in t) or ("price" in t) or ("retail" in t)

        if euro_present and keyword_present:
            is_pricetag = True
            retail_from_ocr = parse_price_from_text(ocr_text)
    except Exception:
        pass

    last_media[msg.chat.id] = {
        "ts": datetime.now(),
        "file_ids": [msg.photo[-1].file_id],
        "caption": msg.caption or "",
        "ocr": ocr_text,
        "retail_from_ocr": retail_from_ocr,
        "is_pricetag": is_pricetag,
        "euro_present": euro_present,
        "keyword_present": keyword_present,
        "message_id": msg.message_id,
    }

@router.message(F.text)
async def handle_text(msg: Message):
    chat_id = msg.chat.id
    user_id = msg.from_user.id

    media = last_media.get(chat_id)
    if not media:
        return
    if datetime.now() - media["ts"] > timedelta(seconds=WAIT_TEXT_SECONDS):
        del last_media[chat_id]
        return

    mode_key = active_mode.get(user_id, "sale")
    mode = MODES.get(mode_key, MODES["sale"])
    label = mode["label"]
    calc_fn = mode["calc"]
    tpl_fn = mode["template"]

    cleaned = cleanup_text_basic(msg.text.strip())
    data = parse_input(cleaned)

    if media.get("retail_from_ocr"):
        data["retail"] = media["retail_from_ocr"]
        if not data.get("price"):
            data["price"] = data["retail"]

    price = data.get("price")
    discount = data.get("discount", 0)
    retail = data.get("retail", 0.0)
    sizes = data.get("sizes", "")
    season = data.get("season", "")

    if price is None:
        await msg.answer("Не нашла цену в тексте/на фото. Укажи число перед € (например, <code>650€ -35%</code>).")
        del last_media[chat_id]
        return

    final_price = calc_fn(price, discount)
    result_msg = tpl_fn(final_price, retail, sizes, season, mode_label=label)

    await bot.send_message(TARGET_CHAT_ID, result_msg)

    file_ids = media.get("file_ids") or []
    if file_ids:
        await bot.send_photo(TARGET_CHAT_ID, file_ids[0], caption=result_msg)

    if media.get("is_pricetag"):
        try:
            await bot.delete_message(chat_id, media["message_id"])
        except Exception:
            if PRICETAG_NOTICE:
                await msg.answer("⚠️ Не удалось удалить фото ценника: у бота нет права «Удалять сообщения».")
    else:
        if PRICETAG_NOTICE and media.get("retail_from_ocr"):
            reasons = []
            if not media.get("euro_present"):
                reasons.append("нет символа €")
            if not media.get("keyword_present"):
                reasons.append("нет слова prezzo/price/retail")
            if reasons:
                await msg.answer("⚠️ Фото не удалено: " + "; ".join(reasons) + ".")

    del last_media[chat_id]

# ===================== ЗАПУСК =================
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
