# bot.py — альбомы, 30+ режимов, OCR ценника, дебаунс 0.8с для альбомов,
# подсказки при отсутствии цены и поддержка "альбом/фото → отдельный текст"
# + Гарантия порядка публикаций через FIFO-очередь + КНОПКА "Написать" с авто-пересылкой админу

import os
import re
import io
import asyncio
from typing import Dict, Callable, Optional, List, Tuple
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, InputMediaPhoto,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from collections import defaultdict

# ====== НАСТРОЙКИ ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1002973176038"))
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

ALBUM_SETTLE_MS = int(os.getenv("ALBUM_SETTLE_MS", "800"))
ALBUM_WINDOW_SECONDS = int(os.getenv("ALBUM_WINDOW_SECONDS", "30"))

OCR_ENABLED = os.getenv("OCR_ENABLED", "1") == "1"
OCR_LANG = os.getenv("OCR_LANG", "ita+eng")

# ====== КОНТАКТ-КНОПКИ/ПЕРЕСЫЛКА ======
# Ваши значения уже подставлены; при желании можете вынести в переменные окружения Render
ADMIN_FORWARD_ID = int(os.getenv("ADMIN_FORWARD_ID", "1037914226"))     # ваш личный ID
MANAGER_USERNAME = os.getenv("MANAGER_USERNAME", "julia_fashionshop")  # без @

# item_id -> [message_id...]; item_id -> chat_id где лежит карточка
ITEM_MESSAGES: Dict[str, List[int]] = defaultdict(list)
ITEM_CHAT_ID: Dict[str, int] = {}
_item_seq = 0  # счётчик для уникальных item_id

# ====== ИНИЦИАЛИЗАЦИЯ ======
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ====== ПАМЯТЬ ======
last_media: Dict[int, Dict] = {}             # {chat_id: {"ts", "file_ids", "caption", "mgid"}}
active_mode: Dict[int, str] = {}             # {user_id: "mode"}
album_buffers: Dict[Tuple[int, str], Dict] = {}  # {(chat_id, media_group_id): {"items", "caption", "task", "user_id"}}

# ====== КНОПКИ ======
def get_contact_kb(item_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать", callback_data=f"contact_{item_id}")]
    ])

def get_open_chat_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть чат с менеджером", url=f"https://t.me/{MANAGER_USERNAME}")]
    ])

@router.callback_query(F.data.startswith("contact_"))
async def on_contact_click(cb: CallbackQuery):
    item_id = cb.data.split("contact_", 1)[1]

    # 1) Пересылаем админу ВСЕ сообщения карточки (альбом/фото + подпись)
    try:
        chat_src = ITEM_CHAT_ID.get(item_id, cb.message.chat.id)
        mids = ITEM_MESSAGES.get(item_id, [cb.message.message_id])
        for mid in mids:
            await bot.forward_message(chat_id=ADMIN_FORWARD_ID, from_chat_id=chat_src, message_id=mid)
    except Exception:
        pass

    # 2) Клиенту — кнопка открыть чат с вами
    try:
        await cb.message.answer(
            "✅ Товар отправлен менеджеру. Нажмите ниже, чтобы сразу написать:",
            reply_markup=get_open_chat_kb()
        )
    except Exception:
        pass
    await cb.answer()

# ====== ОЧЕРЕДЬ ПУБЛИКАЦИЙ (FIFO) ======
# Очередь хранит (file_ids, caption, item_id)
publish_queue: "asyncio.Queue[Tuple[List[str], str, str]]" = asyncio.Queue()

async def _do_publish(file_ids: List[str], caption: str, item_id: str):
    """Реальная отправка сообщений (не вызывать напрямую)."""
    if not file_ids:
        return
    # OCR-фильтрация выполняется здесь, чтобы порядок сохранялся
    fids = await filter_pricetag_photos(file_ids)

    # Для ОДНОГО фото прикрепляем подпись и кнопку к самому фото.
    # Для АЛЬБОМА — шлём медиагруппу без подписи, потом отдельным сообщением текст + кнопка.
    if len(fids) == 1:
        msg = await bot.send_photo(
            TARGET_CHAT_ID,
            fids[0],
            caption=caption,
            reply_markup=get_contact_kb(item_id),
        )
        ITEM_MESSAGES[item_id].append(msg.message_id)
        ITEM_CHAT_ID[item_id] = TARGET_CHAT_ID
    else:
        sent = await bot.send_media_group(
            TARGET_CHAT_ID,
            [InputMediaPhoto(media=fids[0])] + [InputMediaPhoto(media=fid) for fid in fids[1:]]
        )
        for m in sent:
            ITEM_MESSAGES[item_id].append(m.message_id)
        ITEM_CHAT_ID[item_id] = TARGET_CHAT_ID

        cap_msg = await bot.send_message(
            TARGET_CHAT_ID,
            caption,
            reply_markup=get_contact_kb(item_id)
        )
        ITEM_MESSAGES[item_id].append(cap_msg.message_id)

