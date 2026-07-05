import os
import re
import logging
import tempfile
import requests
import base64
import time
from io import BytesIO
from PIL import Image
from urllib.parse import quote_plus
from dotenv import load_dotenv
from bs4 import BeautifulSoup

from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pydub import AudioSegment
from apify_client import ApifyClient

# ==================== ЗАГРУЗКА .env ====================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")

if not BOT_TOKEN:
    print("❌ ОШИБКА: BOT_TOKEN не найден в .env!")
    exit(1)

if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
    print("⚠️ ВНИМАНИЕ: Spotify API ключи не найдены в .env!")

if not APIFY_API_TOKEN:
    print("⚠️ ВНИМАНИЕ: APIFY_API_TOKEN не найден в .env!")
# =====================================================

MAX_FILE_SIZE = 50 * 1024 * 1024
TEMP_DIR = tempfile.mkdtemp()
COVER_SIZE = (3000, 3000)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ==================== SPOTIFY API (для обложки и названий) ====================

def get_spotify_token():
    """Получение токена для Spotify API (только для обложки и метаданных)"""
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    
    auth = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    data = {"grant_type": "client_credentials"}
    
    try:
        response = requests.post("https://accounts.spotify.com/api/token", headers=headers, data=data, timeout=10)
        response.raise_for_status()
        return response.json().get("access_token")
    except Exception as e:
        logger.error(f"Ошибка получения Spotify токена: {e}")
        return None


