# bot.py — альбомы, 30+ режимов, OCR ценника, дебаунс 0.8с для альбомов,
# подсказки при отсутствии цены и поддержка "альбом → отдельный текст"

import os
import re
import io
import asyncio
from typing import Dict, Callable, Optional, List, Tuple
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

ALBUM_SETTLE_MS = int(os.getenv("ALBUM_SETTLE_MS", "800"))            # короткий дебаунс для альбомов
ALBUM_WINDOW_SECONDS = int(os.getenv("ALBUM_WINDOW_SECONDS", "30"))    # окно ожидания текста после альбома

OCR_ENABLED = os.getenv("OCR_ENABLED", "1") == "1"
OCR_LANG = os.getenv("OCR_LANG", "ita+eng")

# ====== ИНИЦИАЛИЗАЦИЯ ======
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ====== ПАМЯТЬ ======
# буфер для схемы "альбом → отдельный текст"
# last_media[chat_id] = {"ts": dt, "file_ids": [...], "caption": str, "mgid": str}
last_media: Dict[int, Dict] = {}

# активный режим по user_id
active_mode: Dict[int, str] = {}

# временный буфер прихода кадров одного альбома (до сливки)
# key = (chat_id, media_group_id)
album_buffers: Dict[Tuple[int, str], Dict] = {}

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

def default_template(final_price: int, retail: float, sizes: str, season: str, mode_label: Optional[str] = None) -> str:
    tag = f"<i>({mode_label})</i>\n" if mode_label else ""
    return (
        f"{tag}"
        f"✅ <b>{final_price}€</b>\n"
        f"❌ <b>Retail price {round_price(retail)}€</b>\n"
        f"{sizes}\n"
        f"{season}"
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

# ====== ПУБЛИКАЦИЯ ======
async def publish_to_target(file_ids: List[str], caption: str):
    if not file_ids:
        return
    if len(file_ids) == 1:
        await bot.send_photo(TARGET_CHAT_ID, file_ids[0], caption=caption)
    else:
        media = [InputMediaPhoto(media=file_ids[0], caption=caption, parse_mode=ParseMode.HTML)]
        media += [InputMediaPhoto(media=fid) for fid in file_ids[1:]]
        await bot.send_media_group(TARGET_CHAT_ID, media)

# ====== OCR ======
if OCR_ENABLED:
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        OCR_ENABLED = False

async def ocr_should_hide(file_id: str) -> bool:
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
    return res or (file_ids[:1])

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
        "Бот принимает фото/альбомы и текст с ценой.\n"
        "• Одиночное фото без цены — публикуется с подсказкой.\n"
        "• Альбом без подписи — временно копится и ждёт твой текст (до 30с).\n"
        "• Если текст пришёл — публикуем одним постом."
    )

@router.message(Command("ping"))
async def ping(msg: Message):
    await msg.answer("pong")

# ====== ВСПОМОГ.: сборка подписи по режиму ======
def build_result_text(user_id: int, caption: str) -> Optional[str]:
    cleaned = cleanup_text_basic(caption)
    data = parse_input(cleaned)
    price = data.get("price")
    if price is None:
        return None
    mode = MODES.get(active_mode.get(user_id, "sale"), MODES["sale"])
    calc_fn, tpl_fn, label = mode["calc"], mode["template"], mode["label"]
    final_price = calc_fn(price, data.get("discount", 0))
    return tpl_fn(final_price, data["retail"], data["sizes"], data["season"], mode_label=label)

# ====== ХЕНДЛЕРЫ ======

# 1) Одиночное фото: публикуем сразу; если нет цены — с подсказкой
@router.message(F.photo & (F.media_group_id == None))
async def handle_single_photo(msg: Message):
    fid = msg.photo[-1].file_id
    caption = (msg.caption or "").strip()

    if caption:
        result = build_result_text(msg.from_user.id, caption)
        if result:
            fids = await filter_pricetag_photos([fid])
            await publish_to_target(fids, result)
            return

    warn = "⚠️ Нет цены в подписи. Пример: 650€ -35%"
    final_caption = warn if not caption else f"{warn}\n\n{caption}"
    fids = await filter_pricetag_photos([fid])
    await publish_to_target(fids, final_caption)

