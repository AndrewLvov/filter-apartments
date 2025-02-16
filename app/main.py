import re
from datetime import datetime as dtime
import os
import json
from functools import wraps
from time import sleep

from telethon import (TelegramClient, events)
from telethon.tl.types import MessageEntityTextUrl
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from playwright.async_api import async_playwright
from playwright._impl._errors import TimeoutError
import asyncio
import pytz

load_dotenv()

# Database setup
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///adverts.db")
Base = declarative_base()
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
session = Session()


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
    id = Column(Integer, primary_key=True)
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

Base.metadata.create_all(engine)

# Define your bot token and chat ID
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
# BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
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
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            user_agent=
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/93.0.4577.82 Safari/537.36")
        # test for HTTP 403, throw exception in that case
        # Listen for responses and check for HTTP 403
        async def handle_response(response):
            if response.url == url and response.status == 403:
                raise PermissionDenied("HTTP 403 Forbidden")

        page.on("response", handle_response)
        await page.goto(url)

        # Extract year built
        year_built = await page.evaluate('''
            () => {
                const paragraphs = document.querySelectorAll('p');
                for (let i = 0; i < paragraphs.length; i++) {
                    if (paragraphs[i].textContent.includes('Rok budowy')) {
                        const nextParagraph = paragraphs[i].nextElementSibling;
                        if (nextParagraph && nextParagraph.tagName === 'P') {
                            return nextParagraph.textContent.trim();
                        }
                    }
                }
                return null;
            }
        ''')

        # Extract full content for animal-related checks
        try:
            year_built = year_built and int(year_built)
        except ValueError:
            print(f"Failed to extract year built from {year_built}")
            year_built = None
        content = await page.content()
        
        # Check for "bez zwierząt" and "zwierz"
        no_animals = (
                "bez zwierząt" in content.lower()
                or "zwierzęta nie akceptowane" in content.lower())
        animals_mentioned = extract_context(content.lower(), "zwierz")
        
        await browser.close()
        
        return {
            "year_built": year_built,
            "no_animals": no_animals,
            "animals_mentioned": animals_mentioned
        }

