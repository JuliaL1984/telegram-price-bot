# bot.py — альбомы, 30+ режимов, OCR ценника, дебаунс 1.5с для альбомов,
# подсказки при отсутствии цены и поддержка "альбом/фото/видео → общий текст"
# + Строгая гарантия порядка публикаций через GLOBAL SEQ-барьер (по message_id)
# Версия с 5-строчной подписью, точным парсингом размеров/сезона,
# и двумя режимами: /lux (OCR off) и /luxocr (OCR on), одинаковая формула.
# Округление всегда вверх; бренд из подписи удалён.
# Альбомы БЕЗ подписи публикуются сразу (без ожидания текста).

import os
import re
import io
import math
import json
import asyncio
import heapq
from typing import Dict, Callable, Optional, List, Tuple, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from collections import deque

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, InputMediaPhoto, InputMediaVideo
from aiogram.enums import ParseMode, MessageEntityType
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command

# ====== НАСТРОЙКИ ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1002973176038"))
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

ALBUM_SETTLE_MS = int(os.getenv("ALBUM_SETTLE_MS", "1500"))  # стабильнее собирает альбомы
ALBUM_WINDOW_SECONDS = int(os.getenv("ALBUM_WINDOW_SECONDS", "30"))

# Сколько держать текст с эмодзи в ожидании фото (очереди партий)
BATCH_IDLE_MS = int(os.getenv("BATCH_IDLE_MS", "2800"))

# OCR
OCR_ENABLED = os.getenv("OCR_ENABLED", "1") == "1"
OCR_LANG = os.getenv("OCR_LANG", "ita+eng")
VISION_JSON = os.getenv("VISION_JSON", "").strip()  # если есть — используем Google Vision

# Базовая политика: в альбомах убирать кадры-ценники (1 — да; 0 — пересылать как есть)
FILTER_PRICETAGS_IN_ALBUMS = os.getenv("FILTER_PRICETAGS_IN_ALBUMS", "1") == "1"

# ====== ИНИЦИАЛИЗАЦИЯ ======
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ====== ПАМЯТЬ ======
# MediaItem: {"kind": "photo"|"video"|"text"|"forward", "fid": str, "mid": int, "cap": bool}
last_media: Dict[int, Dict[str, Any]] = {}
active_mode: Dict[int, str] = {}
album_buffers: Dict[Tuple[int, str], Dict[str, Any]] = {}

# -------- Очередь партий (FIFO) для текстов-эмодзи и их медиа --------
@dataclass
class BatchRec:
    text_msg: Optional[Message] = None
    media: List[Dict[str, Any]] = field(default_factory=list)
    timer: Optional[asyncio.Task] = None
    user_id: Optional[int] = None

batches: Dict[int, deque[BatchRec]] = {}

def _get_q(chat_id: int) -> deque:
    q = batches.get(chat_id)
    if q is None:
        q = batches[chat_id] = deque()
    return q

def _arm_batch_timer(chat_id: int, rec: BatchRec):
    # Если медиа не придут вовремя — ничего не публикуем (чтобы "ничего не улетало")
    if rec.timer:
        rec.timer.cancel()
    async def _fire():
        try:
            await asyncio.sleep(BATCH_IDLE_MS/1000)
        except asyncio.CancelledError:
            return
        q = _get_q(chat_id)
        if rec in q and not rec.media:
            q.remove(rec)
            return
    rec.timer = asyncio.create_task(_fire())

async def _publish_batch_pair(chat_id: int, rec: BatchRec):
    """Публикуем текст и медиа в ТОЧНОМ исходном порядке по message_id."""
    if rec.timer:
        rec.timer.cancel()
    if not rec.text_msg or not rec.media:
        return

    text_mid = rec.text_msg.message_id
    first_media_mid = min(m["mid"] for m in rec.media)
    user_id = rec.user_id or (rec.text_msg.from_user.id if rec.text_msg.from_user else 0)
    seq_first = min(text_mid, first_media_mid)

    async def _send_text():
        await publish_to_target(
            first_mid=seq_first,
            user_id=user_id,
            items=[{"kind": "forward", "from_chat_id": rec.text_msg.chat.id, "mid": rec.text_msg.message_id, "cap": True}],
            caption=""
        )

    async def _send_media():
        await publish_to_target(
            first_mid=seq_first,
            user_id=user_id,
            items=rec.media,
            caption=""
        )

    if text_mid <= first_media_mid:
        await _send_text()
        await _send_media()
    else:
        await _send_media()
        await _send_text()

def _attach_media_to_next_batch(chat_id: int, media_items: List[Dict[str, Any]], user_id: int) -> bool:
    q = _get_q(chat_id)
    for rec in list(q):
        if rec.media:
            continue
        rec.media = media_items
        rec.user_id = rec.user_id or user_id
        asyncio.create_task(_publish_batch_pair(chat_id, rec))
        q.remove(rec)
        return True
    return False

