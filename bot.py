import os
import re
import logging
from typing import Dict, Optional, List

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, ContentType

# ----------------- НАСТРОЙКИ -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("price-bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Храним формулы по чатам (пока в памяти)
FORMULAS: Dict[int, str] = {}  # chat_id -> formula string, напр. "-%+10%+30€"

# ----------------- РЕГЭКСЫ -----------------
# Цена и скидка. Ловим:
#   5010-35% | 5010 - 35% | 5010€ -35% | Цена 5 010 € - 35 %
PRICE_PAT = re.compile(
    r'(?:^|\n|\s)(?:цена[:\s]*)?'
    r'(?P<price>\d[\d\s.,]*)\s*(?:€|eur|euro)?\s*[-–]?\s*(?P<disc>\d+(?:[.,]\d+)?)\s*%',
    re.IGNORECASE
)

# Размеры
SIZES_PAT = re.compile(
    r'размер(?:ы)?[:\s]+(?P<sizes>[A-Za-zА-Яа-я0-9/ ,.\-]+)',
    re.IGNORECASE
)

BRAND_WORDS = [
    'gucci','prada','valentino','balenciaga','jimmy choo','bally','stone island',
    'chopard','hermès','hermes','dior','lv','louis vuitton','versace','celine',
    'burberry','miumiu','bottega veneta','loewe','ysl','saint laurent','fendi'
]
BAN_WORDS = BRAND_WORDS + ['мужское','женское']

# ----------------- УТИЛИТЫ -----------------
def to_float(num_str: str) -> float:
    """Превращаем '5 010,50' -> 5010.50"""
    s = num_str.replace('\u00A0','').replace(' ', '').replace(',', '.')
    return float(s)

def fmt_eur(x: float) -> str:
    return f"{x:.2f}€"

def parse_post(text: str):
    """Достаём цену, скидку, размеры и дополнительные строки."""
    m = PRICE_PAT.search(text)
    if not m:
        return None

    price = to_float(m.group('price'))
    disc = float(m.group('disc').replace(',', '.'))
    final_after_sale = price * (1 - disc/100.0)

    sizes: Optional[str] = None
    ms = SIZES_PAT.search(text)
    if ms:
        sizes = ms.group('sizes').strip()

    # Собираем полезные строки, исключая бренды/пол и строку с ценой/размерами
    extra: List[str] = []
    for line in (ln.strip() for ln in text.splitlines() if ln.strip()):
        low = line.lower()
        if PRICE_PAT.search(line):      # строка с ценой
            continue
        if low.startswith('размер'):    # строка с размерами
            continue
        if any(w in low for w in BAN_WORDS):
            continue
        extra.append(line)

    return {
        "price": price,
        "discount": disc,
        "after_sale": final_after_sale,
        "sizes": sizes,
        "extra": extra,
    }

# ----------------- ФОРМУЛА -----------------
# Формула — последовательность шагов:
#   -%        -> применить скидку из поста
#   +10%      -> прибавить 10 процентов
#   +30€      -> прибавить 30 евро
#   -5€       -> вычесть 5 евро
STEP_PAT = re.compile(r'(?P<op>[+\-])\s*(?P<val>\d+(?:[.,]\d+)?)\s*(?P<unit>%|€)', re.IGNORECASE)

def apply_formula(base_after_sale: float, discount_pct: float, formula: str) -> float:
    """
    Применяем формулу к цене **после скидки**.
    Специальный шаг '-%' означает «применить скидку к исходной цене».
    Логика:
      1) Если в формуле есть '-%', то сначала считаем price*(1-disc/100),
         иначе берём base_after_sale как уже готовую цену после скидки.
      2) Затем последовательно применяем остальные шаги.
    """
    # Нормализуем
    f = formula.replace(' ', '')
    # Сначала проверим наличие '-%'
    start_price = base_after_sale
    if '-%' in f:
        # Повторно применим скидку к исходной цене? Нет — base_after_sale уже содержит её.
        # Поэтому убираем этот маркер только как "явно указали использовать скидку".
        f = f.replace('-%', '')
        # start_price уже равен base_after_sale
    # Теперь остальное по порядку
    price = start_price
    for m in STEP_PAT.finditer(f):
        op = m.group('op')
        val = float(m.group('val').replace(',', '.'))
        unit = m.group('unit')
        if unit == '%':
            if op == '+':
                price = price * (1 + val/100.0)
            else:
                price = price * (1 - val/100.0)
        else:  # €
            if op == '+':
                price = price + val
            else:
                price = price - val
    return price

def get_formula(chat_id: int) -> str:
    # Формула по умолчанию — просто "-%" (применить скидку магазина).
    return FORMULAS.get(chat_id, "-%")

# ----------------- КОМАНДЫ -----------------
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    await msg.answer(
        "Бот онлайн.\n"
        "• Установить формулу: <code>/formula -%+10%+30€</code>\n"
        "• Затем отправляй пост вида:\n"
        "  <code>5010-35%</code> (можно в подписи к фото)\n"
        "  или <code>Цена 5010€ -35%</code>\n",
        parse_mode=ParseMode.HTML
    )

@dp.message(Command("formula"))
async def cmd_formula(msg: Message):
    # Ожидаем "/formula <шаги>"
    parts = msg.text.split(maxsplit=1)
    if len(parts) == 1:
        return await msg.answer(
            "Использование: <code>/formula -%+10%+30€</code>\n"
            "Допустимые шаги: <code>-%</code>, <code>+N%</code>, <code>-N%</code>, "
            "<code>+N€</code>, <code>-N€</code>.",
            parse_mode=ParseMode.HTML
        )
    formula = parts[1].strip()
    # Лёгкая валидация
    if not ('%' in formula or '€' in formula):
        return await msg.answer("Формула должна содержать шаги с % или €.", parse_mode=ParseMode.HTML)

    FORMULAS[msg.chat.id] = formula
    await msg.answer(f"Формула сохранена: <b>{formula}</b>", parse_mode=ParseMode.HTML)

# ----------------- ОСНОВНОЙ ХЕНДЛЕР -----------------
@dp.message(F.content_type.in_({ContentType.TEXT, ContentType.PHOTO}))
async def handle_price(msg: Message):
    text = (msg.caption or msg.text or "").strip()
    if not text:
        return

    parsed = parse_post(text)
    if not parsed:
        # Тихо игнорируем нерелевантные сообщения
        return

    # применяем формулу
    formula = get_formula(msg.chat.id)
    final_price = apply_formula(parsed["after_sale"], parsed["discount"], formula)

    # собираем ответ
    lines = [
        f"✅ <b>{fmt_eur(final_price)}</b>",
        f"❌ <b>Retail price {fmt_eur(parsed['price'])}</b>",
    ]
    if parsed.get("sizes"):
        lines.append(f"Размеры: {parsed['sizes']}")
    lines += parsed.get("extra", [])

    await msg.answer("\n".join(lines), parse_mode=ParseMode.HTML)

# ----------------- ЗАПУСК -----------------
async def main():
    log.info("Bot is starting…")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
