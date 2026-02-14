import os
import logging
import asyncio
import json
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

# --- CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # Load from Environment Variable
if not TOKEN:
    print("Error: TELEGRAM_BOT_TOKEN not found in environment variables.")

# --- LOGGING ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- DUMMY SERVER FOR RENDER (PREVENTS PORT BINDING ERROR) ---
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Bot is running!')

def start_web_server():
    port = int(os.environ.get('PORT', 8080))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    print(f"Starting dummy web server on port {port}")
    server.serve_forever()

# --- HELPER: FORMAT SIZE ---
def human_readable_size(size, decimal_places=2):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.{decimal_places}f} {unit}"
        size /= 1024.0
    return f"{size:.{decimal_places}f} TB"

# --- HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hi! Send me a YouTube (or other supported) link, and I'll let you choose the quality.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    status_msg = await update.message.reply_text("🔍 Checking link...")

    ydl_opts = {'quiet': True, 'no_warnings': True}
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # extract_info with download=False is fast
            info = ydl.extract_info(url, download=False)
            
            # Filter formats: we want video with audio or mergeable streams
            formats = info.get('formats', [])
            title = info.get('title', 'Unknown Title')
            thumb = info.get('thumbnail', None)
            duration = info.get('duration', 0)
            
            # Generate unique quality buttons
            keyboard = []
            seen_res = set()
            
            # 1. Add Audio Option
            keyboard.append([InlineKeyboardButton(f"🎵 MP3 / Audio", callback_data=f"audio|{info['id']}")])

            # 2. Add Video Options (Best logical ones)
            # We sort formats by height (resolution) descending
            sorted_formats = sorted([f for f in formats if f.get('height')], key=lambda x: x['height'], reverse=True)
            
            for f in sorted_formats:
                res = f.get('height')
                if res and res not in seen_res:
                    # Limit button count to avoid clutter
                    if len(seen_res) < 5: 
                        btn_text = f"🎬 {res}p"
                        # callback data: type|video_id|resolution
                        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"video|{info['id']}|{res}")])
                        seen_res.add(res)
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Send thumbnail with buttons
            if thumb:
                await update.message.reply_photo(photo=thumb, caption=f"📹 **{title}**\n\nSelect a format:", reply_markup=reply_markup, parse_mode='Markdown')
            else:
                await update.message.reply_text(f"📹 **{title}**\n\nSelect a format:", reply_markup=reply_markup, parse_mode='Markdown')
            
            await status_msg.delete()

    except Exception as e:
        logger.error(e)
        await status_msg.edit_text(f"❌ Error: {str(e)}\nLink might not be supported.")

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # Ack the callback
    
    data = query.data.split('|')
    type_ = data[0]
    vid_id = data[1]
    
    url = f"https://www.youtube.com/watch?v={vid_id}" # Reconstruct URL
    
    await query.edit_message_reply_markup(reply_markup=None) # Remove buttons
    status_msg = await query.message.reply_text(f"⬇️ Downloading {type_}... This might take a moment.")
    
    # Define generic options
    # We use a cookies file if available for better stability
    ydl_opts = {
        'outtmpl': f'downloads/%(id)s_%(height)s.%(ext)s',
        'quiet': True,
        # 'cookies': 'cookies.txt', # Optional: Use if you get "Sign in to confirm you're not a bot"
    }

    try:
        if type_ == 'audio':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'outtmpl': f'downloads/%(id)s.%(ext)s',
            })
        elif type_ == 'video':
            res = data[2]
            # Download specific height video + best audio available and merge
            ydl_opts.update({
                'format': f'bestvideo[height={res}]+bestaudio/best[height={res}]/best',
                'merge_output_format': 'mp4'
            })

        # --- DOWNLOAD ---
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Find the actual filename generated
            if 'requested_downloads' in info:
                filename = info['requested_downloads'][0]['filepath']
            else:
                # Fallback calculation
                filename = ydl.prepare_filename(info)
                if type_ == 'audio': 
                    filename = filename.rsplit('.', 1)[0] + '.mp3'
        
        # --- SEND ---
        await status_msg.edit_text("⬆️ Uploading to Telegram...")
        
        if type_ == 'audio':
            await query.message.reply_audio(audio=open(filename, 'rb'), title=info.get('title', 'Audio'))
        else:
            await query.message.reply_video(video=open(filename, 'rb'), caption=info.get('title', 'Video'))
            
        await status_msg.delete()

        # --- DELETE ---
        if os.path.exists(filename):
            os.remove(filename)
            print(f"Deleted {filename}")

    except Exception as e:
        logger.error(e)
        await status_msg.edit_text(f"❌ Download failed: {str(e)}")
        # Cleanup if failed
        if 'filename' in locals() and os.path.exists(filename):
            os.remove(filename)

# --- MAIN ---
if __name__ == '__main__':
    # Start Dummy Server in Background Thread
    if os.getenv("RENDER"):
        t = Thread(target=start_web_server)
        t.daemon = True
        t.start()

    # Create Application
    application = Application.builder().token(TOKEN).build()

    # Add Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_click))

    # Run
    print("Bot is running...")
    application.run_polling()