async def publish_worker():
    """Единственный воркер, публикует все задачи по очереди."""
    while True:
        file_ids, caption, item_id = await publish_queue.get()
        try:
            await _do_publish(file_ids, caption, item_id)
        except Exception:
            pass
        finally:
            publish_queue.task_done()

async def publish_to_target(file_ids: List[str], caption: str):
    """Ставит публикацию в очередь (сохраняет порядок) + генерирует item_id."""
    global _item_seq
    _item_seq += 1
    item_id = f"it{int(datetime.now().timestamp())}-{_item_seq}"
    await publish_queue.put((file_ids, caption, item_id))

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
        "• Фото/альбом без цены — ждём твой текст (до 30 с).\n"
        "• Если текст пришёл — публикуем одним постом."
    )

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

# ====== ХЕЛПЕР: запомнить медиа и ждать текст ======
async def _remember_media_for_text(chat_id: int, file_ids: List[str], mgid: Optional[str] = None, caption: str = ""):
    last_media[chat_id] = {
        "ts": datetime.now(),
        "file_ids": file_ids,
        "caption": caption or "",
        "mgid": mgid or "",
    }

# ====== ХЕНДЛЕРЫ ======
@router.message(F.photo & (F.media_group_id == None))
async def handle_single_photo(msg: Message):
    fid = msg.photo[-1].file_id
    caption = (msg.caption or "").strip()

    if caption:
        result = build_result_text(msg.from_user.id, caption)
        if result:
            # Ставим публикацию в очередь — кнопка прикрепится автоматически
            await publish_to_target([fid], result)
            return

    await _remember_media_for_text(msg.chat.id, [fid], caption=caption)
    try:
        await msg.answer("Добавь текст с ценой/скидкой (например: 650€ -35%) — опубликую одним постом.")
    except Exception:
        pass

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

        # порядок сообщений альбома
        items.sort(key=lambda x: x["mid"])
        idx_cap = next((i for i, it in enumerate(items) if it["cap"]), None)
        if idx_cap not in (None, 0):
            items.insert(0, items.pop(idx_cap))

        file_ids = [it["fid"] for it in items]

        if caption:
            result = build_result_text(user_id, caption)
            if result:
                await publish_to_target(file_ids, result)
                return

        # ждём текст после альбома
        await _remember_media_for_text(chat_id, file_ids, mgid=mgid, caption=caption)
        lm = last_media.get(chat_id)
        if not (lm and (datetime.now() - lm["ts"] <= timedelta(seconds=ALBUM_WINDOW_SECONDS))):
            try:
                await bot.send_message(
                    chat_id,
                    "Добавь текст с ценой/скидкой (например: 650€ -35%) — опубликую альбом одним постом."
                )
            except Exception:
                pass

    buf["task"] = asyncio.create_task(_flush_album())

@router.message(F.text)
async def handle_text(msg: Message):
    chat_id, user_id = msg.chat.id, msg.from_user.id

    # Есть ли «свежие» медиа, которым ждём подпись?
    bucket = last_media.get(chat_id)
    if bucket and (datetime.now() - bucket["ts"] <= timedelta(seconds=ALBUM_WINDOW_SECONDS)):
        raw_text = (bucket.get("caption") or "")
        if raw_text:
            raw_text += "\n"
        raw_text += (msg.text or "")

        result = build_result_text(user_id, raw_text)
        file_ids = bucket.get("file_ids") or []

        if result:
            await publish_to_target(file_ids, result)
        else:
            await publish_to_target(file_ids, f"⚠️ Не нашла цену в тексте. Пример: 650€ -35%\n\n{msg.text}")

        del last_media[chat_id]
        return

    # Или есть незавершённый альбом?
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

        items.sort(key=lambda x: x["mid"])
        idx_cap = next((i for i, it in enumerate(items) if it["cap"]), None)
        if idx_cap not in (None, 0):
            items.insert(0, items.pop(idx_cap))

        file_ids = [it["fid"] for it in items]
        result = build_result_text(user_id, caption)

        if data.get("task"):
            data["task"].cancel()
        album_buffers.pop(key, None)

        if result:
            await publish_to_target(file_ids, result)
        else:
            await publish_to_target(file_ids, f"⚠️ Не нашла цену в тексте. Пример: 650€ -35%\n\n{msg.text}")
        return

    # обычный текст — игнорируем
    return

# ====== ЗАПУСК ======
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    # запускаем воркер очереди перед polling
    asyncio.create_task(publish_worker())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