async def extract_olx_info(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            user_agent=
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/93.0.4577.82 Safari/537.36")
        async def handle_response(response):
            if response.url == url and response.status == 403:
                raise PermissionDenied("HTTP 403 Forbidden")

        page.on("response", handle_response)
        await page.goto(url)

        # Wait for the main content to load
        # try:
        #     await page.wait_for_selector('#mainContent', timeout=5)
        # except TimeoutError:
        #     content = await page.content()
        #     raise
        #
        # Extract the content of the specific element
        content = await page.evaluate('''
            () => {
                const descriptionElement = document.querySelector('[data-cy="ad_description"]');
                return descriptionElement ? descriptionElement.innerText : '';
            }
        ''')
        
        # Check for "bez zwierząt" and "zwierz"
        no_animals = (
                "bez zwierząt" in content.lower() or
                "zwierzęta nie akceptowane" in content.lower() or
                'zwierzęta nie są akceptowane' in content.lower() or
                "nieposiadających zwierząt" in content.lower()
        )
        animals_mentioned = extract_context(content.lower(), "zwierz")
        
        await browser.close()

        return {
            "no_animals": no_animals,
            "animals_mentioned": animals_mentioned
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

def filter_area(min_area=None, max_area=None):
    @wraps(filter_area)
    def wrapper(ad):
        return (
                (not min_area or ad['area'] >= min_area) and
                (not max_area or ad['area'] <= max_area))
    return wrapper

def filter_price_to_area_ratio(min_ratio=None, max_ratio=None):
    @wraps(filter_price_to_area_ratio)
    def wrapper(ad):
        ratio = ad['price'] / ad['area']
        return (not min_ratio or ratio >= min_ratio) and (not max_ratio or ratio <= max_ratio)
    return wrapper

def format_advert(ad):
    animals_info = ""
    if ad['no_animals']:
        animals_info = "\n🚫 <span style='color: red;'><b>Без тварин</b></span>"
    elif ad.get('animals_mentioned'):
        animals_info = f"\n🐾 <b>тварини:</b> {ad['animals_mentioned']}"

    return (
        f"{animals_info}\n"
        f"💰 <b>Ціна:</b> {ad['price']} zł (+{ad['media']} zł media)\n"
        f"〽️ <b>Площа:</b> {ad['area']} m²\n"
        f"🔢 <b>Кімнат:</b> {ad['rooms']}\n"
        f"🏠 <b>Район {ad['district']}</b>\n"
#         f"🗯 <b>Депозит:</b> {ad['deposit']} zł\n"
        f"🏗 <b>Рік будинку:</b> {ad.get('year_built') or 'N/A'}\n"
        "\n"
        f"📅 <b>Posted at:</b> {ad['posted_at'].strftime('%Y-%m-%d %H:%M')}\n"
        f"🔗 <a href='{ad['url']}'>Посилання</a>"
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
    if media:
        await client.send_message(chat_id, message, file=media, parse_mode='html')
    else:
        await client.send_message(chat_id, message, parse_mode='html')

async def process_message(event, client):
    try:
        url = [e.url for e in event.entities if isinstance(e, MessageEntityTextUrl)][0]
    except IndexError:
        url = None
    if session.query(Advert).filter_by(url=url).first():
        return
        # raise StopProcessing()

    text = event.message
    try:
        text = text.message
    except AttributeError:
        pass

    if not event.entities:
        return

    advert = extract_fields(text)
    advert['url'] = url

    if event.date < PROCESS_FROM_DATE:
        raise StopProcessing()

    # Extract media from the original message
    media = event.media if event.media else None

    # Extract additional information based on the URL
    try:
        if 'otodom.pl' in url:
            additional_info = await extract_otodom_info(url)
        elif 'olx.pl' in url:
            try:
                additional_info = await extract_olx_info(url)
            except TimeoutError as e:
                print(e)
                additional_info = {
                    "no_animals": False,
                    "animals_mentioned": None
                }
        else:
            additional_info = {}
    except PermissionDenied:
        print(f"Got HTTP 403 from {url}")
        sleep(300)
        return

    # Update advert with additional information
    advert.update(additional_info)

    new_advert = Advert(**advert)
    session.add(new_advert)
    session.commit()

    print(f"Processed and saved advert {advert.get('url')}")
    print(json.dumps(advert, indent=4, cls=DateTimeEncoder))

    # Filter and send the advert
    filters = (
        filter_room_count(3, 4),
        filter_regions(exclude=(
            'Praga_Południe', 'Praga_Północ', "Białołęka")),
        filter_area(min_area=50, max_area=200),
        filter_price_to_area_ratio(min_ratio=60, max_ratio=110),
        # filter_year_built(min_year=2017),
    )
    filter_results = {f.__name__: f(advert) for f in filters}
    print(f"Filter results: {json.dumps(filter_results, indent=4)}")
    if all(filter_results.values()):
        formatted_message = format_advert(advert)
        await send_to_telegram(client, OUTPUT_CHAT_ID, formatted_message, media)

async def main():
    client = TelegramClient('me', API_ID, API_HASH)
    # await client.start(bot_token=BOT_TOKEN)
    await client.start(phone=os.getenv("TELEGRAM_PHONE"))
    
    @client.on(events.NewMessage(chats=INPUT_CHAT_ID))
    async def handler(event):
        await process_message(event, client)
        
    # async for dialog in client.iter_dialogs():
    #     if not dialog.is_group and dialog.is_channel:
    #         print(dialog.id, dialog.name)
    #
    input_chat = await client.get_entity(INPUT_CHAT_ID)

    try:
        async for message in client.iter_messages(input_chat):
            await process_message(message, client)
    except StopProcessing:
        pass
    
    await client.run_until_disconnected()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())

