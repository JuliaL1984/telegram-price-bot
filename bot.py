# bot.py — мгновенная обработка фото с подписью + без таймера + поддержка альбомов (media_group)
import os
import re
import asyncio
import tempfile
from typing import Dict, Callable, Optional, List, Tuple
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, InputMediaPhoto
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1002973176038"))  # куда публикуем
# админы, кто может переключать режимы
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

# ===================== ИНИЦИАЛИЗАЦИЯ =================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Храним последнее одиночное фото по чату (если пришло без подписи)
last_single: Dict[int, Dict] = {}  # chat_id -> { "file_ids":[str], "ts":dt }

# Буферы альбомов: ключ = (chat_id, media_group_id)
album_buffers: Dict[Tuple[int, str], Dict] = {}
ALBUM_MAX_AGE = timedelta(minutes=3)   # страховка от «висяков»

# ===================== ВСПОМОГАТЕЛЬНОЕ =================
def round_price(value: float) -> int:
    return int(round(value, 0))

def default_calc(price: float, discount: int) -> int:
    """
    Твоя базовая формула:
    - сначала скидка, затем наценка по диапазону.
    """
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

def parse_input(text: str) -> Dict[str, Optional[str]]:
    """
    Достаём:
      price — число перед €
      discount — -NN%
      retail — 'Retail price NNN'
      sizes — строка с размерами (XS/S/M/L/XL/XXL…)
      season — FWxx/xx или SSxx
    Если retail не нашли — берём равным price.
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

# ===================== ПУБЛИКАЦИЯ =================
async def build_result_text(user_id: int, raw_text: str) -> Optional[str]:
    """Считает по активному режиму и собирает текст поста."""
    mode_key = active_mode.get(user_id, "sale")
    mode = MODES.get(mode_key, MODES["sale"])
    label = mode["label"]
    calc_fn = mode["calc"]
    tpl_fn = mode["template"]

    cleaned = cleanup_text_basic(raw_text.strip())
    data = parse_input(cleaned)

    price = data.get("price")
    if price is None:
        return None

    discount = data.get("discount", 0)
    retail = data.get("retail", 0.0)
    sizes = data.get("sizes", "")
    season = data.get("season", "")

    final_price = calc_fn(price, discount)
    return tpl_fn(final_price, retail, sizes, season, mode_label=label)

async def post_single(file_id: str, caption: str):
    """Публикация одного фото + подпись."""
    await bot.send_message(TARGET_CHAT_ID, caption)
    await bot.send_photo(TARGET_CHAT_ID, file_id, caption=caption)

async def post_album(file_ids: List[str], caption: str):
    """Публикация альбома (media group) с подписью на первом фото."""
    media = []
    for i, fid in enumerate(file_ids):
        if i == 0:
            media.append(InputMediaPhoto(media=fid, caption=caption, parse_mode="HTML"))
        else:
            media.append(InputMediaPhoto(media=fid))
    await bot.send_media_group(TARGET_CHAT_ID, media)

# ===================== КОМАНДЫ =================
@router.message(Command(commands=list(MODES.keys())))
async def set_mode(msg: Message):
    user_id = msg.from_user.id
    cmd = msg.text.lstrip("/").split()[0]
    if not is_admin(user_id):
        return await msg.answer("⛔ Доступно только администраторам.")
    active_mode[user_id] = cmd
    label = MODES[cmd]["label"]
    await msg.answer(f"✅ Режим <b>{label}</b> активирован.\nМожно отправлять фото С ПОДПИСЬЮ или фото → текст.")

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
        "• Отправь <u>фото с подписью</u> (цена/скидка/размеры/сезон) — бот обработает СРАЗУ.\n"
        "• Или: фото → следом текст (без ожидания).\n"
        "• Можно отправлять <u>альбомом</u> — подпись у альбома или следующий текст.\n\n"
        "<b>Режимы:</b> /sale /lux /bags30 /shoes20 …  Текущий режим смотри: /mode\n"
    )
    await msg.answer(txt)

# ===================== ОБРАБОТКА ФОТО =================
# 1) ОДИНОЧНОЕ ФОТО (без media_group_id)
@router.message(F.photo & (F.media_group_id == None))
async def handle_single_photo(msg: Message):
    chat_id = msg.chat.id
    file_id = msg.photo[-1].file_id
    caption = (msg.caption or "").strip()

    # Фото пришло С ПОДПИСЬЮ — считаем и публикуем сразу
    if caption:
        result = await build_result_text(msg.from_user.id, caption)
        if not result:
            return await msg.answer("Не нашла цену в подписи. Формат: <code>650€ -35%</code> и т.д.")
        await post_single(file_id, result)
        return

    # Фото без подписи — ждём текст (без таймера)
    last_single[chat_id] = {"file_ids": [file_id], "ts": datetime.now()}

# 2) ЧАСТЬ АЛЬБОМА (есть media_group_id)
@router.message(F.photo & F.media_group_id)
async def handle_album_photo(msg: Message):
    chat_id = msg.chat.id
    mgid = str(msg.media_group_id)
    file_id = msg.photo[-1].file_id
    caption = (msg.caption or "").strip()

    key = (chat_id, mgid)
    buf = album_buffers.get(key)
    if not buf:
        buf = {
            "file_ids": [],
            "caption": "",
            "ts": datetime.now(),
        }
        album_buffers[key] = buf

    buf["file_ids"].append(file_id)
    if caption and not buf["caption"]:
        buf["caption"] = caption
    buf["ts"] = datetime.now()

# ===================== ОБРАБОТКА ТЕКСТА =================
@router.message(F.text)
async def handle_text(msg: Message):
    chat_id = msg.chat.id
    user_id = msg.from_user.id
    text = msg.text.strip()

    # 1) если есть незакрытый альбом — используем его
    #    берём самый свежий альбом этого чата
    fresh_key = None
    fresh_ts = None
    for key, buf in list(album_buffers.items()):
        c_id, _ = key
        if c_id != chat_id:
            continue
        if fresh_ts is None or buf["ts"] > fresh_ts:
            fresh_key = key
            fresh_ts = buf["ts"]

    if fresh_key:
        buf = album_buffers.pop(fresh_key)
        file_ids = buf["file_ids"]
        # подпись: приоритет — подпись альбома, иначе текст пользователя
        raw_text = buf["caption"] or text
        result = await build_result_text(user_id, raw_text)
        if not result:
            return await msg.answer("Не нашла цену. Формат: <code>650€ -35%</code> и т.д.")
        if len(file_ids) == 1:
            await post_single(file_ids[0], result)
        else:
            await post_album(file_ids, result)
        return

    # 2) если до этого было одиночное фото без подписи — склеиваем с текстом
    media = last_single.get(chat_id)
    if media:
        file_ids = media.get("file_ids") or []
        last_single.pop(chat_id, None)

        result = await build_result_text(user_id, text)
        if not result:
            return await msg.answer("Не нашла цену. Формат: <code>650€ -35%</code> и т.д.")
        if file_ids:
            await post_single(file_ids[0], result)
        else:
            await bot.send_message(TARGET_CHAT_ID, result)
        return

    # 3) просто текст без фото — подскажем формат
    await msg.answer("Пришли фото с подписью (<code>650€ -35%</code>) или фото → затем текст.")

# ===================== ГИГИЕНА БУФЕРА АЛЬБОМОВ =================
async def cleanup_albums():
    """Периодически удаляем старые незавершённые альбомы (на всякий случай)."""
    while True:
        now = datetime.now()
        for key, buf in list(album_buffers.items()):
            if now - buf.get("ts", now) > ALBUM_MAX_AGE:
                album_buffers.pop(key, None)
        await asyncio.sleep(30)

# ===================== ЗАПУСК =================
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    # стартуем периодическую очистку буферов
    asyncio.create_task(cleanup_albums())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
