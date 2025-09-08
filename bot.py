import os, re, json, math, asyncio, time
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict, Any

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, ContentType, InlineKeyboardMarkup, InlineKeyboardButton

# ---------- Кнопка "Заказать" ----------
ORDER_URL = "https://t.me/julia_fashionshop"
order_kb = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="🛍 Заказать", url=ORDER_URL)]]
)

# ---------- ЛОКАЛЬНЫЙ OCR (Tesseract) ----------
import pytesseract
from PIL import Image
import cv2
import numpy as np

# ---------- Хранилище формулы ----------
FORMULA_PATH = "formula.json"
MEDIA_BUFFER: Dict[str, Dict[str, Any]] = {}

def load_formula() -> str:
    if os.path.exists(FORMULA_PATH):
        try:
            with open(FORMULA_PATH, "r", encoding="utf-8") as f:
                return json.load(f).get("formula", "")
        except Exception:
            return ""
    return ""

def save_formula(s: str):
    try:
        with open(FORMULA_PATH, "w", encoding="utf-8") as f:
            json.dump({"formula": s}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

CURRENT_FORMULA = load_formula()

# ---------- Регулярки ----------
PRICE_DISC_RX = re.compile(r'(?P<retail>\d+(?:[.,]\d+)?)\s*€\s*-\s*(?P<disc>\d{1,2})\s*%', re.I)
SEASON_RX     = re.compile(r'\b(?:NEW\s*)?(?:FW|SS)\s*\d{2}(?:/\d{2})?', re.I)
SIZES_RX      = re.compile(r'(?:XXXL|XXL|XS|XL|L|M|S)(?:/(?:XXXL|XXL|XS|XL|L|M|S))*|\b\d{2}\b', re.I)
UNWANTED_WORDS= re.compile(r'\b(мужское|женское)\b', re.I)

PROTECT_HANDLE_RX = re.compile(r'@julia_fashionshop', re.I)
HASHTAG_RX        = re.compile(r'#\w[\w-]*', re.U)
PRICE_OR_DISC_RX  = re.compile(r'[€]|-\s*\d{1,2}\s*%|retail\s*price', re.I)
BRAND_LINE_RX     = re.compile(r'^(?:[A-Z][A-Za-z0-9-]{2,})(?:\s+[A-Z][A-Za-z0-9-]{2,}){0,2}$')

# ---------- Модель данных ----------
@dataclass
class ParsedItem:
    retail: Optional[float]
    disc_pct: Optional[int]
    season: Optional[str]
    sizes: Optional[str]

# ---------- Парсинг карточки ----------
def parse_item(text: str) -> ParsedItem:
    if not text:
        return ParsedItem(None, None, None, None)
    txt = UNWANTED_WORDS.sub("", text).strip()
    m = PRICE_DISC_RX.search(txt)
    retail = float(m.group("retail").replace(",", ".")) if m else None
    disc   = int(m.group("disc")) if m else None
    m_sea  = SEASON_RX.search(txt)
    season = m_sea.group(0) if m_sea else None
    sizes = None
    for line in txt.splitlines():
        L = line.strip()
        if "/" in L and len(L) <= 30:
            sizes = re.sub(r'\s+', '', L)
        elif re.fullmatch(r'\d{2}', L):
            sizes = L
    return ParsedItem(retail, disc, season, sizes)

# ---------- Формулы ----------
def parse_formula_to_ops(formula: str) -> List[Tuple[str, float]]:
    ops = []
    if not formula:
        return ops
    formula = formula.replace(" ", "")
    tokens = re.findall(r'([+-])(%|\d+(?:[.,]\d+)?%|\d+(?:[.,]\d+)?€)', formula)
    for sign, body in tokens:
        if body == "%":
            ops.append(("ITEM_DISC", -1 if sign == "-" else +1))  # ожидаем только "-%"
        elif body.endswith("%"):
            val = float(body[:-1].replace(",", "."))
            ops.append(("PCT", val if sign == "+" else -val))
        elif body.endswith("€"):
            val = float(body[:-1].replace(",", "."))
            ops.append(("ABS", val if sign == "+" else -val))
    return ops

def apply_formula(retail: float, disc_pct: int, ops: list) -> float:
    price = retail
    for kind, val in ops:
        if kind == "ITEM_DISC":
            price = price * (1 - disc_pct / 100.0)
        elif kind == "PCT":
            price = price * (1 + val / 100.0)
        elif kind == "ABS":
            price = price + val
    base_for_fee = price
    if base_for_fee <= 250:
        price = base_for_fee + 55
    elif base_for_fee <= 400:
        price = base_for_fee + 70
    return price

def fmt_eur(value: float) -> str:
    return f"{math.ceil(value)}€"

# ---------- OCR ----------
def preprocess_for_ocr(img_path: str) -> Image.Image:
    img = cv2.imread(img_path, cv2.IMREAD_COLOR)
    if img is None:
        return Image.open(img_path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.convertScaleAbs(gray, alpha=1.6, beta=0)
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY, 31, 10)
    bw = cv2.resize(bw, None, fx=1.7, fy=1.7, interpolation=cv2.INTER_LINEAR)
    return Image.fromarray(bw)

async def ocr_extract_eur_amounts(path: str) -> List[float]:
    pil_img = preprocess_for_ocr(path)
    try_langs = ["eng+ita", "eng", "eng+rus"]
    text = ""
    for lang in try_langs:
        try:
            text = pytesseract.image_to_string(pil_img, lang=lang)
            if text:
                break
        except Exception:
            continue
    text = (text or "").replace("\n", " ")
    amounts = []
    for m in re.finditer(r'(?:€\s*|)(\d{2,6})(?:[.,]\d{1,2})?\s*€', text):
        try:
            amounts.append(float(m.group(1).replace(",", ".")))
        except:
            pass
    for m in re.finditer(r'(?:EUR|EURO)\s*(\d{2,6})', text, flags=re.I):
        try:
            amounts.append(float(m.group(1)))
        except:
            pass
    return [a for a in amounts if 30 <= a <= 10000]

# ---------- Построение ответа ----------
def build_bold_block(final_price: str, retail_fmt: str, season: str, sizes: str) -> str:
    lines = [f"*{final_price} ✅*", f"*Retail price {retail_fmt} ❌*"]
    if season:
        lines.append(f"*{season}*")
    if sizes:
        lines.append(f"*{sizes}*")
    return "\n".join(lines).strip()

# ---------- Чистка брендов ----------
def clean_brands(text: str) -> str:
    t = HASHTAG_RX.sub('', text or "")
    out_lines = []
    for raw in t.splitlines():
        line = raw.strip()
        if not line:
            continue
        if PRICE_OR_DISC_RX.search(line) or SEASON_RX.search(line) or SIZES_RX.search(line):
            out_lines.append(raw)
            continue
        if BRAND_LINE_RX.match(line):
            continue
        out_lines.append(raw)
    cleaned = "\n".join(out_lines)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned

# ---------- Telegram ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
bot = Bot(BOT_TOKEN, parse_mode=ParseMode.MARKDOWN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start(msg: Message):
    await msg.answer("Бот онлайн. Используй /formula или пост «Цена ...» для формулы.")

@dp.message(Command("formula"))
async def set_formula_cmd(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("Использование: `/formula -%+10%+30€`")
        return
    global CURRENT_FORMULA
    CURRENT_FORMULA = parts[1].strip()
    save_formula(CURRENT_FORMULA)
    await msg.reply(f"Формула сохранена: `{CURRENT_FORMULA}`")

@dp.channel_post(F.text.regexp(r'^\s*Цена\b', flags=re.I))
async def formula_from_broadcast(msg: Message):
    text = msg.text or msg.caption or ""
    m = re.search(r'Цена\s*([^\n#]+)', text, flags=re.I)
    if not m:
        return
    global CURRENT_FORMULA
    CURRENT_FORMULA = m.group(1).strip()
    save_formula(CURRENT_FORMULA)
    await msg.answer(f"Формула обновлена: `{CURRENT_FORMULA}`")
    try:
        await bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
    except Exception:
        pass

# ------- Одиночные посты -------
@dp.channel_post(F.media_group_id == None, (F.photo) | (F.content_type == ContentType.TEXT))
async def on_single_post(msg: Message):
    text = msg.caption or msg.text or ""
    if PROTECT_HANDLE_RX.search(text or ""):
        return

    item = parse_item(text)
    retail, disc = (item.retail if item else None), (item.disc_pct if item else None)

    # OCR если нет розницы
    ocr_used = False
    if retail is None and msg.photo:
        file = await bot.get_file(msg.photo[-1].file_id)
        path = f"/tmp/{file.file_id}.jpg"
        await bot.download_file(file.file_path, destination=path)
        amounts = await ocr_extract_eur_amounts(path)
        if amounts:
            retail = max(amounts)
            ocr_used = True

    if retail is None or disc is None:
        cleaned = clean_brands(text or "")
        if cleaned and cleaned != (text or "").strip():
            try:
                if msg.photo:
                    await bot.send_photo(chat_id=msg.chat.id, photo=msg.photo[-1].file_id,
                                         caption=cleaned, parse_mode="Markdown", reply_markup=order_kb)
                else:
                    await bot.send_message(chat_id=msg.chat.id, text=cleaned, parse_mode="Markdown", reply_markup=order_kb)
                await bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
            except Exception:
                pass
        return

    ops = parse_formula_to_ops(CURRENT_FORMULA)
    final = apply_formula(retail, disc, ops)
    out = build_bold_block(fmt_eur(final), fmt_eur(retail), item.season or "", item.sizes or "")

    try:
        if msg.photo:
            await bot.send_photo(chat_id=msg.chat.id, photo=msg.photo[-1].file_id,
                                 caption=out, parse_mode="Markdown", reply_markup=order_kb)
        else:
            await bot.send_message(chat_id=msg.chat.id, text=out, parse_mode="Markdown", reply_markup=order_kb)
    except Exception:
        pass

    if ocr_used:
        try:
            await bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
        except Exception:
            pass
    else:
        cleaned = clean_brands(text or "")
        if cleaned and cleaned != (text or "").strip():
            try:
                if msg.photo:
                    await bot.send_photo(chat_id=msg.chat.id, photo=msg.photo[-1].file_id,
                                         caption=cleaned, parse_mode="Markdown", reply_markup=order_kb)
                else:
                    await bot.send_message(chat_id=msg.chat.id, text=cleaned, parse_mode="Markdown", reply_markup=order_kb)
                await bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
            except Exception:
                pass

# ------- Медиальбомы -------
ALBUM_WAIT_SEC = 1.0

@dp.channel_post(F.media_group_id)
async def on_album_collect(msg: Message):
    mgid = str(msg.media_group_id)
    MEDIA_BUFFER.setdefault(mgid, {"msgs": [], "ts": time.time()})
    MEDIA_BUFFER[mgid]["msgs"].append(msg)

    await asyncio.sleep(ALBUM_WAIT_SEC)

    bundle = MEDIA_BUFFER.pop(mgid, None)
    if not bundle:
        return

    msgs: List[Message] = sorted(bundle["msgs"], key=lambda m: m.message_id)
    photos = [m for m in msgs if m.photo]
    texts  = [m for m in msgs if (m.text or m.caption)]
    chat_id = msg.chat.id

    text_blob = "\n".join([(m.text or m.caption or "") for m in texts]).strip()
    if PROTECT_HANDLE_RX.search(text_blob):
        return

    item = parse_item(text_blob)
    disc = item.disc_pct if item else None
    if disc is None:
        m = re.search(r'-(\d{1,2})\s*%', text_blob)
        if m:
            disc = int(m.group(1))

    retail: Optional[float] = None
    ocr_hits: List[Message] = []
    for pmsg in photos:
        try:
            file = await bot.get_file(pmsg.photo[-1].file_id)
            path = f"/tmp/{file.file_id}.jpg"
            await bot.download_file(file.file_path, destination=path)
            amounts = await ocr_extract_eur_amounts(path)
            if amounts:
                ocr_hits.append(pmsg)
                val = max(amounts)
                retail = max(retail or 0, val)
        except Exception:
            pass

    if retail is None and item and item.retail is not None:
        retail = item.retail

    if photos and (retail is not None) and (disc is not None):
        ops = parse_formula_to_ops(CURRENT_FORMULA)
        final = apply_formula(retail, disc, ops)
        caption = build_bold_block(fmt_eur(final), fmt_eur(retail), (item.season or ""), (item.sizes or ""))
        first_photo_file_id = photos[0].photo[-1].file_id
        await bot.send_photo(chat_id=chat_id, photo=first_photo_file_id,
                             caption=caption, parse_mode="Markdown", reply_markup=order_kb)
        for m_ in msgs:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=m_.message_id)
            except Exception:
                pass
    else:
        if photos:
            first_photo_file_id = photos[0].photo[-1].file_id
            cleaned = clean_brands(text_blob)
            if cleaned and cleaned != text_blob:
                try:
                    await bot.send_photo(chat_id=chat_id, photo=first_photo_file_id,
                                         caption=cleaned, parse_mode="Markdown", reply_markup=order_kb)
                    for m_ in msgs:
                        try:
                            await bot.delete_message(chat_id=chat_id, message_id=m_.message_id)
                        except Exception:
                            pass
                except Exception:
                    pass

    # Удаляем сообщения, из которых читали ценник (если нужно скрыть фото ценников)
    for p in ocr_hits:
        try:
            await bot.delete_message(chat_id=p.chat.id, message_id=p.message_id)
        except Exception:
            pass

# ---------- run ----------
async def main():
    print("Bot is running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
