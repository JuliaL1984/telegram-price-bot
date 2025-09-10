# bot.py — мгновенная публикация, альбомы, OCR-удаление ценников, 30+ команд
import os
import re
import asyncio
import tempfile
from typing import Dict, Callable, Optional, Tuple, List

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, InputMediaPhoto
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command

# OCR
import pytesseract
from PIL import Image

BOT_VERSION = "instant-album-ocr-modes-v1"

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1002973176038"))  # куда публикуем
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

# небольшая тех. задержка, чтобы Телеграм успел прислать все части альбома
ALBUM_SETTLE_MS = int(os.getenv("ALBUM_SETTLE_MS", "900"))

# язык(и) для Tesseract: например "eng" или "eng+ita"
TESS_LANG = os.getenv("TESS_LANG", "eng+ita")
# показывать ли мягкое предупреждение, если удаление не удалось
PRICETAG_NOTICE = os.getenv("PRICETAG_NOTICE", "1") == "1"

# ===================== ИНИЦИАЛИЗАЦИЯ =================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Буфер альбомов: ключ (chat_id, media_group_id) -> {
#   "file_ids":[], "message_ids":[], "caption":str, "task": Task, "any_pricetag": bool, "user_id": int
# }
albums: Dict[Tuple[int, str], Dict] = {}

# Текущий режим пользователя
active_mode: Dict[int, str] = {}  # user_id -> mode_key

# ===================== УТИЛИТЫ =====================
def round_price(value: float) -> int:
    return int(round(value, 0))

def default_calc(price: float, discount: int) -> int:
    """Базовая формула: сначала скидка, затем наценка по диапазону."""
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
        f"{sizes or ''}\n"
        f"{season or ''}"
    ).strip()

def cleanup_text_basic(text: str) -> str:
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"(?i)\b(Gucci|Prada|Louis\s*Vuitton|LV|Stone\s*Island|Balenciaga|Bally|Jimmy\s*Choo)\b", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def parse_input(text: str):
    """
    Достаём из подписи:
      price  — число перед €
      discount — -NN%
      retail — 'Retail price NNN'
      sizes  — строка с XS/S/M/L/XL…
      season — FWxx/xx или SSxx
    """
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

def is_admin(user_id: int) -> bool:
    return (not ADMINS) or (user_id in ADMINS)

# ===================== РЕЖИМЫ (30+ команд) =====================
def mk_mode(label: str,
            calc: Callable[[float, int], int] = default_calc,
            template: Callable[[int, float, str, str, Optional[str]], str] = default_template):
    return {"label": label, "calc": calc, "template": template}

MODES: Dict[str, Dict] = {
    # БАЗОВЫЕ
    "sale":     mk_mode("SALE"),
    "lux":      mk_mode("LUX"),
    "outlet":   mk_mode("OUTLET"),
    "stock":    mk_mode("STOCK"),
    "newfw":    mk_mode("NEW FW"),
    "newss":    mk_mode("NEW SS"),

    # СУМКИ
    "bags10":   mk_mode("BAGS -10%"),
    "bags15":   mk_mode("BAGS -15%"),
    "bags20":   mk_mode("BAGS -20%"),
    "bags25":   mk_mode("BAGS -25%"),
    "bags30":   mk_mode("BAGS -30%"),
    "bags40":   mk_mode("BAGS -40%"),

    # ОБУВЬ
    "shoes10":  mk_mode("SHOES -10%"),
    "shoes20":  mk_mode("SHOES -20%"),
    "shoes30":  mk_mode("SHOES -30%"),
    "shoes40":  mk_mode("SHOES -40%"),

    # ОДЕЖДА
    "rtw10":    mk_mode("RTW -10%"),
    "rtw20":    mk_mode("RTW -20%"),
    "rtw30":    mk_mode("RTW -30%"),
    "rtw40":    mk_mode("RTW -40%"),

    # АКСЕССУАРЫ
    "acc10":    mk_mode("ACCESSORIES -10%"),
    "acc20":    mk_mode("ACCESSORIES -20%"),
    "acc30":    mk_mode("ACCESSORIES -30%"),

    # МУЖ/ЖЕН
    "men":      mk_mode("MEN"),
    "women":    mk_mode("WOMEN"),

    # СПЕЦ. СХЕМЫ
    "vip":      mk_mode("VIP"),
    "promo":    mk_mode("PROMO"),
    "flash":    mk_mode("FLASH"),
    "bundle":   mk_mode("BUNDLE"),
    "limited":  mk_mode("LIMITED"),

    # РЕЗЕРВ (5 слотов под будущее)
    "m1": mk_mode("M1"),
    "m2": mk_mode("M2"),
    "m3": mk_mode("M3"),
    "m4": mk_mode("M4"),
    "m5": mk_mode("M5"),
}

