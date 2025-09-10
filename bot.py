# bot.py — мгновенная публикация (без ожидания 8 сек) + режимы + /ping
import os
import re
import asyncio
from typing import Dict, Callable, Optional
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command

# ======== версия сборки (для проверки /ping) ========
BOT_VERSION = "instant-v1"

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1002973176038"))  # куда публикуем
# Окно, в течение которого можно прислать текст ПОСЛЕ фото (ожидания нет — просто «связка»)
PAIR_WINDOW_SECONDS = int(os.getenv("PAIR_WINDOW_SECONDS", "300"))   # 5 минут

# Админы (кто может переключать режимы)
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

# ===================== ИНИЦИАЛИЗАЦИЯ =================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Память для «фото → подпись»
last_media: Dict[int, Dict] = {}     # по chat.id: {"ts": dt, "file_id": str}

# ===================== ВСПОМОГАТЕЛЬНОЕ =================
def round_price(v: float) -> int:
    return int(round(v, 0))

def default_calc(price: float, discount: int) -> int:
    """Базовая формула: сначала скидка, потом фикс. наценка по диапазонам."""
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
      price  — первое число перед €
      discount — -..%
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

# ===================== РЕЕСТР РЕЖИМОВ =================
def mk_mode(label: str,
            calc: Callable[[float, int], int] = default_calc,
            template: Callable[[int, float, str, str, Optional[str]], str] = default_template):
    return {"label": label, "calc": calc, "template": template}

MODES: Dict[str, Dict] = {
    # БАЗОВЫЕ
    "sale": mk_mode("SALE"),
    "lux": mk_mode("LUX"),
    "outlet": mk_mode("OUTLET"),
    "stock": mk_mode("STOCK"),
    "newfw": mk_mode("NEW FW"),
    "newss": mk_mode("NEW SS"),
    # СУМКИ
    "bags10": mk_mode("BAGS -10%"), "bags15": mk_mode("BAGS -15%"),
    "bags20": mk_mode("BAGS -20%"), "bags25": mk_mode("BAGS -25%"),
    "bags30": mk_mode("BAGS -30%"), "bags40": mk_mode("BAGS -40%"),
    # ОБУВЬ
    "shoes10": mk_mode("SHOES -10%"), "shoes20": mk_mode("SHOES -20%"),
    "shoes30": mk_mode("SHOES -30%"), "shoes40": mk_mode("SHOES -40%"),
    # ОДЕЖДА
    "rtw10": mk_mode("RTW -10%"), "rtw20": mk_mode("RTW -20%"),
    "rtw30": mk_mode("RTW -30%"), "rtw40": mk_mode("RTW -40%"),
    # АКСЕССУАРЫ
    "acc10": mk_mode("ACCESSORIES -10%"), "acc20": mk_mode("ACCESSORIES -20%"), "acc30": mk_mode("ACCESSORIES -30%"),
    # М/Ж
    "men": mk_mode("MEN"), "women": mk_mode("WOMEN"),
    # РЕЗЕРВ
    "m1": mk_mode("M1"), "m2": mk_mode("M2"), "m3": mk_mode("M3"), "m4": mk_mode("M4"), "m5": mk_mode("M5"),
}

def is_admin(user_id: int) -> bool:
    return (not ADMINS) or (user_id in ADMINS)

# ===================== КОМАНДЫ =====================
@router.message(Command(commands=list(MODES.keys())))
async def set_mode(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.answer("⛔ Доступно только администраторам.")
    cmd = msg.text.lstrip("/").split()[0]
    msg.conf["mode_key"] = cmd  # локально
    await msg.answer(f"✅ Режим <b>{MODES[cmd]['label']}</b> активирован.\nПришлите фото с подписью или фото → текст.")

# Показ текущего режима (в простом виде по пользователю не храним — страдает только текст)
@router.message(Command("mode"))
async def show_mode(msg: Message):
    await msg.answer("Режимы: /sale /lux /bags30 /shoes20 …  (включай нужный перед загрузкой)")

@router.message(Command("help"))
async def show_help(msg: Message):
    await msg.answer(
        "<b>Как работать:</b>\n"
        "• Отправь <b>фото с подписью</b> (например: <code>650€ -35%</code>), — бот опубликует сразу.\n"
        "• Или отправь <b>сначала фото</b>, а затем текст — связка доступна в течение 5 минут.\n"
        "• Режимы включаются командами: /sale /lux /bags30 /shoes20 и т.д.\n"
        "• Проверка версии: /ping"
    )

@router.message(Command("ping"))
async def ping(msg: Message):
    await msg.answer(f"pong {BOT_VERSION}")

# ===================== ПУБЛИКАЦИЯ =====================
async def publish_result(data: Dict[str, Optional[str]], label: Optional[str],
                         file_id: Optional[str]) -> None:
    """Сборка текста и отправка в целевую группу (с фото или без)."""
    price = data.get("price")
    discount = data.get("discount", 0)
    retail = data.get("retail", 0.0)
    sizes = data.get("sizes", "")
    season = data.get("season", "")

    if price is None:
        # Мягкая подсказка (в личку отправителю — не в группу)
        return

    # Выбор режима: если label есть — ищем его, иначе базовый
    mode = None
    if label:
        # ищем ключ по надписи (редко используется, можно упростить)
        mode = next((m for m in MODES.values() if m["label"] == label), None)
    if not mode:
        mode = MODES["sale"]

    calc_fn = mode["calc"]
    tpl_fn = mode["template"]

    final_price = calc_fn(price, discount or 0)
    result_msg = tpl_fn(final_price, retail, sizes, season, mode_label=mode["label"])

    if file_id:
        await bot.send_photo(TARGET_CHAT_ID, file_id, caption=result_msg)
    else:
        await bot.send_message(TARGET_CHAT_ID, result_msg)

# ===================== ХЕНДЛЕРЫ КОНТЕНТА =================
@router.message(F.photo)
async def handle_photo(msg: Message):
    """
    1) Если в подписи есть цена — публикуем СРАЗУ (без ожидания).
    2) Если подписи нет — запомним file_id на 5 минут, чтобы связать со следующим текстом.
    """
    file_id = msg.photo[-1].file_id
    caption = (msg.caption or "").strip()

    if caption:
        cleaned = cleanup_text_basic(caption)
        data = parse_input(cleaned)
        # Публикация сразу
        await publish_result(data, label=None, file_id=file_id)
    else:
        # Сохраняем для связки «фото → потом текст»
        last_media[msg.chat.id] = {"ts": datetime.now(), "file_id": file_id}
        await msg.answer("Ок, жду текст к фото (например: 650€ -35%).")

@router.message(F.text)
async def handle_text(msg: Message):
    """
    Если прямо перед этим прислали фото без подписи — свяжем и опубликуем.
    Если фото не было — ничего не делаем (или можно отправить подсказку).
    """
    media = last_media.get(msg.chat.id)
    cleaned = cleanup_text_basic(msg.text.strip())
    data = parse_input(cleaned)

    # есть запомненное фото в окне 5 минут?
    if media and (datetime.now() - media["ts"] <= timedelta(seconds=PAIR_WINDOW_SECONDS)):
        await publish_result(data, label=None, file_id=media["file_id"])
        del last_media[msg.chat.id]
        return

    # если фото не было — можно подсказать:
    await msg.answer("Пришли фото с подписью (пример: <code>650€ -35%</code>), и я опубликую.")

# ===================== ЗАПУСК =================
async def main():
    # сбрасываем вебхук и пустые апдейты
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
