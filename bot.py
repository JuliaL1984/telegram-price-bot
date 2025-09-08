import os
import re
import math
import logging
from typing import Dict, Optional, List

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, ContentType

# ----------------- НАСТРОЙКИ -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# Куда публиковать результат: "@my_channel" или "-1001234567890".
# Если пусто — отвечаем туда, где пришло сообщение.
TARGET_CHAT = os.getenv("TARGET_CHAT", "").strip()

# Временный режим: печатать chat_id на любое сообщение (0/1)
ECHO_CHAT_ID = os.getenv("ECHO_CHAT_ID", "0") == "1"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fashion-shop-bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Формулы по чатам (в памяти)
FORMULAS: Dict[int, str] = {}
DEFAULT_FORMULA = "-%"

# Чтобы отвечать один раз на альбом (media group)
PROCESSED_GROUPS = set()

# ----------------- РЕГЭКСЫ -----------------
# Цена/скидка: допускаем точку/запятую/пробел между € и минусом: "5200€.-35%"
PRICE_PAT = re.compile(
    r'(?s)(?:^|\n|\s)(?:цена[:\s]*)?'
    r'(?P<price>\d[\d\s.,]*)\s*(?:€|eur|euro)?'
    r'\s*[,.\s]*[-–]?\s*(?P<disc>\d+(?:[.,]\d+)?)\s*%',
    re.IGNORECASE
)

# Размеры: "Размер S", "Размеры: 38/39/40"
SIZES_PAT = re.compile(r'размер(?:ы)?[:\s]+(?P<sizes>[A-Za-zА-Яа-я0-9/ ,.\-]+)', re.IGNORECASE)

# Хештеги вида #BottegaVeneta
HASHTAG_PAT = re.compile(r'(^|\s)#\w+', re.UNICODE)

BRAND_WORDS = [
    'gucci','prada','valentino','balenciaga','jimmy choo','bally','stone island',
    'chopard','hermès','hermes','dior','lv','louis vuitton','versace','celine',
    'burberry','miumiu','bottega veneta','loewe','ysl','saint laurent','fendi'
]
BAN_WORDS = BRAND_WORDS + ['мужское','женское','в наличии в бутике']

# Шаги формулы: +10%, -5€, и т.п.; спец-метка "-%" — применить скидку магазина
STEP_PAT = re.compile(r'(?P<op>[+\-])\s*(?P<val>\d+(?:[.,]\d+)?)\s*(?P<unit>%|€)', re.IGNORECASE)

# ----------------- УТИЛИТЫ -----------------
def to_float(num_str: str) -> float:
    s = num_str.replace('\u00A0','').replace(' ', '').replace(',', '.')
    return float(s)

def fmt_eur(x: float) -> str:
    # Всегда округляем вверх
    return f"{math.ceil(x)}€"

def parse_post(text: str):
    m = PRICE_PAT.search(text)
    if not m:
        return None

    price = to_float(m.group('price'))
    disc = float(m.group('disc').replace(',', '.'))
    after_sale = price * (1 - disc/100.0)

    # Явные размеры "Размер ..."
    sizes: Optional[str] = None
    ms = SIZES_PAT.search(text)
    if ms:
        sizes = ms.group('sizes').strip()

    extra: List[str] = []
    fallback_sizes: List[str] = []
    for line in (ln.strip() for ln in text.splitlines() if ln.strip()):
        low = line.lower()

        # пропускаем строку с ценой
        if PRICE_PAT.search(line):
            continue

        # убираем бренды/пол/наличие и ХЕШТЕГИ
        if any(w in low for w in BAN_WORDS):
            continue
        if HASHTAG_PAT.search(low):
            continue

        # явные "Размер ..."
        if low.startswith('размер'):
            continue

        # ФОЛЛБЭК РАЗМЕРОВ: "40. 42", "38 40 42", "S/M", "Xs"
        if sizes is None and re.fullmatch(r'[A-Za-zА-Яа-я0-9/.\s]{1,20}', line) and any(ch.isdigit() or ch.isalpha() for ch in line):
            cleaned = re.sub(r'[,\.\s]+', '/', line).strip('/ ')
            if re.fullmatch(r'(?:[0-9]{1,2}|[XSMLxl]{1,3})(?:/(?:[0-9]{1,2}|[XSMLxl]{1,3}))*', cleaned, re.I):
                fallback_sizes.append(cleaned.upper())
                continue

        # не цена/не размеры/не бренд/не хештег — оставляем
        extra.append(line)

    if sizes is None and fallback_sizes:
        uniq = []
        for s in "/".join(fallback_sizes).split('/'):
            u = s.strip().upper()
            if u and u not in uniq:
                uniq.append(u)
        sizes = "/".join(uniq)

    return {
        "price": price,
        "discount": disc,
        "after_sale": after_sale,
        "sizes": sizes,
        "extra": extra,
    }