# ===================== РАСЧЁТ/ШАБЛОН ПО РЕЖИМУ =====================
def build_result_text(user_id: int, raw_text: str) -> Optional[str]:
    cleaned = cleanup_text_basic(raw_text.strip())
    data = parse_input(cleaned)
    if data.get("price") is None:
        return None
    mode_key = active_mode.get(user_id, "sale")
    mode = MODES.get(mode_key, MODES["sale"])
    final_price = mode["calc"](data["price"], data.get("discount", 0))
    return mode["template"](
        final_price,
        data.get("retail", 0.0),
        data.get("sizes", ""),
        data.get("season", ""),
        mode_label=mode["label"],
    )

# ===================== ПУБЛИКАЦИЯ В КАНАЛ =====================
async def publish_to_target(file_ids: List[str], caption: str):
    """1 фото — sendPhoto; альбом — sendMediaGroup (подпись у первого кадра)."""
    if not file_ids:
        return
    if len(file_ids) == 1:
        await bot.send_photo(TARGET_CHAT_ID, file_ids[0], caption=caption)
    else:
        media = [InputMediaPhoto(media=file_ids[0], caption=caption, parse_mode=ParseMode.HTML)]
        media += [InputMediaPhoto(media=fid) for fid in file_ids[1:]]
        await bot.send_media_group(TARGET_CHAT_ID, media)

# ===================== OCR: ЦЕННИКИ =====================
def looks_like_pricetag(txt: str) -> bool:
    """Строгое правило: есть '€' и слово 'prezzo'/'price'/'retail'."""
    if not txt:
        return False
    t = txt.lower()
    return ("€" in t) and (("prezzo" in t) or ("price" in t) or ("retail" in t))

async def ocr_text_from_message(msg: Message) -> str:
    """Распознать текст на фото (берём самое большое превью из message.photo)."""
    file_id = msg.photo[-1].file_id
    tg_file = await bot.get_file(file_id)
    with tempfile.TemporaryDirectory() as td:
        local_path = os.path.join(td, "img.jpg")
        await bot.download_file(tg_file.file_path, destination=local_path)
        img = Image.open(local_path)
        return pytesseract.image_to_string(img, lang=TESS_LANG).strip()

async def is_msg_pricetag(msg: Message) -> bool:
    try:
        txt = await ocr_text_from_message(msg)
        return looks_like_pricetag(txt)
    except Exception:
        return False

async def try_delete_messages(chat_id: int, message_ids: List[int], notify: Optional[Message] = None):
    """Удаляем исходные сообщения (только если у бота есть такие права в этом чате)."""
    failed = False
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            failed = True
    if failed and PRICETAG_NOTICE and notify:
        await notify.answer("⚠️ Обнаружен ценник. Не удалось удалить сообщение(я): проверь права бота «Удалять сообщения» в этом чате.")

# ===================== КОМАНДЫ =====================
@router.message(Command(commands=list(MODES.keys())))
async def set_mode(msg: Message):
    user_id = msg.from_user.id
    cmd = msg.text.lstrip("/").split()[0]
    if not is_admin(user_id):
        return await msg.answer("⛔ Доступно только администраторам.")
    active_mode[user_id] = cmd
    await msg.answer(f"✅ Режим <b>{MODES[cmd]['label']}</b> активирован.")

