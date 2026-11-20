import html
import re
from datetime import datetime as dtime
import os
import json
import tomllib
from functools import wraps
from pathlib import Path

from telethon import (TelegramClient, events)
from telethon.tl.types import MessageEntityTextUrl
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, text, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import httpx
import asyncio
import pytz

load_dotenv()

# Database setup
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///adverts.db")
Base = declarative_base()
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
Session = sessionmaker(bind=engine)


class StopProcessing(Exception):
    """
    No need to process more messages
    """
    pass


class PermissionDenied(Exception):
    """
    Permission denied to process the message
    """
    pass


class Advert(Base):
    __tablename__ = 'adverts'
    id = Column(Integer, primary_key=True, autoincrement=True, server_default=text("nextval('adverts_id_seq'::regclass)"))
    external_id = Column(Integer, unique=True)
    url = Column(String, unique=False)
    district = Column(String)
    price = Column(Float)
    media = Column(Float)
    deposit = Column(Integer)
    rooms = Column(Integer)
    area = Column(Float)
    posted_at = Column(DateTime)
    year_built = Column(Integer)
    no_animals = Column(Boolean)
    animals_mentioned = Column(String)


class ConfigResult(Base):
    """Outcome of evaluating one advert against one search configuration."""
    __tablename__ = 'config_results'
    __table_args__ = (UniqueConstraint('url', 'config_name'),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    url = Column(String, nullable=False)
    config_name = Column(String, nullable=False)
    status = Column(String, nullable=False)  # sent | filtered | failed


Base.metadata.create_all(engine)

# Define your bot token and chat ID
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]  # adverts are posted by the bot, not the user account
INPUT_CHAT_ID = int(os.getenv("TELEGRAM_INPUT_CHANNEL_ID"))
OUTPUT_CHAT_ID = int(os.getenv("TELEGRAM_OUTPUT_GROUP_ID"))
PROCESS_FROM_DATE = dtime.strptime(os.getenv("PROCESS_FROM_DATE"), '%Y-%m-%d %H:%M:%S')
timezone = pytz.timezone('Europe/Warsaw')
PROCESS_FROM_DATE = timezone.localize(PROCESS_FROM_DATE)

# Sample advert text
advert_text = """
ELEGANCKIE 2POK 52M + SŁONECZNY TARAS 35M + GARAŻ

📍 Район: #Wilanów

💰 Цена: 3600 zł 
🔢 Комнаты: #2_комнаты
〽 Площадь: 52.0 м²
📜 Частное лицо

📆 31/08/2024 | 23:46
"""


class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, dtime):
            # Convert datetime to ISO format
            return obj.strftime('%Y-%m-%d %H:%M:%S')
        return super().default(obj)

# Function to extract fields from the advert text
def extract_fields(text):
    patterns = {
        "district": lambda t: re.search(r'Район: #(\w+)', t).group(1),
        "price": lambda t: float(re.search(r'Цена: (\d+)', t).group(1)),
        "media": lambda t: re.search(r'\[\+(\d+)', t).group(1),
        "deposit": lambda t: float(re.search(r'Кауция: (\d+)', t).group(1)),
        "rooms": lambda t: int(re.search(r'Комнаты: #(\d+)', t).group(1)),
        "area": lambda t: float(re.search(r'Площадь: ([\d.]+)', t).group(1)),
        "posted_at": lambda t: dtime.strptime(
            re.search(r'📆 (.*)$', t).group(1),
            '%d/%m/%Y | %H:%M'
        )
    }
    values = {}
    for key, value in patterns.items():
        try:
             values[key] = value(text)
        except AttributeError:
            values[key] = None

    return values


