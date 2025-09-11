# bot.py — альбомы, 30+ режимов, OCR ценника, дебаунс 0.8с для альбомов,
# подсказки при отсутствии цены и поддержка "альбом/фото → отдельный текст"

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

ALBUM_SETTLE_MS = int(os.getenv("ALBUM_SETTLE_MS", "800"))
ALBUM_WINDOW_SECONDS = int(os.getenv("ALBUM_WINDOW_SECONDS", "30"))

OCR_ENABLED = os.getenv("OCR_ENABLED", "1") == "1"
OCR_LANG = os.getenv("OCR_LANG", "ita+eng")

# ====== ИНИЦИАЛИЗАЦИЯ ======
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ====== ПАМЯТЬ ======
last_media: Dict[int, Dict] = {}             # {chat_id: {"ts", "file_ids", "caption", "mgid"}}
active_mode: Dict[int, str] = {}             # {user_id: "mode"}
album_buffers: Dict[Tuple[int, str], Dict] = {}  # {(chat_id, media_group_id): {"items", "caption", "task", "user_id"}}

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
        "• Фото/альбом без цены — ждём твой текст (до 30с).\n"
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

# ====== ХЕЛПЕР для ожидания текста ======
async def _remember_media_for_text(chat_id: int, file_ids: List[str], mgid: Optional[str] = None, caption: str = ""):
    last_media[chat_id] = {
        "ts": datetime.now(),
        "file_ids": file_ids,
        "caption": caption or "",
        "mgid": mgid or "",
    }

# ====== ХЕНДЛЕРЫ ======

# 1) Одиночное фото
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

    # Цены нет — ждём текст
    await _remember_media_for_text(msg.chat.id, [fid], caption=caption)
    try:
        await msg.answer("Добавь текст с ценой/скидкой (например: 650€ -35%) — опубликую одним постом.")
    except Exception:
        pass

# 2) Альбом: собираем кадры; если подписи нет — ждём текст
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
        caption = data["caption"]
        user_id = data["user_id"]

        # порядок кадров
        items.sort(key=lambda x: x["mid"])
        idx_cap = next((i for i, it in enumerate(items) if it["cap"]), None)
        if idx_cap not in (None, 0):
            items.insert(0, items.pop(idx_cap))

        file_ids = [it["fid"] for it in items]

        if caption:
            result = build_result_text(user_id, caption)
            if result:
                fids = await filter_pricetag_photos(file_ids)
                await publish_to_target(fids, result)
                return

        await _remember_media_for_text(chat_id, file_ids, mgid=mgid, caption=caption)
        # Подсказку шлем только если ещё нет свежего ожидания текста, чтобы не дублировать
        lm = last_media.get(chat_id)
        if not (lm and (datetime.now() - lm["ts"] <= timedelta(seconds=ALBUM_WINDOW_SECONDS))):
            try:
                await bot.send_message(chat_id, "Добавь текст с ценой/скидкой (например: 650€ -35%) — опубликую альбом одним постом.")
            except Exception:
                pass

    buf["task"] = asyncio.create_task(_flush_album())

# 3) Текст под последними фото/альбомом (с правильным сопоставлением и защитой от гонки)
@router.message(F.text)
async def handle_text(msg: Message):
    chat_id, user_id = msg.chat.id, msg.from_user.id

    # Вариант 1: уже есть сохранённые медиа
    bucket = last_media.get(chat_id)
    if bucket and (datetime.now() - bucket["ts"] <= timedelta(seconds=ALBUM_WINDOW_SECONDS)):
        raw_text = (bucket.get("caption") or "")
        if raw_text:
            raw_text += "\n"
        raw_text += (msg.text or "")

        result = build_result_text(user_id, raw_text)
        fids = await filter_pricetag_photos(bucket.get("file_ids") or [])

        if result:
            await publish_to_target(fids, result)
        else:
            await publish_to_target(fids, f"⚠️ Не нашла цену в тексте. Пример: 650€ -35%\n\n{msg.text}")

        del last_media[chat_id]
        return

    # Вариант 2: альбом ещё в буфере (гонка). Выберем тот, чей последний кадр <= id этого текста
    cand = [(k, v) for (k, v) in album_buffers.items() if k[0] == chat_id and v.get("items")]
    if cand:
        def last_mid(buf): return max(it["mid"] for it in buf["items"])
        cand.sort(key=lambda kv: last_mid(kv[1]))
        eligible = [kv for kv in cand if last_mid(kv[1]) <= msg.message_id]
        key, data = (eligible[-1] if eligible else cand[-1])

        items = data["items"]
        caption = (data.get("caption") or "")
        if caption:
            caption += "\n"
        caption += (msg.text or "")

        # порядок кадров
        items.sort(key=lambda x: x["mid"])
        idx_cap = next((i for i, it in enumerate(items) if it["cap"]), None)
        if idx_cap not in (None, 0):
            items.insert(0, items.pop(idx_cap))

        file_ids = [it["fid"] for it in items]
        result = build_result_text(user_id, caption)
        fids = await filter_pricetag_photos(file_ids)

        # отменяем отложенный слив и убираем буфер
        if data.get("task"):
            data["task"].cancel()
        album_buffers.pop(key, None)

        if result:
            await publish_to_target(fids, result)
        else:
            await publish_to_target(fids, f"⚠️ Не нашла цену в тексте. Пример: 650€ -35%\n\n{msg.text}")
        return

    # Вариант 3: ничего не нашли — игнор
    return

# ====== ЗАПУСК ======
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