def apply_formula(base_after_sale: float, discount_pct: float, formula: str) -> float:
    # Формулу применяем к цене после скидки магазина
    f = formula.replace(' ', '').replace('-%', '')
    price = base_after_sale
    for m in STEP_PAT.finditer(f):
        op = m.group('op')
        val = float(m.group('val').replace(',', '.'))
        unit = m.group('unit')
        if unit == '%':
            price = price * (1 + val/100.0) if op == '+' else price * (1 - val/100.0)
        else:  # €
            price = price + val if op == '+' else price - val
    return price

def get_formula(chat_id: int) -> str:
    return FORMULAS.get(chat_id, DEFAULT_FORMULA)

# ---------- Выбор формулы по порогам ----------
def choose_formula_for_price(after_sale_eur: float, user_formula: str) -> str:
    """
    Если цена после скидки (цена-%) ≤ 250 → -%+55€
    Если 251–400 → -%+70€
    Иначе → пользовательская /formula
    """
    if after_sale_eur <= 250:
        return "-%+55€"
    elif 251 <= after_sale_eur <= 400:
        return "-%+70€"
    else:
        return user_formula

# ----------------- СЛУЖЕБНОЕ: chat_id -----------------
@dp.message(Command("chatid"))
async def cmd_chatid(msg: Message):
    await msg.answer(f"Chat ID: <code>{msg.chat.id}</code>", parse_mode=ParseMode.HTML)
    print("Chat ID:", msg.chat.id)

if ECHO_CHAT_ID:
    @dp.message()
    async def echo_chat_id(msg: Message):
        await msg.answer(f"Chat ID: <code>{msg.chat.id}</code>", parse_mode=ParseMode.HTML)
        print("Chat ID:", msg.chat.id)

# ----------------- КОМАНДЫ ПОЛЬЗОВАТЕЛЯ -----------------
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    await msg.answer(
        "Бот онлайн.\n"
        "1) Формула: <code>/formula -%+10%+30€</code>\n"
        "2) Отправляй пост: <code>5010-35%</code> (можно в подписи к фото/альбому)\n"
        "   или <code>Цена 5010€ -35%</code>.\n"
        "Сервис: <code>/chatid</code> — показать ID этого чата.",
        parse_mode=ParseMode.HTML
    )

@dp.message(Command("formula"))
async def cmd_formula(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) == 1:
        return await msg.answer(
            "Использование: <code>/formula -%+10%+30€</code>\n"
            "Шаги: <code>-%</code>, <code>+N%</code>, <code>-N%</code>, <code>+N€</code>, <code>-N€</code>.",
            parse_mode=ParseMode.HTML
        )
    formula = parts[1].strip()
    if not ('%' in formula or '€' in formula):
        return await msg.answer("Формула должна содержать шаги с % или €.", parse_mode=ParseMode.HTML)

    FORMULAS[msg.chat.id] = formula
    await msg.answer(f"Формула сохранена: <b>{formula}</b>", parse_mode=ParseMode.HTML)

# ----------------- ОСНОВНОЙ ХЕНДЛЕР -----------------
@dp.message(F.content_type.in_({ContentType.TEXT, ContentType.PHOTO}))
async def handle_price(msg: Message):
    # Для альбомов: отвечаем один раз, на сообщение с подписью
    if msg.media_group_id:
        if msg.caption is None:
            return
        if msg.media_group_id in PROCESSED_GROUPS:
            return
        PROCESSED_GROUPS.add(msg.media_group_id)

    text = (msg.caption or msg.text or "").strip()
    if not text:
        return

    parsed = parse_post(text)
    if not parsed:
        return  # молчим на нерелевантные сообщения

    # Выбор формулы по порогу
    user_formula = get_formula(msg.chat.id)
    formula = choose_formula_for_price(parsed["after_sale"], user_formula)

    # Итог
    final_price = apply_formula(parsed["after_sale"], parsed["discount"], formula)

    # Сборка подписи: всё жирным; эмодзи в КОНЦЕ строк
    lines = [
        f"<b>{fmt_eur(final_price)} ✅</b>",
        f"<b>Retail price {fmt_eur(parsed['price'])} ❌</b>",
    ]
    if parsed.get("sizes"):
        lines.append(f"<b>Размеры: {parsed['sizes']}</b>")
    for line in parsed.get("extra", []):
        lines.append(f"<b>{line}</b>")
    caption = "\n".join(lines)

    # Куда отправлять
    target = TARGET_CHAT if TARGET_CHAT else msg.chat.id

    if msg.content_type == ContentType.PHOTO:
        await bot.send_photo(target, msg.photo[-1].file_id, caption=caption, parse_mode=ParseMode.HTML)
    else:
        await bot.send_message(target, caption, parse_mode=ParseMode.HTML)

# ----------------- ЗАПУСК -----------------
async def main():
    log.info("Bot is starting…")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