# 2) Альбом: собираем кадры (0.8с); если подписи нет — ждём текст вместо публикации предупреждения
@router.message(F.photo & F.media_group_id)
async def handle_album_photo(msg: Message):
    chat_id, mgid = msg.chat.id, str(msg.media_group_id)
    key = (chat_id, mgid)

    fid, mid = msg.photo[-1].file_id, msg.message_id
    cap_text = (msg.caption or "").strip()
    has_cap = bool(cap_text)

    buf = album_buffers.get(key)
    if not buf:
        buf = {"items": [], "caption": "", "task": None, "user_id": msg.from_user.id}
        album_buffers[key] = buf

    buf["items"].append({"fid": fid, "mid": mid, "cap": has_cap})
    if has_cap and not buf["caption"]:
        buf["caption"] = cap_text

    if buf["task"]:
        buf["task"].cancel()

    async def _flush_album():
        await asyncio.sleep(ALBUM_SETTLE_MS / 1000)

        data = album_buffers.pop(key, None)
        if not data:
            return

        items = data["items"]
        caption = data["caption"]          # может быть пустой
        user_id = data["user_id"]

        # порядок кадров: по message_id; если был кадр с подписью — в начало
        items.sort(key=lambda x: x["mid"])
        idx_cap = next((i for i, it in enumerate(items) if it["cap"]), None)
        if idx_cap not in (None, 0):
            items.insert(0, items.pop(idx_cap))

        file_ids = [it["fid"] for it in items]

        # === НЕТ подписи в альбоме ===
        if not caption:
            # не публикуем предупреждение в группу — ждём твой текст
            last_media[chat_id] = {
                "ts": datetime.now(),
                "file_ids": file_ids,
                "caption": "",
                "mgid": mgid,
            }
            # и мягкая подсказка тебе в личку
            try:
                await msg.answer("Добавь текст с ценой/скидкой (например: 650€ -35%) — опубликую альбом одним постом.")
            except Exception:
                pass
            return

        # есть подпись внутри альбома
        result = build_result_text(user_id, caption)
        if not result:
            warn = f"⚠️ Не нашла цену в подписи альбома. Пример: 650€ -35%\n\n{caption}"
            fids = await filter_pricetag_photos(file_ids)
            await publish_to_target(fids, warn)
            return

        fids = await filter_pricetag_photos(file_ids)
        await publish_to_target(fids, result)

    buf["task"] = asyncio.create_task(_flush_album())

# 3) Текст: используется для кейса "альбом → отдельный текст"
@router.message(F.text)
async def handle_text(msg: Message):
    chat_id, user_id = msg.chat.id, msg.from_user.id
    bucket = last_media.get(chat_id)
    if not bucket:
        return

    # уважаем окно ожидания
    if datetime.now() - bucket["ts"] > timedelta(seconds=ALBUM_WINDOW_SECONDS):
        del last_media[chat_id]
        return

    # собираем исходную подпись (если была) + текущий текст
    raw_text = (bucket.get("caption") or "") + ("\n" if bucket.get("caption") else "") + (msg.text or "")
    result = build_result_text(user_id, raw_text)

    fids = await filter_pricetag_photos(bucket.get("file_ids") or [])
    if not result:
        warn = "⚠️ Не нашла цену в тексте. Пример: 650€ -35%"
        final_caption = f"{warn}\n\n{msg.text}"
        await publish_to_target(fids, final_caption)
    else:
        await publish_to_target(fids, result)

    del last_media[chat_id]

# ====== ЗАПУСК ======
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
