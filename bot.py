import os
import re
import logging
import tempfile
import requests
import base64
import time
import json
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


# ==================== SPOTIFY API ====================

def get_spotify_token():
    """Получение токена для Spotify API"""
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


def get_album_data(album_id):
    """Получение данных об альбоме с ISRC, explicit"""
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
                track_id = track.get("id")
                if track_id:
                    try:
                        time.sleep(0.5)
                        track_detail_resp = requests.get(
                            f"https://api.spotify.com/v1/tracks/{track_id}",
                            headers=headers,
                            timeout=30
                        )
                        track_detail_resp.raise_for_status()
                        track_detail = track_detail_resp.json()
                        
                        isrc = track_detail.get('external_ids', {}).get('isrc')
                        track['external_ids'] = {'isrc': isrc} if isrc else {}
                        track['explicit'] = track_detail.get('explicit', False)
                        track['duration_ms'] = track_detail.get('duration_ms', track.get('duration_ms'))
                        
                        track['track_artists'] = []
                        for artist in track_detail.get('artists', []):
                            track['track_artists'].append({
                                'name': artist.get('name', 'Неизвестно'),
                                'id': artist.get('id')
                            })
                        
                    except Exception as e:
                        logger.warning(f"Не удалось получить данные для трека {track_id}: {e}")
                        track['external_ids'] = {}
                        track['explicit'] = False
                        track['track_artists'] = []
                
                tracks.append(track)
            
            if len(tracks_data.get("items", [])) < limit:
                break
            offset += limit
        
        return album_data, tracks
        
    except Exception as e:
        logger.error(f"Ошибка получения данных Spotify: {e}")
        return None, None


def get_track_data(track_id):
    """Получение данных о треке с explicit-информацией"""
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
        track_data = track_resp.json()
        
        if 'explicit' not in track_data:
            track_data['explicit'] = track_data.get('explicit', False)
        
        return track_data
        
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


# ==================== APPLE MUSIC (ССЫЛКИ) ====================

def get_apple_music_artist_url(artist_name):
    """Получение ссылки на Apple Music через поиск"""
    try:
        encoded_name = quote_plus(artist_name)
        url = f"https://itunes.apple.com/search?term={encoded_name}&entity=musicArtist&limit=1"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get('results') and len(data['results']) > 0:
            artist_id = data['results'][0].get('artistId')
            if artist_id:
                return f"https://music.apple.com/artist/{artist_id}"
        
        return None
        
    except Exception as e:
        logger.error(f"Ошибка получения Apple Music ссылки для {artist_name}: {e}")
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
   → Бот определит наличие нецензурной лексики (🔞)
   → Ссылки на Spotify и Apple Music для каждого исполнителя

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