def extract_album_id(url):
    """Извлечение ID альбома из ссылки Spotify"""
    patterns = [
        r'spotify\.com/album/([a-zA-Z0-9]+)',
        r'open\.spotify\.com/album/([a-zA-Z0-9]+)',
        r'spotify\.com/track/([a-zA-Z0-9]+)',
        r'open\.spotify\.com/track/([a-zA-Z0-9]+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1), "track" if "track" in pattern else "album"
    
    return None, None


def get_album_metadata(album_id):
    """Получение базовых данных об альбоме через Spotify API (без ISRC)"""
    # Ждём 10 секунд перед каждым запросом к Spotify
    time.sleep(10.0)
    
    token = get_spotify_token()
    if not token:
        return None, None
    
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        album_resp = requests.get(
            f"https://api.spotify.com/v1/albums/{album_id}",
            headers=headers,
            timeout=30
        )
        album_resp.raise_for_status()
        album_data = album_resp.json()
        
        # Получаем список треков без ISRC
        tracks = []
        offset = 0
        limit = 50
        
        while True:
            tracks_resp = requests.get(
                f"https://api.spotify.com/v1/albums/{album_id}/tracks?limit={limit}&offset={offset}",
                headers=headers,
                timeout=30
            )
            tracks_resp.raise_for_status()
            tracks_data = tracks_resp.json()
            
            for track in tracks_data.get("items", []):
                tracks.append({
                    'name': track.get('name', 'Без названия'),
                    'duration_ms': track.get('duration_ms', 0),
                    'id': track.get('id')
                })
            
            if len(tracks_data.get("items", [])) < limit:
                break
            offset += limit
        
        return album_data, tracks
        
    except Exception as e:
        logger.error(f"Ошибка получения данных альбома: {e}")
        return None, None


def get_track_data(track_id):
    """Получение данных о треке через Spotify API"""
    token = get_spotify_token()
    if not token:
        return None
    
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        track_resp = requests.get(
            f"https://api.spotify.com/v1/tracks/{track_id}",
            headers=headers,
            timeout=15
        )
        track_resp.raise_for_status()
        return track_resp.json()
        
    except Exception as e:
        logger.error(f"Ошибка получения данных трека: {e}")
        return None


def get_cover_image(url, size=COVER_SIZE):
    """Скачивание и ресайз обложки в 3000x3000 JPG"""
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        
        img = Image.open(BytesIO(response.content))
        
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        
        img = img.resize(size, Image.Resampling.LANCZOS)
        
        buffer = BytesIO()
        img.save(buffer, format='JPEG', quality=95, optimize=True)
        buffer.seek(0)
        
        return buffer
        
    except Exception as e:
        logger.error(f"Ошибка обработки обложки: {e}")
        return None


def ms_to_min_sec(ms):
    """Конвертация миллисекунд в MM:SS"""
    if not ms:
        return "0:00"
    total_sec = int(ms / 1000)
    minutes = total_sec // 60
    seconds = total_sec % 60
    return f"{minutes}:{seconds:02d}"


# ==================== ISRC FINDER (НОВЫЙ СПОСОБ!) ====================

def get_isrc_from_isrcfinder(album_id):
    """Получение ISRC для всех треков через isrcfinder.com"""
    url = f"https://www.isrcfinder.com/?q={album_id}"
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        text = soup.get_text()
        
        # Ищем ISRC для каждого трека
        isrcs = {}
        
        # Шаблон для поиска ISRC в тексте (например, USSM11709907)
        isrc_pattern = r'([A-Z]{2}[A-Z0-9]{10})'
        isrc_matches = re.findall(isrc_pattern, text)
        
        # Шаблон для поиска названий треков с ISRC
        # Ищем блоки вида: "Track Name (3:28) ISRC: USSM11709907"
        track_pattern = r'([^\n]+)\s+ISRC:\s*([A-Z]{2}[A-Z0-9]{10})'
        track_matches = re.findall(track_pattern, text)
        
        if track_matches:
            for track_name, isrc in track_matches:
                isrcs[track_name.strip()] = isrc
        
        # Если не нашли по шаблону, пробуем найти через таблицу
        if not isrcs and isrc_matches:
            # Ищем названия треков рядом с ISRC
            lines = text.split('\n')
            for i, line in enumerate(lines):
                for isrc in isrc_matches:
                    if isrc in line:
                        # Ищем название трека в предыдущих 3 строках
                        for j in range(max(0, i-3), i):
                            if lines[j].strip() and not lines[j].strip().startswith(('ISRC', 'UPC', 'Album', 'Artist', 'Track')):
                                track_name = lines[j].strip()
                                if track_name and track_name not in isrcs:
                                    isrcs[track_name] = isrc
                                break
        
        logger.info(f"Найдено {len(isrcs)} ISRC на isrcfinder.com")
        return isrcs
        
    except Exception as e:
        logger.error(f"Ошибка парсинга isrcfinder.com: {e}")
        return {}


def get_upc_from_isrcfinder(album_id):
    """Получение UPC через isrcfinder.com/upc-finder"""
    url = f"https://www.isrcfinder.com/upc-finder/?q={album_id}"
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        text = soup.get_text()
        
        # Ищем UPC: 12-13 цифр
        upc_pattern = r'(?:UPC|EAN)[:\s]*([0-9]{12,13})'
        upc_match = re.search(upc_pattern, text, re.IGNORECASE)
        
        if upc_match:
            return upc_match.group(1)
        
        # Ищем просто 12-13 цифр подряд (может быть UPC)
        numbers = re.findall(r'\b([0-9]{12,13})\b', text)
        if numbers:
            return numbers[0]
        
        return None
        
    except Exception as e:
        logger.error(f"Ошибка парсинга UPC: {e}")
        return None


# ==================== APIFY (ЖАНРЫ) ====================

def get_album_genre_via_apify(album_url):
    """Получение жанров альбома через Apify"""
    if not APIFY_API_TOKEN:
        return None
    
    try:
        client = ApifyClient(APIFY_API_TOKEN)
        
        run_input = {
            "albums": [album_url],
            "albumsIncludeArtists": True,
        }
        
        run = client.actor("musicae/spotify-extended-scraper").call(run_input=run_input)
        dataset_items = client.dataset(run["defaultDatasetId"]).list_items().items
        
        if dataset_items:
            album_data = dataset_items[0]
            genres = []
            
            if album_data.get('artists'):
                for artist in album_data['artists']:
                    if artist.get('artist_genres'):
                        genres.extend(artist['artist_genres'])
            
            if not genres and album_data.get('genres'):
                genres = album_data['genres']
            
            return ", ".join(set(genres)) if genres else None
        
        return None
        
    except Exception as e:
        logger.error(f"Ошибка Apify: {e}")
        return None


# ==================== КОМАНДЫ БОТА ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = """
🎵 *Музыкальный бот* 🎵

*Доступные функции:*

📀 *Информация о релизе:*
   Отправьте ссылку на альбом или трек Spotify
   → Получите всю информацию + обложку 3000x3000 JPG

🎵 *Конвертация MP3 → WAV:*
   Отправьте MP3 файл
   → Получите WAV с тем же именем

*Примеры ссылок:*
• https://open.spotify.com/album/1W25XYjRQPob14CkgOYVms
• https://open.spotify.com/track/4iV5W9uYEdYUVa79Axb7Rh
    """
    await update.message.reply_text(welcome_text, parse_mode='Markdown')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📖 *Как пользоваться ботом:*

1️⃣ *Получить информацию о релизе:*
   Просто отправьте ссылку на Spotify

2️⃣ *Конвертировать MP3 в WAV:*
   Отправьте MP3 файл

3️⃣ *Команды:*
   /start — приветствие
   /help — эта справка
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')


async def handle_spotify_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ссылок на Spotify"""
    url = update.message.text.strip()
    
    if not any(x in url for x in ['spotify.com', 'open.spotify']):
        return
    
    album_id, type_ = extract_album_id(url)
    if not album_id:
        await update.message.reply_text("❌ Не удалось распознать ссылку")
        return
    
    status_msg = await update.message.reply_text("🔍 Получаю информацию...")
    
    try:
        # ========== 1. СНАЧАЛА ПОЛУЧАЕМ ISRC ЧЕРЕЗ ISRC FINDER ==========
        isrcs = get_isrc_from_isrcfinder(album_id)
        upc = get_upc_from_isrcfinder(album_id)
        
        # ========== 2. ПОТОМ ПОЛУЧАЕМ МЕТАДАННЫЕ ЧЕРЕЗ SPOTIFY API ==========
        # Пробуем получить данные с задержкой и повторными попытками
        album_data = None
        tracks = []
        
        for attempt in range(3):
            try:
                # Ждём перед запросом
                time.sleep(2.0 * (attempt + 1))
                
                if type_ == "track":
                    track_data = get_track_data(album_id)
                    if track_data:
                        album_data = {
                            'name': track_data.get('name', 'Неизвестно'),
                            'artists': track_data.get('artists', [{'name': 'Неизвестно'}]),
                            'release_date': track_data.get('album', {}).get('release_date', 'Неизвестно'),
                            'total_tracks': 1,
                            'images': track_data.get('album', {}).get('images', []),
                            'external_ids': {'upc': upc or 'Неизвестно'},
                        }
                        tracks = [{
                            'name': track_data.get('name', 'Без названия'),
                            'duration_ms': track_data.get('duration_ms', 0),
                            'external_ids': {'isrc': isrcs.get(track_data.get('name'), 'Не найден')}
                        }]
                        break
                else:
                    album_data, tracks = get_album_metadata(album_id)
                    if album_data:
                        # Добавляем ISRC к трекам
                        for track in tracks:
                            track_name = track.get('name', '')
                            if track_name in isrcs:
                                track['external_ids'] = {'isrc': isrcs[track_name]}
                            else:
                                track['external_ids'] = {'isrc': 'Не найден'}
                        break
                        
            except Exception as e:
                logger.warning(f"Попытка {attempt+1}/3 получить метаданные: {e}")
                if attempt == 2:
                    # Если не получилось — используем только ISRC Finder
                    album_data = {
                        'name': 'Неизвестно (используйте isrcfinder.com)',
                        'artists': [{'name': 'Неизвестно'}],
                        'release_date': 'Неизвестно',
                        'total_tracks': len(isrcs),
                        'images': [],
                        'external_ids': {'upc': upc or 'Неизвестно'},
                    }
                    # Создаём треки из ISRC
                    tracks = []
                    for i, (track_name, isrc) in enumerate(isrcs.items(), 1):
                        tracks.append({
                            'name': track_name,
                            'duration_ms': 0,
                            'external_ids': {'isrc': isrc}
                        })
                    if not tracks:
                        tracks = [{'name': 'Трек не найден', 'duration_ms': 0, 'external_ids': {'isrc': 'Не найден'}}]
        
        # Если album_data так и не получен
        if not album_data:
            album_data = {
                'name': 'Неизвестно',
                'artists': [{'name': 'Неизвестно'}],
                'release_date': 'Неизвестно',
                'total_tracks': len(isrcs) or 1,
                'images': [],
                'external_ids': {'upc': upc or 'Неизвестно'},
            }
            if not tracks:
                tracks = [{'name': 'Трек не найден', 'duration_ms': 0, 'external_ids': {'isrc': 'Не найден'}}]
        
        # ========== 3. ЖАНРЫ ==========
        genres = get_album_genre_via_apify(url)
        if not genres:
            genres = "Не указан"
        
        name = album_data.get('name', 'Неизвестно')
        artist = album_data['artists'][0]['name'] if album_data.get('artists') else 'Неизвестно'
        release_date = album_data.get('release_date', 'Неизвестно')
        total_tracks = album_data.get('total_tracks', len(tracks))
        upc = album_data.get('external_ids', {}).get('upc', upc or 'Неизвестно')
        
        # ========== ШАПКА ==========
        header = f"📀 *{name}*\n"
        header += f"👤 *Исполнитель:* {artist}\n"
        header += f"📅 *Дата выхода:* {release_date}\n"
        header += f"🎵 *Треков:* {total_tracks}\n"
        header += f"🏷️ *UPC:* `{upc}`\n"
        header += f"🎸 *Жанр:* {genres}\n\n"
        header += "*🎶 Треки:*\n"
        
        # ========== ФОРМИРУЕМ СПИСОК ТРЕКОВ ==========
        all_tracks_text = ""
        for i, track in enumerate(tracks, 1):
            track_name = track.get('name', 'Без названия')
            duration = ms_to_min_sec(track.get('duration_ms'))
            isrc = track.get('external_ids', {}).get('isrc', 'Не найден')
            
            all_tracks_text += f"{i}. `{track_name}` ({duration}) ISRC: `{isrc}`\n"
        
        # ========== ОТПРАВКА ==========
        full_text = header + all_tracks_text
        
        cover_url = None
        if album_data.get('images'):
            cover_url = album_data['images'][0]['url']
        
        await status_msg.delete()
        
        # Обложка
        if cover_url:
            cover_buffer = get_cover_image(cover_url)
            if cover_buffer:
                await update.message.reply_document(
                    document=InputFile(cover_buffer, filename="cover.jpg")
                )
        
        # Текст
        if len(full_text) <= 4096:
            await update.message.reply_text(full_text, parse_mode="Markdown")
        else:
            tracks_lines = all_tracks_text.split('\n')
            mid = len(tracks_lines) // 2
            
            first_part = header + "\n".join(tracks_lines[:mid])
            second_part = "🎵 *Продолжение списка треков:*\n\n" + "\n".join(tracks_lines[mid:])
            
            await update.message.reply_text(first_part, parse_mode="Markdown")
            await update.message.reply_text(second_part, parse_mode="Markdown")
            
    except Exception as e:
        logger.error(f"Ошибка обработки: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {str(e)}")

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Конвертация MP3 в WAV"""
    try:
        audio_file = await update.message.audio.get_file()
        
        if audio_file.file_size > MAX_FILE_SIZE:
            await update.message.reply_text("❌ Файл больше 50 МБ")
            return
        
        if not audio_file.file_path.lower().endswith('.mp3'):
            await update.message.reply_text("❌ Отправьте файл в формате MP3")
            return
        
        status_msg = await update.message.reply_text("🔄 Конвертирую MP3 → WAV...")
        
        original_name = update.message.audio.file_name or "audio"
        base_name = os.path.splitext(original_name)[0]
        
        mp3_path = os.path.join(TEMP_DIR, f"{update.message.message_id}_input.mp3")
        await audio_file.download_to_drive(mp3_path)
        
        wav_path = os.path.join(TEMP_DIR, f"{update.message.message_id}_output.wav")
        audio = AudioSegment.from_mp3(mp3_path)
        audio.export(wav_path, format="wav")
        
        with open(wav_path, 'rb') as wav_file:
            await update.message.reply_audio(
                audio=wav_file,
                filename=f"{base_name}.wav",
                title=f"{base_name}.wav",
                performer="Конвертировано ботом"
            )
        
        os.remove(mp3_path)
        os.remove(wav_path)
        
        await status_msg.delete()
        await update.message.reply_text(f"✅ Конвертация завершена!\nФайл: `{base_name}.wav`", parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Ошибка конвертации: {e}")
        await update.message.reply_text(f"❌ Ошибка конвертации: {str(e)}")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка: {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text("❌ Произошла ошибка, попробуйте позже")


# ==================== ЗАПУСК ====================

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    # === ПРАВИЛЬНОЕ УДАЛЕНИЕ ВЕБХУКА ===
    import asyncio
    try:
        asyncio.run(app.bot.delete_webhook(drop_pending_updates=True))
        print("✅ Webhook удалён")
    except Exception as e:
        print(f"⚠️ Не удалось удалить webhook: {e}")
    # ====================================
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.AUDIO & ~filters.COMMAND, handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_spotify_link))
    
    app.add_error_handler(error_handler)
    
    print("🎵 Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()