async def extract_otodom_info(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        response = await client.get(url, headers=headers)
        if response.status_code == 403:
            raise PermissionDenied("HTTP 403 Forbidden")
        response.raise_for_status()
        html = response.text

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>({.*?})</script>', html, re.DOTALL)
    if not m:
        return {"year_built": None, "no_animals": False, "animals_mentioned": None}

    data = json.loads(m.group(1))
    ad = data['props']['pageProps']['ad']

    try:
        year_built = int(ad['target']['Build_year'])
    except (KeyError, TypeError, ValueError):
        year_built = None

    description = re.sub(r'<[^>]+>', ' ', ad.get('description', ''))
    description_lower = description.lower()

    no_animals = (
        "bez zwierząt" in description_lower
        or "zwierzęta nie akceptowane" in description_lower
        or "zwierzęta nie są akceptowane" in description_lower
        or "nieposiadających zwierząt" in description_lower
    )
    animals_mentioned = extract_context(description_lower, "zwierz")

    return {
        "year_built": year_built,
        "no_animals": no_animals,
        "animals_mentioned": animals_mentioned,
    }


def extract_context(text, search_term):
    words = text.split()
    for i, word in enumerate(words):
        if search_term in word:
            start = max(0, i - 3)
            end = min(len(words), i + 4)
            return " ".join(words[start:end])
    return None

def filter_room_count(min_rooms=1, max_rooms=10):
    @wraps(filter_room_count)
    def wrapper(ad):
        if ad['rooms'] is None:
            return True
        return min_rooms <= ad['rooms'] <= max_rooms
    return wrapper

def filter_regions(include=(), exclude=()):
    @wraps(filter_regions)
    def wrapper(ad):
        return (
                (not include or ad['district'] in include) and
                (not exclude or ad['district'] not in exclude))
    return wrapper

def filter_price(min_price=None, max_price=None):
    @wraps(filter_price)
    def wrapper(ad):
        if ad['price'] is None:
            return True
        return (not min_price or ad['price'] >= min_price) and (not max_price or ad['price'] <= max_price)
    return wrapper

def filter_area(min_area=None, max_area=None):
    @wraps(filter_area)
    def wrapper(ad):
        if ad['area'] is None:
            return True
        return (
                (not min_area or ad['area'] >= min_area) and
                (not max_area or ad['area'] <= max_area))
    return wrapper

def filter_price_to_area_ratio(min_ratio=None, max_ratio=None):
    @wraps(filter_price_to_area_ratio)
    def wrapper(ad):
        if not ad['price'] or not ad['area']:
            return True
        ratio = ad['price'] / ad['area']
        return (not min_ratio or ratio >= min_ratio) and (not max_ratio or ratio <= max_ratio)
    return wrapper

class SearchConfig:
    def __init__(self, data, path):
        self.name = data.get('name') or path.stem
        self.enabled = data.get('enabled', True)
        self.backfill = data.get('backfill', False)
        self.output_chat_id = int(data.get('output_chat_id') or OUTPUT_CHAT_ID)
        f = data.get('filters', {})
        self.filters = (
            filter_room_count(f.get('min_rooms', 1), f.get('max_rooms', 10)),
            filter_regions(include=f.get('include_districts', ()), exclude=f.get('exclude_districts', ())),
            filter_area(f.get('min_area'), f.get('max_area')),
            filter_price(f.get('min_price'), f.get('max_price')),
            filter_price_to_area_ratio(f.get('min_price_per_m2'), f.get('max_price_per_m2')),
            filter_year_built(f.get('min_year_built'), f.get('max_year_built')),
        )

    def evaluate(self, ad):
        results = {f.__name__: f(ad) for f in self.filters}
        return all(results.values()), results


def load_configs():
    config_dir = Path(os.getenv('CONFIG_DIR') or Path(__file__).resolve().parent.parent / 'configs')
    only = {n.strip() for n in os.getenv('ACTIVE_CONFIGS', '').split(',') if n.strip()}
    configs = []
    for path in sorted(config_dir.glob('*.toml')):
        with open(path, 'rb') as fh:
            cfg = SearchConfig(tomllib.load(fh), path)
        if cfg.enabled and (not only or cfg.name in only):
            configs.append(cfg)
    if not configs:
        raise RuntimeError(f"No enabled search configs found in {config_dir}")
    print(f"Loaded configs: {[c.name for c in configs]}")
    return configs


CONFIGS = []


def format_advert(ad):
    animals_info = ""
    if ad['no_animals']:
        animals_info = "\n🚫 <b>Без тварин</b>"
    elif ad.get('animals_mentioned'):
        animals_info = f"\n🐾 <b>тварини:</b> {html.escape(ad['animals_mentioned'])}"

    return (
        f"{animals_info}\n"
        f"💰 <b>Ціна:</b> {ad['price']} zł (+{ad['media']} zł media)\n"
        f"〽️ <b>Площа:</b> {ad['area']} m²\n"
        f"🔢 <b>Кімнат:</b> {ad['rooms']}\n"
        f"🏠 <b>Район {html.escape(str(ad['district']))}</b>\n"
#         f"🗯 <b>Депозит:</b> {ad['deposit']} zł\n"
        f"🏗 <b>Рік будинку:</b> {ad.get('year_built') or 'N/A'}\n"
        "\n"
        f"📅 <b>Posted at:</b> {ad['posted_at'].strftime('%Y-%m-%d %H:%M')}\n"
        f"🔗 <a href=\"{html.escape(ad['url'])}\">Посилання</a>"
    )

def filter_year_built(min_year=None, max_year=None):
    @wraps(filter_year_built)
    def wrapper(ad):
        year_built = ad.get('year_built')
        if year_built is None:
            return True
        return (not min_year or year_built >= min_year) and (not max_year or year_built <= max_year)
    return wrapper

async def send_to_telegram(client, chat_id, message, media=None):
    """Post via the Bot API so the message comes from the bot, not the user account.
    `client` (the user session) is only used to download the photo from the source message."""
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    photo = None
    if media:
        try:
            photo = await client.download_media(media, file=bytes)
        except Exception as e:
            print(f"Could not download media, sending text only: {e}")
    async with httpx.AsyncClient(timeout=60) as http:
        for _ in range(5):
            if photo and len(message) <= 1024:
                r = await http.post(f"{base}/sendPhoto",
                                    data={"chat_id": chat_id, "caption": message, "parse_mode": "HTML"},
                                    files={"photo": ("photo.jpg", photo)})
            else:
                r = await http.post(f"{base}/sendMessage",
                                    data={"chat_id": chat_id, "text": message, "parse_mode": "HTML",
                                          "disable_web_page_preview": "true"})
            body = r.json()
            if body.get("ok"):
                return
            if r.status_code == 429:
                await asyncio.sleep(body.get("parameters", {}).get("retry_after", 5) + 1)
                continue
            if photo:  # e.g. unusable image: retry as plain text
                photo = None
                continue
            raise RuntimeError(f"Bot API error {r.status_code}: {body.get('description')}")
        raise RuntimeError("Bot API: too many retries")

def advert_to_dict(row):
    return {c.name: getattr(row, c.name) for c in Advert.__table__.columns}

async def process_message(event, client):
    try:
        url = [e.url for e in event.entities if isinstance(e, MessageEntityTextUrl)][0]
    except (IndexError, TypeError):
        url = None

    if not url:
        return

    if event.date < PROCESS_FROM_DATE:
        raise StopProcessing()

    # Use proper session management
    with Session() as session:
        existing = session.query(Advert).filter_by(url=url).first()
        results = {r.config_name: r for r in session.query(ConfigResult).filter_by(url=url)}

        if existing:
            # Already stored: only (re)evaluate configs that need backfill or whose last send failed.
            configs = [c for c in CONFIGS
                       if (c.name not in results and c.backfill)
                       or (c.name in results and results[c.name].status == 'failed')]
            if not configs:
                return
            advert = advert_to_dict(existing)
        else:
            configs = CONFIGS
            text = event.message
            try:
                text = text.message
            except AttributeError:
                pass

            if not event.entities:
                return

            advert = extract_fields(text)
            advert['url'] = url

            # Extract additional information based on the URL
            additional_info = {}
            if 'otodom.pl' in url:
                try:
                    additional_info = await asyncio.wait_for(extract_otodom_info(url), timeout=60)
                except PermissionDenied:
                    print(f"Got HTTP 403 from {url}, skipping")
                    return
                except Exception as e:
                    print(f"Error scraping otodom {url}, using defaults: {e}")
                    additional_info = {"year_built": None, "no_animals": False, "animals_mentioned": None}

            advert.update(additional_info)

            # Ensure year_built is None if it's an empty string to avoid database errors
            if advert.get('year_built') == '':
                advert['year_built'] = None

            session.add(Advert(**advert))
            session.commit()

            print(f"Processed and saved advert {advert.get('url')}")
            print(json.dumps(advert, indent=4, cls=DateTimeEncoder))

        media = event.media if event.media else None

        for cfg in configs:
            passed, filter_results = cfg.evaluate(advert)
            print(f"[{cfg.name}] Filter results: {json.dumps(filter_results)}")
            status = 'filtered'
            if passed:
                try:
                    await send_to_telegram(client, cfg.output_chat_id, format_advert(advert), media)
                    status = 'sent'
                except Exception as e:
                    print(f"[{cfg.name}] Failed to send {url}: {e}")
                    status = 'failed'
            row = results.get(cfg.name)
            if row:
                row.status = status
            else:
                session.add(ConfigResult(url=url, config_name=cfg.name, status=status))
            session.commit()


_last_activity = dtime.now()
WATCHDOG_TIMEOUT_HOURS = 6


async def watchdog():
    """Exit the process if no Telegram update has been received for WATCHDOG_TIMEOUT_HOURS.
    Kubernetes will restart the pod, re-establishing the update stream."""
    while True:
        await asyncio.sleep(600)  # check every 10 minutes
        idle_seconds = (dtime.now() - _last_activity).total_seconds()
        if idle_seconds > WATCHDOG_TIMEOUT_HOURS * 3600:
            print(f"No activity for {idle_seconds / 3600:.1f}h, restarting to reconnect.")
            raise SystemExit(1)


async def main():
    global _last_activity
    CONFIGS[:] = load_configs()
    session_path = os.getenv('TELEGRAM_SESSION_PATH', '/app/session/bot')
    client = TelegramClient(session_path, API_ID, API_HASH)
    # await client.start(bot_token=BOT_TOKEN)
    await client.start(phone=os.getenv("TELEGRAM_PHONE"))

    @client.on(events.NewMessage(chats=INPUT_CHAT_ID))
    async def handler(event):
        global _last_activity
        _last_activity = dtime.now()
        await process_message(event, client)

    # async for dialog in client.iter_dialogs():
    #     if not dialog.is_group and dialog.is_channel:
    #         print(dialog.id, dialog.name)
    #
    input_chat = await client.get_entity(INPUT_CHAT_ID)

    try:
        async for message in client.iter_messages(input_chat):
            await process_message(message, client)
            # Add small delay to prevent overwhelming the system
            await asyncio.sleep(1)
    except StopProcessing:
        pass

    # Watchdog starts only after the backlog is drained — resets on each new live message
    _last_activity = dtime.now()
    asyncio.ensure_future(watchdog())
    await client.run_until_disconnected()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