# ====== ГЛОБАЛЬНЫЙ ПОРЯДОК ПО message_id (min-heap) ======
# payload: (seq, first_mid, user_id, items, caption, album_ocr_on)
publish_queue: "asyncio.Queue[Tuple[int, int, int, List[Dict[str, Any]], str, bool]]" = asyncio.Queue()
_heap: List[Tuple[int, int, Tuple[int, int, int, List[Dict[str, Any]], str, bool]]] = []
_heap_tie = 0

def calc_seq_by_first_mid(first_mid: int) -> int:
    return int(first_mid)

def is_ocr_enabled_for(user_id: int) -> bool:
    mode = active_mode.get(user_id, "sale")
    if mode == "lux":
        return False
    if mode == "luxocr":
        return True
    return FILTER_PRICETAGS_IN_ALBUMS

async def _do_publish(user_id: int, items: List[Dict[str, Any]], caption: str, album_ocr_on: bool):
    if not items:
        return

    if items and items[0].get("kind") == "forward":
        it = items[0]
        try:
            await bot.forward_message(
                chat_id=TARGET_CHAT_ID,
                from_chat_id=it["from_chat_id"],
                message_id=it["mid"],
            )
        except Exception:
            if caption:
                await bot.send_message(TARGET_CHAT_ID, caption)
        return

    if items and items[0].get("kind") == "text":
        await bot.send_message(TARGET_CHAT_ID, caption or "")
        return

    items = await filter_pricetag_media(items, album_ocr_on)

    if len(items) == 1:
        it = items[0]
        if it["kind"] == "video":
            await bot.send_video(TARGET_CHAT_ID, it["fid"], caption=caption)
        else:
            await bot.send_photo(TARGET_CHAT_ID, it["fid"], caption=caption)
        return

    first = items[0]
    media = []
    if first["kind"] == "video":
        media.append(InputMediaVideo(media=first["fid"], caption=caption, parse_mode=ParseMode.HTML))
    else:
        media.append(InputMediaPhoto(media=first["fid"], caption=caption, parse_mode=ParseMode.HTML))
    for it in items[1:]:
        media.append(InputMediaVideo(media=it["fid"]) if it["kind"] == "video" else InputMediaPhoto(media=it["fid"]))
    await bot.send_media_group(TARGET_CHAT_ID, media)

async def publish_worker():
    global _heap_tie
    while True:
        payload = await publish_queue.get()
        seq = payload[0]
        _heap_tie += 1
        heapq.heappush(_heap, (seq, _heap_tie, payload))
        publish_queue.task_done()
        while _heap:
            _, _, pl = heapq.heappop(_heap)
            _seq, first_mid, user_id, items, caption, album_ocr_on = pl
            try:
                await _do_publish(user_id, items, caption, album_ocr_on)
            except Exception as e:
                print("PUBLISH ERROR:", repr(e))

async def publish_to_target(first_mid: int, user_id: int, items: List[Dict[str, Any]], caption: str):
    album_ocr_on = is_ocr_enabled_for(user_id)
    seq = calc_seq_by_first_mid(first_mid)
    await publish_queue.put((seq, first_mid, user_id, items, caption, album_ocr_on))

# ====== OCR ======
GV_CLIENT = None
if OCR_ENABLED and VISION_JSON:
    try:
        from google.cloud import vision as gv
        creds_dict = None
        if VISION_JSON.startswith("{"):
            creds_dict = json.loads(VISION_JSON)
            import google.oauth2.service_account as svc
            creds = svc.Credentials.from_service_account_info(creds_dict)
            GV_CLIENT = gv.ImageAnnotatorClient(credentials=creds)
        else:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = VISION_JSON
            GV_CLIENT = gv.ImageAnnotatorClient()
        print("Google Vision: initialized")
    except Exception as e:
        GV_CLIENT = None
        print("Google Vision init failed:", repr(e))

TESS_AVAILABLE = False
if OCR_ENABLED:
    try:
        import pytesseract  # noqa
        from PIL import Image, ImageOps, ImageFilter  # noqa
        TESS_AVAILABLE = True
        print("Tesseract: available")
    except Exception as e:
        print("Tesseract not available:", repr(e))

_PRICE_TOKEN_RE = re.compile(
    r"""(?xi)
    (?:[€]\s*\d{1,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?    # € слева
     | \d{1,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?\s*[€]    # € справа
     | \d{1,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?\s*(?:eur|euro)
    )
    """
)

_PRICE_WITH_DISC_RE = re.compile(
    r"""(?xi)
    \d{2,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?\s*(?:€|eur|euro)?
    \s*(?:-|—|–)?\s*\d{1,2}\s*%
    """
)

def _preprocess_for_tesseract(img):
    from PIL import ImageOps, ImageFilter
    g = ImageOps.grayscale(img)
    g = ImageOps.autocontrast(g)
    g = g.filter(ImageFilter.SHARPEN)
    return g

