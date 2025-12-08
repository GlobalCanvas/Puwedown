import os
import re
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, CommandHandler, ContextTypes, filters
import yt_dlp
import asyncio

# Bot configuration
BOT_TOKEN = "8577140991:AAGJJZLA2H88z3MUbqcHRxJRr7NK46N5-L8"

# Supported platforms
VIDEO_URL_PATTERN = re.compile(
    r'https?://(?:www\.)?(?:youtube\.com|youtu\.be|(?:vt\.)?tiktok\.com|vm\.tiktok\.com|instagram\.com|twitter\.com|x\.com|facebook\.com|fb\.watch|vimeo\.com|dailymotion\.com)/\S+|'
    r'https?://(?:www\.)?reddit\.com/\S+|'
    r'https?://(?:clips\.)?twitch\.tv/\S+'
)

# Translations
TRANSLATIONS = {
    'en': {
        'welcome': (
            "🎥 *Video Downloader Bot* 🎥\n\n"
            "Send me a video link and I'll help you download it!\n\n"
            "✨ *Supported platforms:*\n"
            "• YouTube\n"
            "• TikTok\n"
            "• Instagram\n"
            "• Twitter/X\n"
            "• Facebook\n"
            "• Vimeo\n"
            "• And more!\n\n"
            "Just paste a link and choose your preferred quality! 🚀\n\n"
            "Use /settings to change language"
        ),
        'analyzing': "🔍 *Analyzing video...*\n\nPlease wait...",
        'video_found': "✅ *Video Found!*",
        'title': "📝 *Title:*",
        'duration': "⏱ *Duration:*",
        'choose_quality': "🎯 *Choose quality:*",
        'cancel': "❌ Cancel",
        'cancelled': "❌ Download cancelled.",
        'downloading': "⬇️ *Downloading...*",
        'format': "🎯 Format:",
        'wait': "Please wait, this may take a moment... ⏳",
        'uploading': "📤 *Uploading...*",
        'complete': "✅ *Download complete!*",
        'error': "❌ *Error:*",
        'error_process': "Could not process this video.\n\nPlease make sure the link is valid and accessible.",
        'error_download': "Error during download.\n\nPlease try again later.",
        'session_expired': "Session expired. Please send the link again.",
        'file_too_large': "File too large to send via Telegram (>50MB).",
        'download_failed': "Download failed. Please try again.",
        'settings': "⚙️ *Settings*\n\nChoose your language:",
        'language_changed': "✅ Language changed to English!",
        'unknown': "Unknown"
    },
    'uk': {
        'welcome': (
            "🎥 *Бот для завантаження відео* 🎥\n\n"
            "Надішліть мені посилання на відео, і я допоможу вам його завантажити!\n\n"
            "✨ *Підтримувані платформи:*\n"
            "• YouTube\n"
            "• TikTok\n"
            "• Instagram\n"
            "• Twitter/X\n"
            "• Facebook\n"
            "• Vimeo\n"
            "• Та інші!\n\n"
            "Просто вставте посилання та оберіть бажану якість! 🚀\n\n"
            "Використовуйте /settings для зміни мови"
        ),
        'analyzing': "🔍 *Аналізую відео...*\n\nБудь ласка, зачекайте...",
        'video_found': "✅ *Відео знайдено!*",
        'title': "📝 *Назва:*",
        'duration': "⏱ *Тривалість:*",
        'choose_quality': "🎯 *Оберіть якість:*",
        'cancel': "❌ Скасувати",
        'cancelled': "❌ Завантаження скасовано.",
        'downloading': "⬇️ *Завантажую...*",
        'format': "🎯 Формат:",
        'wait': "Будь ласка, зачекайте, це може зайняти деякий час... ⏳",
        'uploading': "📤 *Відправляю...*",
        'complete': "✅ *Завантаження завершено!*",
        'error': "❌ *Помилка:*",
        'error_process': "Не вдалося обробити це відео.\n\nПереконайтеся, що посилання дійсне та доступне.",
        'error_download': "Помилка під час завантаження.\n\nБудь ласка, спробуйте пізніше.",
        'session_expired': "Сесія закінчилася. Будь ласка, надішліть посилання знову.",
        'file_too_large': "Файл занадто великий для відправки через Telegram (>50MB).",
        'download_failed': "Завантаження не вдалося. Спробуйте ще раз.",
        'settings': "⚙️ *Налаштування*\n\nОберіть мову:",
        'language_changed': "✅ Мову змінено на Українську!",
        'unknown': "Невідомо"
    },
    'ru': {
        'welcome': (
            "🎥 *Бот для скачивания видео* 🎥\n\n"
            "Отправьте мне ссылку на видео, и я помогу вам его скачать!\n\n"
            "✨ *Поддерживаемые платформы:*\n"
            "• YouTube\n"
            "• TikTok\n"
            "• Instagram\n"
            "• Twitter/X\n"
            "• Facebook\n"
            "• Vimeo\n"
            "• И другие!\n\n"
            "Просто вставьте ссылку и выберите желаемое качество! 🚀\n\n"
            "Используйте /settings для смены языка"
        ),
        'analyzing': "🔍 *Анализирую видео...*\n\nПожалуйста, подождите...",
        'video_found': "✅ *Видео найдено!*",
        'title': "📝 *Название:*",
        'duration': "⏱ *Длительность:*",
        'choose_quality': "🎯 *Выберите качество:*",
        'cancel': "❌ Отменить",
        'cancelled': "❌ Загрузка отменена.",
        'downloading': "⬇️ *Скачиваю...*",
        'format': "🎯 Формат:",
        'wait': "Пожалуйста, подождите, это может занять некоторое время... ⏳",
        'uploading': "📤 *Отправляю...*",
        'complete': "✅ *Загрузка завершена!*",
        'error': "❌ *Ошибка:*",
        'error_process': "Не удалось обработать это видео.\n\nУбедитесь, что ссылка действительна и доступна.",
        'error_download': "Ошибка при загрузке.\n\nПожалуйста, попробуйте позже.",
        'session_expired': "Сессия истекла. Пожалуйста, отправьте ссылку снова.",
        'file_too_large': "Файл слишком большой для отправки через Telegram (>50MB).",
        'download_failed': "Загрузка не удалась. Попробуйте еще раз.",
        'settings': "⚙️ *Настройки*\n\nВыберите язык:",
        'language_changed': "✅ Язык изменен на Русский!",
        'unknown': "Неизвестно"
    }
}