*ℹ️ Информация о explicit:*
🔞 — трек содержит нецензурную лексику
✅ — трек без нецензурной лексики
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
        if type_ == "track":
            track_data = get_track_data(album_id)
            if not track_data:
                await status_msg.edit_text("❌ Не удалось получить данные трека")
                return
            
            track_artists = []
            for artist in track_data.get('artists', []):
                track_artists.append(artist.get('name', 'Неизвестно'))
            
            album_artists = []
            for artist in track_data.get('album', {}).get('artists', []):
                artist_id = artist.get('id')
                artist_name = artist.get('name', 'Неизвестно')
                spotify_url = f"https://open.spotify.com/artist/{artist_id}" if artist_id else None
                
                apple_url = None
                try:
                    time.sleep(0.3)
                    apple_url = get_apple_music_artist_url(artist_name)
                except Exception as e:
                    logger.warning(f"Apple Music ссылка для {artist_name}: {e}")
                
                album_artists.append({
                    'name': artist_name,
                    'id': artist_id,
                    'spotify_url': spotify_url,
                    'apple_url': apple_url
                })
            
            track_data['explicit'] = track_data.get('explicit', False)
            track_data['track_artists'] = track_artists
            
            album_data = {
                'name': track_data.get('name', 'Неизвестно'),
                'artists': album_artists,
                'release_date': track_data.get('album', {}).get('release_date', 'Неизвестно'),
                'total_tracks': 1,
                'images': track_data.get('album', {}).get('images', []),
                'external_ids': {'upc': 'Неизвестно'},
            }
            
            tracks = [track_data]
            
        else:
            album_data, tracks = get_album_data(album_id)
            if not album_data:
                await status_msg.edit_text("❌ Не удалось получить данные альбома")
                return
            
            album_artists = []
            for artist in album_data.get('artists', []):
                artist_id = artist.get('id')
                artist_name = artist.get('name', 'Неизвестно')
                spotify_url = f"https://open.spotify.com/artist/{artist_id}" if artist_id else None
                
                apple_url = None
                try:
                    time.sleep(0.3)
                    apple_url = get_apple_music_artist_url(artist_name)
                except Exception as e:
                    logger.warning(f"Apple Music ссылка для {artist_name}: {e}")
                
                album_artists.append({
                    'name': artist_name,
                    'id': artist_id,
                    'spotify_url': spotify_url,
                    'apple_url': apple_url
                })
            album_data['artists'] = album_artists
        
        genres = get_album_genre_via_apify(url)
        if not genres:
            genres = "Не указан"
        
        name = album_data.get('name', 'Неизвестно')
        
        album_artist_text = ""
        artists_list = album_data.get('artists', [{'name': 'Неизвестно'}])
        for i, artist in enumerate(artists_list):
            artist_name = artist.get('name', 'Неизвестно')
            spotify_url = artist.get('spotify_url')
            apple_url = artist.get('apple_url')
            
            links = []
            if spotify_url:
                links.append(f"[Spotify]({spotify_url})")
            if apple_url:
                links.append(f"[Apple]({apple_url})")
            
            if links:
                album_artist_text += f"{artist_name} ({', '.join(links)})"
            else:
                album_artist_text += artist_name
            
            if i < len(artists_list) - 1:
                album_artist_text += ", "
        
        release_date = album_data.get('release_date', 'Неизвестно')
        total_tracks = album_data.get('total_tracks', len(tracks))
        upc = album_data.get('external_ids', {}).get('upc', 'Неизвестно')
        
        explicit_album = any(track.get('explicit', False) for track in tracks)
        explicit_text = "🔞 Содержит нецензурную лексику" if explicit_album else "✅ Без нецензурной лексики"
        
        header = f"📀 *{name}*\n"
        header += f"👤 *Исполнитель:* {album_artist_text}\n"
        header += f"📅 *Дата выхода:* {release_date}\n"
        header += f"🎵 *Треков:* {total_tracks}\n"
        header += f"🏷️ *UPC:* `{upc}`\n"
        header += f"🎸 *Жанр:* {genres}\n"
        header += f"{explicit_text}\n\n"
        header += "*🎶 Треки:*\n"
        
        all_tracks_text = ""
        for i, track in enumerate(tracks, 1):
            track_name = track.get('name', 'Без названия')
            duration = ms_to_min_sec(track.get('duration_ms'))
            isrc = track.get('external_ids', {}).get('isrc', 'Не найден')
            is_explicit = track.get('explicit', False)
            
            track_artists = track.get('track_artists', [])
            if track_artists and len(track_artists) > 0:
                if isinstance(track_artists[0], dict):
                    artists_str = ", ".join([a.get('name', 'Неизвестно') for a in track_artists])
                else:
                    artists_str = ", ".join(track_artists)
            else:
                artists_str = ", ".join([a.get('name', 'Неизвестно') for a in album_data.get('artists', [{'name': 'Неизвестно'}])])
            
            explicit_icon = "🔞 " if is_explicit else "✅ "
            
            if isrc != 'Не найден':
                all_tracks_text += f"{i}. {explicit_icon}`{track_name}` — *{artists_str}* ({duration}) ISRC: `{isrc}`\n"
            else:
                all_tracks_text += f"{i}. {explicit_icon}`{track_name}` — *{artists_str}* ({duration}) ISRC: Не найден\n"
        
        full_text = header + all_tracks_text
        
        cover_url = None
        if album_data.get('images'):
            cover_url = album_data['images'][0]['url']
        
        await status_msg.delete()
        
        if cover_url:
            cover_buffer = get_cover_image(cover_url)
            if cover_buffer:
                await update.message.reply_document(
                    document=InputFile(cover_buffer, filename="cover.jpg")
                )
        
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
        logger.error(f"Ошибка обработки Spotify: {e}")
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
    app = Application.builder().token(BOT_TOKEN).connect_timeout(60.0).read_timeout(60.0).build()
    
    try:
        app.bot.delete_webhook(drop_pending_updates=True)
        print("✅ Webhook удалён")
    except Exception as e:
        print(f"⚠️ Не удалось удалить webhook: {e}")
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.AUDIO & ~filters.COMMAND, handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_spotify_link))
    
    app.add_error_handler(error_handler)
    
    print("🎵 Бот запущен!")
    print(f"   BOT_TOKEN: {'✅' if BOT_TOKEN else '❌'}")
    print(f"   SPOTIFY_API: {'✅' if SPOTIFY_CLIENT_ID else '❌'}")
    print(f"   APIFY_API: {'✅' if APIFY_API_TOKEN else '❌'}")
    app.run_polling(timeout=60)


if __name__ == "__main__":
    main()