async def _load_bytes(file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download(file, buf)
    return buf.getvalue()

def _ocr_google_vision(data: bytes) -> str:
    if not GV_CLIENT:
        return ""
    try:
        from google.cloud import vision as gv  # type: ignore
        image = gv.Image(content=data)
        resp = GV_CLIENT.document_text_detection(image=image)
        if resp.error.message:
            print("GV ERROR:", resp.error.message)
            return ""
        return (resp.full_text_annotation.text or "").strip()
    except Exception as e:
        print("GV EXC:", repr(e))
        return ""

def _ocr_tesseract(data: bytes) -> str:
    if not TESS_AVAILABLE:
        return ""
    try:
        from PIL import Image
        import pytesseract
        img = Image.open(io.BytesIO(data))
        img = _preprocess_for_tesseract(img)
        cfg = r'--oem 3 --psm 6 -c preserve_interword_spaces=1'
        text = pytesseract.image_to_string(img, lang=OCR_LANG, config=cfg)
        return (text or "").strip()
    except Exception as e:
        print("TESS EXC:", repr(e))
        return ""

def _ocr_extract_text(data: bytes) -> Tuple[str, str]:
    if GV_CLIENT:
        t = _ocr_google_vision(data)
        if t:
            return ("GV", t)
    if TESS_AVAILABLE:
        t = _ocr_tesseract(data)
        if t:
            return ("TESS", t)
    return ("", "")

def _looks_like_price_text(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    has_kw = any(k in low for k in ("retail", "prezzo", "price", "eur", "%"))
    has_token = bool(_PRICE_TOKEN_RE.search(text)) or bool(_PRICE_WITH_DISC_RE.search(text))
    has_basic = ("€" in text or "eur" in low or "euro" in low) and bool(re.search(r"\d", text))
    return has_token or (has_kw and bool(re.search(r"\d", text))) or has_basic

async def ocr_should_hide(file_id: str) -> bool:
    if not OCR_ENABLED:
        return False
    try:
        data = await _load_bytes(file_id)
        engine, txt = _ocr_extract_text(data)
        if not txt:
            print(f"OCR[{engine or 'NONE'}] NOT_FOUND")
            return False
        found = _looks_like_price_text(txt)
        print(f"OCR[{engine}] {'FOUND' if found else 'NOT_FOUND'} :: {txt[:120].replace(chr(10),' ')}")
        return found
    except Exception as e:
        print("OCR ERROR:", repr(e))
        return False

async def filter_pricetag_media(items: List[Dict[str, Any]], album_ocr_on: bool) -> List[Dict[str, Any]]:
    if len(items) == 1 or not album_ocr_on:
        return items
    kept: List[Dict[str, Any]] = []
    for it in items:
        if it["kind"] == "photo":
            hide = await ocr_should_hide(it["fid"])
            if not hide:
                kept.append(it)
        else:
            kept.append(it)
    return kept if kept else items[:1]

# ====== КАЛЬКУЛЯТОРЫ ======
def ceil_price(value: float) -> int:
    return int(math.ceil(value - 1e-9))

def default_calc(price: float, discount: int) -> int:
    discounted = price * (1 - discount / 100)
    if discounted <= 250:
        return ceil_price(discounted + 55)
    elif discounted <= 400:
        return ceil_price(discounted + 70)
    else:
        return ceil_price(discounted + 90)

def lux_calc(price: float, discount: int) -> int:
    discounted = price * (1 - discount / 100)
    if discounted <= 250:
        final = discounted + 55
    elif discounted <= 400:
        final = discounted + 70
    else:
        final = discounted * 1.10 + 30
    return ceil_price(final)

# ====== РАЗБОР И 5-СТРОЧНАЯ ПОДПИСЬ ======
def cleanup_text_basic(text: str) -> str:
    text = re.sub(r"#\S+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# === РАЗМЕРЫ ===
SIZE_ALPHA   = r"(?:XXS|XS|S|M|L|XL|XXL)"
SIZE_NUM_EU  = r"(?:3\d|4[0-6])(?:[.,]5)?"
SIZE_NUM_US  = r"(?:[5-9]|1[0-2])(?:[.,]5)?"
SIZE_NUM_BAL = r"(?:[1-6])"
SIZE_NUM_ANY = rf"(?:{SIZE_NUM_EU}|{SIZE_NUM_US}|{SIZE_NUM_BAL})"
SIZE_TOKEN   = rf"(?:{SIZE_ALPHA}|{SIZE_NUM_ANY})"

def _strip_seasons_for_size_scan(text: str) -> str:
    return re.sub(r"\b(?:NEW\s+)?(?:FW|SS)\d+(?:/\d+)?\b", " ", text, flags=re.I)

def _strip_discounts_and_prices(text: str) -> str:
    text = re.sub(r"[–—\-−]\s?\d{1,2}\s?%", " ", text)
    text = re.sub(_PRICE_TOKEN_RE, " ", text)
    return text

def extract_sizes_anywhere(text: str) -> str:
    work = _strip_seasons_for_size_scan(text)
    work = _strip_discounts_and_prices(work)

    ranges_dash  = re.findall(rf"(?<!\d)({SIZE_NUM_ANY})\s*[-–—]\s*({SIZE_NUM_ANY})(?!\d)", work)
    ranges_slash = re.findall(rf"(?<!\d)({SIZE_NUM_ANY})\s*/\s*({SIZE_NUM_ANY})(?!\d)", work)

    singles_num   = re.findall(rf"(?<!\d)({SIZE_NUM_ANY})(?!\d)", work)
    singles_alpha = re.findall(rf"\b({SIZE_ALPHA})\b", work, flags=re.I)

    parts: List[str] = []
    used = set()

    def add(tok: str):
        tok = tok.replace(".5", ",5")
        if tok not in used:
            parts.append(tok); used.add(tok)

    for a, b in (ranges_dash + ranges_slash):
        add(f"{a.replace('.5', ',5')}-{b.replace('.5', ',5')}")
    for t in singles_alpha:
        add(t.upper())

    covered_nums = set()
    def _expand(n1: str, n2: str):
        try:
            a = float(n1.replace(",", "."))
            b = float(n2.replace(",", "."))
        except Exception:
            a = float(n1.replace(",", "."))
            b = float(n2.replace(",", "."))
        lo, hi = sorted((a, b))
        x = lo
        while x <= hi + 1e-9:
            s = ("{:.1f}".format(x)).replace(".5", ",5").rstrip("0").rstrip(",")
            covered_nums.add(s)
            x += 0.5
    for a, b in (ranges_dash + ranges_slash):
        _expand(a, b)

    for t in singles_num:
        norm = t.replace(".5", ",5")
        if norm in covered_nums:
            continue
        add(norm)

    evidence_of_ranges = bool(ranges_dash or ranges_slash)
    has_alpha = bool(singles_alpha)
    if not evidence_of_ranges and not has_alpha:
        only_nums = [p for p in parts if re.fullmatch(r"\d+(?:,\d)?", p)]
        if len(only_nums) == 1:
            val = only_nums[0].replace(",", ".")
            try:
                f = float(val)
                is_eu  = 30.0 <= f <= 46.0
                is_us  = 5.0  <= f <= 12.0
                is_bal = 1.0  <= f <= 6.0
                if not (is_eu or is_us or is_bal):
                    return ""
            except Exception:
                return ""
        elif len(only_nums) == 0:
            return ""
    return ", ".join(parts)

# --- УНИВЕРСАЛЬНЫЙ ПАРСЕР ДЕНЕГ ---
def parse_money_token(token: Optional[str]) -> Optional[float]:
    if not token:
        return None
    s = re.sub(r"[^\d.,]", "", token)
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            dec, thou = ",", "."
        else:
            dec, thou = ".", ","
        s = s.replace(thou, "").replace(dec, ".")
        try:
            return float(s)
        except ValueError:
            return None
    if "," in s and "." not in s:
        if s.count(",") == 1 and re.search(r",\d{1,2}$", s):
            s = s.replace(",", ".")
            try:
                return float(s)
            except ValueError:
                return None
        s = s.replace(",", "")
        try:
            return float(s)
        except ValueError:
            return None
    if "." in s and "," not in s:
        if s.count(".") == 1 and re.search(r"\.\d{1,2}$", s):
            try:
                return float(s)
            except ValueError:
                return None
        s = s.replace(".", "")
        try:
            return float(s)
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None

PRICE_DISCOUNT_RE = re.compile(
    r"""
    (?P<price>\d{2,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)   # цена
    \s*(?:€|eur|euro)?\s*
    (?:-|—|–|\s-\s|\s—\s|\s–\s)?
    \s*(?P<discount>\d{1,2})\s*%
    """,
    re.IGNORECASE | re.VERBOSE | re.S
)

def parse_price_discount(text: str) -> Tuple[Optional[float], Optional[int]]:
    if not text:
        return (None, None)
    m = PRICE_DISCOUNT_RE.search(text)
    if not m:
        return (None, None)
    price = parse_money_token(m.group("price"))
    if price is None:
        return (None, None)
    disc = int(m.group("discount"))
    if not (0 < disc <= 90) or price <= 0:
        return (None, None)
    return (price, disc)

# --- НОРМАЛИЗАЦИЯ «1.150 -> 1150», «2.990 -> 2990» ---
_THOUSANDS_DOT_RE = re.compile(r'(?<!\d)(\d{1,3})\.(\d{3})(?!\d)')
def normalize_thousands(text: str) -> str:
    return _THOUSANDS_DOT_RE.sub(r"\1\2", text)

# --- Блок «Размеры: ...» ---
SIZES_BLOCK_RE = re.compile(r"Размеры:\s*(?P<body>.+?)(?:\n\s*\n|#|$)", re.I | re.S)
SIZE_ITEM_RE   = re.compile(r"\b(XXS|XS|S|M|L|XL|XXL|\д{2}(?:[.,]5)?)\b", re.I)  # (не менял)

def parse_sizes_block(text: str) -> str:
    m = SIZES_BLOCK_RE.search(text or "")
    if not m:
        return ""
    body = m.group("body")
    vals = [v.upper().replace(".5", ",5") for v in SIZE_ITEM_RE.findall(body)]
    out, seen = [], set()
    for v in vals:
        if v not in seen:
            seen.add(v); out.append(v)
    return ", ".join(out)

def _is_price_line(l: str) -> bool:
    return bool(PRICE_DISCOUNT_RE.search(l) or re.search(r"\d{2,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?\s*(€|eur|euro)", l, flags=re.I))

def pick_sizes_line(lines: List[str]) -> str:
    for line in lines:
        l = line.strip()
        if not l or _is_price_line(l):
            continue
        if re.search(rf"\b({SIZE_ALPHA})\b", l, flags=re.I):
            return l
        if re.search(rf"(?<!\d){SIZE_NUM_ANY}(?:\s*(?:[,/]\s*{SIZE_NUM_ANY}))+?(?!\d)", l):
            return l
        if re.search(rf"(?<!\d){SIZE_NUM_ANY}\s*[-–/]\s*{SIZE_NUM_ANY}(?!\d)", l):
            return l
    for i, line in enumerate(lines):
        l = line.strip()
        if not l or _is_price_line(l):
            continue
        if re.fullmatch(rf"{SIZE_NUM_ANY}", l):
            prev_is_price = (i > 0 and _is_price_line(lines[i-1].strip()))
            next_is_price = (i+1 < len(lines) and _is_price_line(lines[i+1].strip()))
            if prev_is_price or next_is_price:
                continue
            return l
    return ""

def pick_season_line(lines: List[str]) -> str:
    for line in lines:
        if re.search(r"\bNEW\s+(?:FW|SS)\d+(?:/\d+)?\b", line, flags=re.I):
            return line.strip()
    for line in lines:
        if re.search(r"\b(?:FW|SS)\d+(?:/\d+)?\b", line, flags=re.I):
            return line.strip()
    return ""

def parse_number_token(token: Optional[str]) -> Optional[float]:
    return parse_money_token(token)

# ========= МУЛЬТИ-ПОЗИЦИИ =========
POS_SPLIT_RE = re.compile(r"\n\s*\n+")
PRICE_TOKEN_OR_PAIR_RE = re.compile(
    rf"(?:{PRICE_DISCOUNT_RE.pattern})|(?:\d{{2,6}}(?:[.,]\d{{3}})*(?:[.,]\д{{1,2}})?\s*(?:€|eur|euro))",
    re.IGNORECASE | re.VERBOSE | re.S,
)

def parse_input(raw_text: str) -> Dict[str, Optional[str]]:
    raw_text = normalize_thousands(raw_text)
    text = cleanup_text_basic(raw_text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    price_uni, discount_uni = parse_price_discount(text)

    price_m    = re.search(r"(\d{1,6}(?:[.,]\d{3})*(?:[.,]\д{1,2})?)\s*€", text)
    discount_m = re.search(r"[–—\-−]\s*(\d{1,2})\s*%", text)
    retail_m   = re.search(r"Retail\s*price\s*(\d{1,6}(?:[.,]\д{3})*(?:[.,]\д{1,2})?)", text, flags=re.I)

    price    = price_uni if price_uni is not None else (parse_number_token(price_m.group(1)) if price_m else None)
    discount = discount_uni if discount_uni is not None else (int(discount_m.group(1)) if discount_m else 0)
    retail   = parse_number_token(retail_m.group(1)) if retail_m else (price if price is not None else 0.0)

    sizes_line  = parse_sizes_block(text) or pick_sizes_line(lines) or extract_sizes_anywhere(text)
    season_line = pick_season_line(lines)

    return {
        "price": price,
        "discount": discount,
        "retail": retail,
        "sizes_line": sizes_line,
        "season_line": season_line,
        "brand_line": "",
        "cleaned_text": text,
    }

# ----------- разрезание блоков (оставил как было) -----------
def _split_by_price_lines(caption: str) -> List[str]:
    rows = (caption or "").splitlines()
    blocks: List[List[str]] = []
    cur: List[str] = []
    saw_price_in_cur = False

    def flush():
        nonlocal cur, saw_price_in_cur
        if cur and saw_price_in_cur:
            while cur and not cur[-1].strip():
                cur.pop()
            if cur:
                blocks.append(cur)
        cur = []
        saw_price_in_cur = False

    for r in rows:
        if _is_price_line(r) and saw_price_in_cur:
            flush()
        cur.append(r)
        if _is_price_line(r):
            saw_price_in_cur = True
    flush()
    return ["\n".join(b).strip() for b in blocks if b]

def _split_positions(caption: str) -> List[str]:
    by_price = _split_by_price_lines(caption)
    if len(by_price) >= 2:
        return by_price
    blocks = [b.strip() for b in POS_SPLIT_RE.split(caption.strip()) if b.strip()]
    if len(blocks) <= 1:
        return []
    good = [b for b in blocks if PRICE_TOKEN_OR_PAIR_RE.search(b)]
    return good if len(good) >= 2 else []

def build_result_text_for_block(user_id: int, text_block: str) -> str:
    data = parse_input(text_block)
    price = data.get("price")
    mode = MODES.get(active_mode.get(user_id, "sale"), MODES["sale"])
    calc_fn, tpl_fn = mode["calc"], mode["template"]
    if price is None:
        hint = "⚠️ Не нашла цену. Пример: 650€ -35% или 1360-20%"
        sizes = (data.get("sizes_line") or "").strip()
        season = (data.get("season_line") or "").strip()
        parts = [hint]
        if sizes: parts.append(sizes)
        if season: parts.append(season)
        return "\n".join(parts)
    final_price = calc_fn(float(price), int(data.get("discount", 0)))
    return tpl_fn(
        final_price=final_price,
        retail=float(data.get("retail", 0.0) or 0.0),
        sizes_line=data.get("sizes_line", "") or "",
        season_line=data.get("season_line", "") or "",
        brand_line="",
    )

def build_result_text_multi(user_id: int, caption: str) -> Optional[str]:
    blocks = _split_positions(caption)
    if not blocks:
        return None
    chunks = [build_result_text_for_block(user_id, b) for b in blocks]
    return ("\n".join(chunks)).strip()

# ====== КОМАНДЫ ======
def template_five_lines(final_price: int,
                        retail: float,
                        sizes_line: str,
                        season_line: str,
                        brand_line: str) -> str:
    line1 = f"✅ <b>{ceil_price(final_price)}€</b>"
    show_retail = bool(retail) and (ceil_price(final_price) <= ceil_price(retail))
    line2 = f"❌ <b>Retail price {ceil_price(retail)}€</b>" if show_retail else ""
    line3 = sizes_line or ""
    line4 = season_line or ""
    line5 = ""
    lines = [line1, line2, line3, line4, line5]
    cleaned = []
    for s in lines:
        if not s:
            continue
        if cleaned and s == cleaned[-1]:
            continue
        cleaned.append(s)
    while len(cleaned) < 5:
        cleaned.append("")
    return "\n".join(cleaned[:5])

def mk_mode(label: str,
            calc: Callable[[float, int], int] = default_calc,
            template: Callable[[int, float, str, str, str], str] = template_five_lines):
    return {"label": label, "calc": calc, "template": template}

MODES: Dict[str, Dict] = {
    "sale": mk_mode("SALE"),
    "lux": mk_mode("LUX", calc=lux_calc),
    "luxocr": mk_mode("LUX OCR", calc=lux_calc),
    "outlet": mk_mode("OUTLET"),
    "stock": mk_mode("STOCK"),
    "newfw": mk_mode("NEW FW"),
    "newss": mk_mode("NEW SS"),
    "bags10": mk_mode("BAGS -10%"),
    "bags15": mk_mode("BAGS -15%"),
    "bags20": mk_mode("BAGS -20%"),
    "bags25": mk_mode("BAGS -25%"),
    "bags30": mk_mode("BAGS -30%"),
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
    "m1": mk_mode("M1"), "m2": mk_mode("M2"), "m3": mk_mode("M3"), "m4": mk_mode("М4"), "m5": mk_mode("M5"),
}

def is_admin(user_id: int) -> bool:
    return (not ADMINS) or (user_id in ADMINS)

@router.message(Command(commands=list(MODES.keys())))
async def set_mode(msg: Message):
    user_id = msg.from_user.id
    cmd = msg.text.lstrip("/").split()[0]
    if not is_admin(user_id):
        return await msg.answer("⛔ Только для админов.")
    active_mode[user_id] = cmd
    await msg.answer(f"✅ Режим <b>{MODES[cmd]['label']}</b> активирован.")

@router.message(Command("mode"))
async def show_mode(msg: Message):
    user_id = msg.from_user.id
    mode_key = active_mode.get(user_id, "sale")
    label = MODES.get(mode_key, MODES["sale"])["label"]
    ocr_state = "ON" if is_ocr_enabled_for(user_id) else "OFF"
    await msg.answer(f"Текущий режим: <b>{label}</b>\nOCR в альбомах: <b>{ocr_state}</b>")

@router.message(Command("help"))
async def show_help(msg: Message):
    await msg.answer(
        "Бот принимает фото/видео (альбомы) и текст с ценой.\n"
        "• /lux — OCR выключен, /luxocr — OCR включен.\n"
        "• Формула: ≤250€ +55€; 251–400€ +70€; >400€ → +10% и +30€. Всё округляем вверх.\n"
        "• Альбом без подписи публикуется сразу.\n"
        "• /mode — показать текущий режим и состояние OCR."
    )

@router.message(Command("ping"))
async def ping(msg: Message):
    await msg.answer("pong")

# ====== СБОРКА ПОДПИСИ ======
def build_result_text(user_id: int, caption: str) -> Optional[str]:
    multi = build_result_text_multi(user_id, caption)
    if multi:
        return multi

    data = parse_input(caption)
    price = data.get("price")
    if price is None:
        return None
    mode = MODES.get(active_mode.get(user_id, "sale"), MODES["sale"])
    calc_fn, tpl_fn, _label = mode["calc"], mode["template"], mode["label"]
    final_price = calc_fn(float(price), int(data.get("discount", 0)))
    return tpl_fn(
        final_price=final_price,
        retail=float(data.get("retail", 0.0) or 0.0),
        sizes_line=data.get("sizes_line", "") or "",
        season_line=data.get("season_line", "") or "",
        brand_line="",
    )

# ====== ХЕЛПЕРЫ ======
async def _remember_media_for_text(chat_id: int, user_id: int, items: List[Dict[str, Any]], first_mid: int, caption: str = ""):
    last_media[chat_id] = {
        "ts": datetime.now(),
        "items": items,
        "caption": caption or "",
        "user_id": user_id,
        "first_mid": first_mid,
    }

# ====== ХЕНДЛЕРЫ ======
@router.message(F.photo & (F.media_group_id == None))
async def handle_single_photo(msg: Message):
    item = {"kind": "photo", "fid": msg.photo[-1].file_id, "mid": msg.message_id, "cap": bool(msg.caption)}
    caption = (msg.caption or "").strip()

    if _attach_media_to_next_batch(msg.chat.id, [item], msg.from_user.id):
        return

    if caption:
        result = build_result_text(msg.from_user.id, caption)
        if result:
            await publish_to_target(first_mid=msg.message_id, user_id=msg.from_user.id, items=[item], caption=result)
            return
    await _remember_media_for_text(msg.chat.id, msg.from_user.id, [item], first_mid=msg.message_id, caption=caption)
    try:
        await msg.answer("Добавь текст с ценой/скидкой (например: 650€ -35% или 1360-20%) — опубликую одним постом.")
    except Exception:
        pass

@router.message(F.video & (F.media_group_id == None))
async def handle_single_video(msg: Message):
    item = {"kind": "video", "fid": msg.video.file_id, "mid": msg.message_id, "cap": bool(msg.caption)}
    caption = (msg.caption or "").strip()

    if _attach_media_to_next_batch(msg.chat.id, [item], msg.from_user.id):
        return

    if caption:
        result = build_result_text(msg.from_user.id, caption)
        if result:
            await publish_to_target(first_mid=msg.message_id, user_id=msg.from_user.id, items=[item], caption=result)
            return
    await _remember_media_for_text(msg.chat.id, msg.from_user.id, [item], first_mid=msg.message_id, caption=caption)
    try:
        await msg.answer("Добавь текст с ценой/скидкой (например: 650€ -35% или 1360-20%) — опубликую одним постом.")
    except Exception:
        pass

@router.message(F.media_group_id)
async def handle_album_any(msg: Message):
    chat_id, mgid = msg.chat.id, str(msg.media_group_id)
    key = (chat_id, mgid)

    if msg.photo:
        fid = msg.photo[-1].file_id
        kind = "photo"
    elif msg.video:
        fid = msg.video.file_id
        kind = "video"
    else:
        return

    cap_text = (msg.caption or "").strip()
    has_cap = bool(cap_text)

    buf = album_buffers.get(key)
    if not buf:
        buf = {"items": [], "caption": "", "task": None, "user_id": msg.from_user.id, "first_mid": msg.message_id}
        album_buffers[key] = buf

    buf["items"].append({"kind": kind, "fid": fid, "mid": msg.message_id, "cap": has_cap})
    if has_cap and not buf["caption"]:
        buf["caption"] = cap_text

    if buf["task"]:
        buf["task"].cancel()

    async def _flush_album():
        await asyncio.sleep(ALBUM_SETTLE_MS / 1000)
        data = album_buffers.pop(key, None)
        if not data:
            return

        items: List[Dict[str, Any]] = data["items"]
        caption = data["caption"]
        user_id = data["user_id"]
        first_mid = data["first_mid"]

        items.sort(key=lambda x: x["mid"])

        if not caption:
            if _attach_media_to_next_batch(chat_id, items, user_id):
                return

        if caption:
            result = build_result_text(user_id, caption)
            if result:
                await publish_to_target(first_mid=first_mid, user_id=user_id, items=items, caption=result)
                return
            await publish_to_target(
                first_mid=first_mid, user_id=user_id, items=items,
                caption=f"⚠️ Не нашла цену в тексте. Пример: 650€ -35% или 1360-20%\n\n{caption}"
            )
            return

        await publish_to_target(first_mid=first_mid, user_id=user_id, items=items, caption="")
        return

    buf["task"] = asyncio.create_task(_flush_album())

@router.message(F.text)
async def handle_text(msg: Message):
    txt = msg.text or ""
    has_price_token = bool(re.search(r"\d+(?:[.,]\d{3})*\s*€", txt, flags=re.I))
    has_discount    = bool(re.search(r"[–—\-−]?\s*\d{1,2}\s?%", txt))
    has_pair        = bool(PRICE_DISCOUNT_RE.search(txt))
    has_price = has_pair or has_price_token or has_discount

    has_custom = any(e.type == MessageEntityType.CUSTOM_EMOJI for e in (msg.entities or []))
    is_forward = bool(getattr(msg, "forward_origin", None))
    chat_id = msg.chat.id

    if not has_price and is_forward:
        return

    if not has_pair and not has_price_token and has_custom and not is_forward and is_admin(msg.from_user.id):
        q = _get_q(chat_id)
        rec = BatchRec(text_msg=msg, user_id=msg.from_user.id)
        q.append(rec)
        _arm_batch_timer(chat_id, rec)
        return

    # ---- ВАЖНО: жёсткая FIFO-привязка «последний начатый альбом до текста» ----
    cand = [(k, v) for (k, v) in album_buffers.items() if k[0] == chat_id and v.get("items")]
    if cand:
        # выбираем альбом с максимальным first_mid, который пришёл НЕ ПОЗЖЕ текста
        eligible = [kv for kv in cand if kv[1].get("first_mid", 0) <= msg.message_id]
        if eligible:
            eligible.sort(key=lambda kv: kv[1].get("first_mid", 0))
            key, data = eligible[-1]
        else:
            # если все альбомы начаты уже после текста — берём самый ранний из них (fallback)
            cand.sort(key=lambda kv: kv[1].get("first_mid", 0))
            key, data = cand[0]

        items: List[Dict[str, Any]] = data["items"]
        caption = (data.get("caption") or "")
        # аккуратно склеиваем подпись: без лишних пустых хвостов
        if caption:
            caption = caption.rstrip()
            caption += "\n"
        caption += (msg.text or "")
        caption = re.sub(r"\n{3,}", "\n\n", caption).rstrip()

        items.sort(key=lambda x: x["mid"])

        user_id = data.get("user_id") or msg.from_user.id
        first_mid = data.get("first_mid", items[0]["mid"] if items else msg.message_id)
        result = build_result_text(user_id, caption)

        if data.get("task"):
            data["task"].cancel()
        album_buffers.pop(key, None)

        if result:
            await publish_to_target(first_mid=first_mid, user_id=user_id, items=items, caption=result)
        else:
            await publish_to_target(
                first_mid=first_mid, user_id=user_id, items=items,
                caption=f"⚠️ Не нашла цену в тексте. Пример: 650€ -35% или 1360-20%\n\n{msg.text}"
            )
        return

    # Привязка к одиночным медиа за недавнее время
    bucket = last_media.get(chat_id)
    if bucket and (datetime.now() - bucket["ts"] <= timedelta(seconds=ALBUM_WINDOW_SECONDS)):
        user_id = bucket.get("user_id") or msg.from_user.id
        raw_text = (bucket.get("caption") or "")
        if raw_text:
            raw_text = raw_text.rstrip() + "\n"
        raw_text += (msg.text or "")
        raw_text = re.sub(r"\n{3,}", "\n\n", raw_text).rstrip()

        result = build_result_text(user_id, raw_text)
        items: List[Dict[str, Any]] = bucket.get("items") or []
        first_mid = bucket.get("first_mid") or (min(it["mid"] for it in items) if items else msg.message_id)

        if result:
            await publish_to_target(first_mid=first_mid, user_id=user_id, items=items, caption=result)
        else:
            await publish_to_target(
                first_mid=first_mid, user_id=user_id, items=items,
                caption=f"⚠️ Не нашла цену в тексте. Пример: 650€ -35% или 1360-20%\n\n{msg.text}"
            )

        del last_media[chat_id]
        return

    if not has_price and not has_custom:
        text_item = [{"kind": "text", "fid": "", "mid": msg.message_id, "cap": True}]
        await publish_to_target(first_mid=msg.message_id, user_id=msg.from_user.id, items=text_item, caption=txt)
        return

    return

# ====== ЗАПУСК ======
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(publish_worker())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
