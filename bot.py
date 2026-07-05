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


# ==================== SPOTIFY API ====================

def get_spotify_token():
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
    """Получение данных альбома через Spotify API"""
    time.sleep(10.0)  # Ждём 10 секунд
    
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
                # Получаем ISRC для каждого трека
                track_id = track.get("id")
                isrc = "Не найден"
                duration_ms = track.get('duration_ms', 0)
                
                if track_id:
                    try:
                        time.sleep(5.0)  # 5 секунд между запросами треков
                        track_detail_resp = requests.get(
                            f"https://api.spotify.com/v1/tracks/{track_id}",
                            headers=headers,
                            timeout=30
                        )
                        if track_detail_resp.status_code == 429:
                            logger.warning("Rate limit 429, ждём 30 секунд...")
                            time.sleep(30)
                            continue
                        track_detail_resp.raise_for_status()
                        track_detail = track_detail_resp.json()
                        isrc = track_detail.get('external_ids', {}).get('isrc', 'Не найден')
                        duration_ms = track_detail.get('duration_ms', duration_ms)
                    except Exception as e:
                        logger.warning(f"Не удалось получить ISRC для {track_id}: {e}")
                
                tracks.append({
                    'name': track.get('name', 'Без названия'),
                    'duration_ms': duration_ms,
                    'id': track.get('id'),
                    'isrc': isrc
                })
            
            if len(tracks_data.get("items", [])) < limit:
                break
            offset += limit
        
        return album_data, tracks
        
    except Exception as e:
        logger.error(f"Ошибка получения данных альбома: {e}")
        return None, None


def get_track_data(track_id):
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
    if not ms:
        return "0:00"
    total_sec = int(ms / 1000)
    minutes = total_sec // 60
    seconds = total_sec % 60
    return f"{minutes}:{seconds:02d}"


# ==================== APIFY (ЖАНРЫ + ISRC) ====================

def get_album_data_from_apify(album_url):
    """Получение данных альбома через Apify (жанры + ISRC)"""
    if not APIFY_API_TOKEN:
        return None, None, None
    
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
            
            # Жанры
            genres = []
            if album_data.get('artists'):
                for artist in album_data['artists']:
                    if artist.get('artist_genres'):
                        genres.extend(artist['artist_genres'])
            if not genres and album_data.get('genres'):
                genres = album_data['genres']
            genres_str = ", ".join(set(genres)) if genres else "Не указан"
            
            # Треки с ISRC
            tracks = []
            if album_data.get('tracks'):
                for track in album_data['tracks']:
                    tracks.append({
                        'name': track.get('name', 'Без названия'),
                        'duration_ms': track.get('duration_ms', 0),
                        'isrc': track.get('isrc', 'Не найден')
                    })
            
            # UPC
            upc = album_data.get('upc', 'Неизвестно')
            
            return genres_str, tracks, upc
        
        return None, None, None
        
    except Exception as e:
        logger.error(f"Ошибка Apify: {e}")
        return None, None, None


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
    """
    await update.message.reply_text(welcome_text, parse_mode='Markdown')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📖 *Как пользоваться ботом:*

1️⃣ *Получить информацию о релизе:*
   Просто отправьте ссылку на Spotify

2️⃣ *Конвертировать MP3 в WAV:*
   Отправьте MP3 файл
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')


async def handle_spotify_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    
    if not any(x in url for x in ['spotify.com', 'open.spotify']):
        return
    
    album_id, type_ = extract_album_id(url)
    if not album_id:
        await update.message.reply_text("❌ Не удалось распознать ссылку")
        return
    
    status_msg = await update.message.reply_text("🔍 Получаю информацию...")
    
    try:
        # Пробуем получить данные через Apify (жанры + ISRC)
        genres, apify_tracks, upc = get_album_data_from_apify(url)
        
        # Получаем метаданные через Spotify API
        album_data, spotify_tracks = get_album_metadata(album_id)
        
        # Объединяем данные
        if album_data:
            name = album_data.get('name', 'Неизвестно')
            artist = album_data['artists'][0]['name'] if album_data.get('artists') else 'Неизвестно'
            release_date = album_data.get('release_date', 'Неизвестно')
            total_tracks = album_data.get('total_tracks', len(spotify_tracks))
            cover_url = album_data['images'][0]['url'] if album_data.get('images') else None
        else:
            name = 'Неизвестно'
            artist = 'Неизвестно'
            release_date = 'Неизвестно'
            total_tracks = len(apify_tracks) if apify_tracks else 0
            cover_url = None
        
        # Берем треки: сначала из Apify (с ISRC), потом из Spotify (если нет ISRC)
        tracks = []
        if apify_tracks:
            tracks = apify_tracks
        elif spotify_tracks:
            tracks = spotify_tracks
        else:
            tracks = [{'name': 'Трек не найден', 'duration_ms': 0, 'isrc': 'Не найден'}]
        
        if not upc:
            upc = 'Неизвестно'
        if not genres:
            genres = 'Не указан'
        
        # Шапка
        header = f"📀 *{name}*\n"
        header += f"👤 *Исполнитель:* {artist}\n"
        header += f"📅 *Дата выхода:* {release_date}\n"
        header += f"🎵 *Треков:* {total_tracks}\n"
        header += f"🏷️ *UPC:* `{upc}`\n"
        header += f"🎸 *Жанр:* {genres}\n\n"
        header += "*🎶 Треки:*\n"
        
        # Список треков
        all_tracks_text = ""
        for i, track in enumerate(tracks, 1):
            track_name = track.get('name', 'Без названия')
            duration = ms_to_min_sec(track.get('duration_ms'))
            isrc = track.get('isrc', 'Не найден')
            all_tracks_text += f"{i}. `{track_name}` ({duration}) ISRC: `{isrc}`\n"
        
        full_text = header + all_tracks_text
        
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
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.AUDIO & ~filters.COMMAND, handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_spotify_link))
    
    app.add_error_handler(error_handler)
    
    print("🎵 Бот запущен!")
    print(f"   BOT_TOKEN: {'✅' if BOT_TOKEN else '❌'}")
    print(f"   SPOTIFY_API: {'✅' if SPOTIFY_CLIENT_ID else '❌'}")
    print(f"   APIFY_API: {'✅' if APIFY_API_TOKEN else '❌'}")
    app.run_polling()


if __name__ == "__main__":
    main()