# User settings storage
USER_SETTINGS_FILE = "user_settings.json"

def load_user_settings():
    """Load user settings from file"""
    if os.path.exists(USER_SETTINGS_FILE):
        with open(USER_SETTINGS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_user_settings(settings):
    """Save user settings to file"""
    with open(USER_SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)

def get_user_language(user_id):
    """Get user's language preference"""
    settings = load_user_settings()
    return settings.get(str(user_id), 'en')

def set_user_language(user_id, language):
    """Set user's language preference"""
    settings = load_user_settings()
    settings[str(user_id)] = language
    save_user_settings(settings)

def t(user_id, key):
    """Get translation for user"""
    lang = get_user_language(user_id)
    return TRANSLATIONS[lang].get(key, TRANSLATIONS['en'][key])

class VideoDownloader:
    def __init__(self):
        self.downloads = {}
    
    def get_video_info(self, url):
        """Extract video information"""
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'cookiefile': None,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'extractor_args': {
                'tiktok': {
                    'api_hostname': 'api22-normal-c-useast2a.tiktokv.com'
                }
            }
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info
    
    def get_format_options(self, info):
        """Get available quality options"""
        formats = []
        seen = set()
        
        for f in info.get('formats', []):
            height = f.get('height')
            ext = f.get('ext')
            format_id = f.get('format_id')
            vcodec = f.get('vcodec', 'none')
            acodec = f.get('acodec', 'none')
            
            # Video formats
            if height and vcodec != 'none' and ext in ['mp4', 'webm']:
                quality = f"{height}p"
                if quality not in seen:
                    formats.append({
                        'type': 'video',
                        'quality': quality,
                        'format_id': format_id,
                        'ext': ext
                    })
                    seen.add(quality)
        
        # Audio format
        formats.append({
            'type': 'audio',
            'quality': 'Audio Only',
            'format_id': 'bestaudio',
            'ext': 'mp3'
        })
        
        # Sort by quality (descending)
        video_formats = sorted(
            [f for f in formats if f['type'] == 'video'],
            key=lambda x: int(x['quality'].replace('p', '')),
            reverse=True
        )
        audio_formats = [f for f in formats if f['type'] == 'audio']
        
        return video_formats[:5] + audio_formats  # Top 5 video qualities + audio
    
    async def download_video(self, url, format_id, output_path):
        """Download video with specified format"""
        if format_id == 'bestaudio':
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': output_path,
                'quiet': True,
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'extractor_args': {
                    'tiktok': {
                        'api_hostname': 'api22-normal-c-useast2a.tiktokv.com'
                    }
                }
            }
        else:
            ydl_opts = {
                'format': f'{format_id}+bestaudio/best',
                'outtmpl': output_path,
                'quiet': True,
                'merge_output_format': 'mp4',
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'extractor_args': {
                    'tiktok': {
                        'api_hostname': 'api22-normal-c-useast2a.tiktokv.com'
                    }
                }
            }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            await asyncio.to_thread(ydl.download, [url])