@router.message(Command("mode"))
async def show_mode(msg: Message):
    user_id = msg.from_user.id
    mode = active_mode.get(user_id, "sale")
    await msg.answer(f"Текущий режим: <b>{MODES.get(mode, {}).get('label', mode)}</b>")

@router.message(Command("help"))
async def show_help(msg: Message):
    await msg.answer(
        "<b>Как работать:</b>\n"
        "• Отправь <u>фото с подписью</u> (например: <code>650€ -35%</code>) — публикация мгновенно.\n"
        "• Альбомы (несколько фото) поддерживаются — подпись у альбома, публикация одним постом.\n"
        "• Фото-ценник (OCR: есть € и слово prezzo/price/retail) — не публикуется, бот пытается удалить.\n"
        "• Режимы включаются командами: /sale /lux /bags30 /shoes20 …  Текущий см. /mode\n"
        f"• Версия: <code>{BOT_VERSION}</code>"
    )

@router.message(Command("ping"))
async def ping(msg: Message):
    await msg.answer(f"pong {BOT_VERSION}")

# ===================== ОБРАБОТКА ФОТО =====================
@router.message(F.photo & (F.media_group_id == None))
async def handle_single_photo(msg: Message):
    """Одиночное фото: OCR → если ценник — удаляем; иначе публикуем ТОЛЬКО если есть валидная подпись."""
    chat_id = msg.chat.id
    file_id = msg.photo[-1].file_id
    caption = (msg.caption or "").strip()

    # 1) OCR: ценник?
    try:
        if await is_msg_pricetag(msg):
            await try_delete_messages(chat_id, [msg.message_id], notify=msg)
            return
    except Exception:
        # OCR не должен ломать основной поток
        pass

    # 2) Публикация только при корректной подписи
    if not caption:
        return await msg.answer("Добавь подпись с ценой (например: <code>650€ -35%</code>) и пришли фото снова.")
    result = build_result_text(msg.from_user.id, caption)
    if not result:
        return await msg.answer("Не нашла цену в подписи. Укажи число перед € (например: <code>650€ -35%</code>).")
    await publish_to_target([file_id], result)

@router.message(F.photo & F.media_group_id)
async def handle_album_photo(msg: Message):
    """Альбом: собираем кадры; если в любом кадре OCR-ценник — удаляем весь набор; иначе публикуем с подписью альбома."""
    chat_id = msg.chat.id
    mgid = str(msg.media_group_id)
    key = (chat_id, mgid)

    file_id = msg.photo[-1].file_id
    caption = (msg.caption or "").strip()

    bucket = albums.get(key)
    if not bucket:
        bucket = {
            "file_ids": [],
            "message_ids": [],
            "caption": "",
            "task": None,
            "any_pricetag": False,
            "user_id": msg.from_user.id,
        }
        albums[key] = bucket

    # OCR: если хотя бы один кадр — ценник, помечаем
    if not bucket["any_pricetag"]:
        try:
            if await is_msg_pricetag(msg):
                bucket["any_pricetag"] = True
        except Exception:
            pass

    bucket["file_ids"].append(file_id)
    bucket["message_ids"].append(msg.message_id)
    if caption and not bucket["caption"]:
        bucket["caption"] = caption  # берём первую подпись
    # перезапускаем короткую «сборку» альбома
    if bucket["task"]:
        bucket["task"].cancel()

    async def _flush():
        await asyncio.sleep(ALBUM_SETTLE_MS / 1000)
        files = bucket["file_ids"]
        mids = bucket["message_ids"]
        cap = bucket["caption"]
        uid = bucket["user_id"]
        albums.pop(key, None)

        if bucket["any_pricetag"]:
            await try_delete_messages(chat_id, mids, notify=msg)
            return

        if not cap:
            return await bot.send_message(chat_id, "Добавь подпись к альбому (например: <code>650€ -35%</code>) и пришли снова.")

        result = build_result_text(uid, cap)
        if not result:
            return await bot.send_message(chat_id, "Не нашла цену в подписи альбома. Пример: <code>650€ -35%</code>.")
        await publish_to_target(files, result)

    bucket["task"] = asyncio.create_task(_flush())

# ===================== ЗАПУСК =====================
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