downloader = VideoDownloader()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    user_id = update.effective_user.id
    welcome_text = t(user_id, 'welcome')
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show settings menu"""
    user_id = update.effective_user.id
    current_lang = get_user_language(user_id)
    
    keyboard = [
        [
            InlineKeyboardButton("🇬🇧 English" + (" ✓" if current_lang == 'en' else ""), callback_data="lang_en"),
            InlineKeyboardButton("🇺🇦 Українська" + (" ✓" if current_lang == 'uk' else ""), callback_data="lang_uk")
        ],
        [
            InlineKeyboardButton("🇷🇺 Русский" + (" ✓" if current_lang == 'ru' else ""), callback_data="lang_ru")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        t(user_id, 'settings'),
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages with video links"""
    user_id = update.effective_user.id
    text = update.message.text
    
    # Find URL in text
    url_match = VIDEO_URL_PATTERN.search(text)
    if not url_match:
        return
    
    # Extract the complete URL
    url_start = url_match.start()
    url_text = text[url_start:].split()[0]
    url = url_text
    
    # Send processing message
    processing_msg = await update.message.reply_text(
        t(user_id, 'analyzing'),
        parse_mode='Markdown'
    )
    
    try:
        # Get video info
        info = await asyncio.to_thread(downloader.get_video_info, url)
        title = info.get('title', t(user_id, 'unknown'))[:50]
        thumbnail = info.get('thumbnail', '')
        duration = info.get('duration', 0)
        
        # Get format options
        formats = downloader.get_format_options(info)
        
        # Store data for callback
        chat_id = update.effective_chat.id
        downloader.downloads[chat_id] = {
            'url': url,
            'title': title,
            'formats': formats
        }
        
        # Create keyboard
        keyboard = []
        for fmt in formats:
            emoji = "🎬" if fmt['type'] == 'video' else "🎵"
            button_text = f"{emoji} {fmt['quality']} ({fmt['ext']})"
            callback_data = f"dl_{fmt['format_id']}_{fmt['ext']}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        keyboard.append([InlineKeyboardButton(t(user_id, 'cancel'), callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Format duration
        mins, secs = divmod(duration, 60)
        duration_str = f"{int(mins)}:{int(secs):02d}" if duration else t(user_id, 'unknown')
        
        # Send options
        caption = (
            f"{t(user_id, 'video_found')}\n\n"
            f"{t(user_id, 'title')} {title}\n"
            f"{t(user_id, 'duration')} {duration_str}\n\n"
            f"{t(user_id, 'choose_quality')}"
        )
        
        if thumbnail:
            await processing_msg.delete()
            await update.message.reply_photo(
                photo=thumbnail,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await processing_msg.edit_text(
                caption,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
    
    except Exception as e:
        await processing_msg.edit_text(
            f"{t(user_id, 'error')} {t(user_id, 'error_process')}",
            parse_mode='Markdown'
        )
        print(f"Error: {e}")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Handle language change
    if query.data.startswith('lang_'):
        lang = query.data.split('_')[1]
        set_user_language(user_id, lang)
        
        keyboard = [
            [
                InlineKeyboardButton("🇬🇧 English" + (" ✓" if lang == 'en' else ""), callback_data="lang_en"),
                InlineKeyboardButton("🇺🇦 Українська" + (" ✓" if lang == 'uk' else ""), callback_data="lang_uk")
            ],
            [
                InlineKeyboardButton("🇷🇺 Русский" + (" ✓" if lang == 'ru' else ""), callback_data="lang_ru")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"{t(user_id, 'settings')}\n\n{t(user_id, 'language_changed')}",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    
    if query.data == "cancel":
        await query.edit_message_caption(
            caption=t(user_id, 'cancelled'),
            parse_mode='Markdown'
        )
        return
    
    # Parse callback data
    parts = query.data.split('_')
    if len(parts) < 3:
        await query.edit_message_caption(
            caption=t(user_id, 'error'),
            parse_mode='Markdown'
        )
        return
    
    # Extract format_id and ext (ext is always last, format_id is everything in between)
    ext = parts[-1]
    format_id = '_'.join(parts[1:-1])
    
    # Get download info
    download_info = downloader.downloads.get(chat_id)
    if not download_info:
        await query.edit_message_caption(
            caption=t(user_id, 'session_expired'),
            parse_mode='Markdown'
        )
        return
    
    # Update message
    await query.edit_message_caption(
        caption=(
            f"{t(user_id, 'downloading')}\n\n"
            f"{t(user_id, 'title')} {download_info['title']}\n"
            f"{t(user_id, 'format')} {ext.upper()}\n\n"
            f"{t(user_id, 'wait')}"
        ),
        parse_mode='Markdown'
    )
    
    file_path = None
    try:
        # Download video
        output_path = f"downloads/{chat_id}_{format_id}.%(ext)s"
        os.makedirs("downloads", exist_ok=True)
        
        await downloader.download_video(
            download_info['url'],
            format_id,
            output_path
        )
        
        # Find downloaded file
        base_path = f"downloads/{chat_id}_{format_id}"
        for possible_ext in [ext, 'mp4', 'mp3', 'webm', 'm4a']:
            test_path = f"{base_path}.{possible_ext}"
            if os.path.exists(test_path):
                file_path = test_path
                break
        
        if file_path and os.path.exists(file_path):
            # Check file size
            file_size = os.path.getsize(file_path)
            if file_size > 50 * 1024 * 1024:  # 50MB limit
                await query.edit_message_caption(
                    caption=f"{t(user_id, 'error')} {t(user_id, 'file_too_large')}",
                    parse_mode='Markdown'
                )
            else:
                # Send file
                await query.edit_message_caption(
                    caption=t(user_id, 'uploading'),
                    parse_mode='Markdown'
                )
                
                with open(file_path, 'rb') as f:
                    if ext == 'mp3':
                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=f,
                            title=download_info['title']
                        )
                    else:
                        await context.bot.send_video(
                            chat_id=chat_id,
                            video=f,
                            caption=f"✅ {download_info['title']}"
                        )
                
                await query.edit_message_caption(
                    caption=t(user_id, 'complete'),
                    parse_mode='Markdown'
                )
        else:
            await query.edit_message_caption(
                caption=f"{t(user_id, 'error')} {t(user_id, 'download_failed')}",
                parse_mode='Markdown'
            )
    
    except Exception as e:
        await query.edit_message_caption(
            caption=f"{t(user_id, 'error')} {t(user_id, 'error_download')}",
            parse_mode='Markdown'
        )
        print(f"Download error: {e}")
    
    finally:
        # Always delete the file after sending (or if error occurred)
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                print(f"Deleted file: {file_path}")
            except Exception as e:
                print(f"Error deleting file: {e}")

def main():
    """Start the bot"""
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", settings))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Start bot
    print("🤖 Bot started! Waiting for messages...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
