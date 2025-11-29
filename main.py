import os
import asyncio
import logging
import time
import math
import re
import mimetypes
from telethon import TelegramClient, events, utils, errors
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo, DocumentAttributeAudio
from aiohttp import web

# --- CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH")
STRING_SESSION = os.environ.get("STRING_SESSION") 
BOT_TOKEN = os.environ.get("BOT_TOKEN")           
PORT = int(os.environ.get("PORT", 7860))

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- CLIENT SETUP ---
user_client = TelegramClient(
    StringSession(STRING_SESSION), 
    API_ID, 
    API_HASH, 
    connection_retries=None, 
    flood_sleep_threshold=60,
    request_retries=10,
    use_ipv6=True 
)
bot_client = TelegramClient(
    'bot_session', 
    API_ID, 
    API_HASH, 
    connection_retries=None, 
    flood_sleep_threshold=60,
    request_retries=10,
    use_ipv6=True
)

# --- GLOBAL STATE ---
pending_requests = {} 
current_task = None
is_running = False
status_message = None
last_update_time = 0

# --- WEB SERVER ---
async def handle(request):
    return web.Response(text="Bot is Running (Auto-Refresh Mode)! 🛡️")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")

# --- HELPER FUNCTIONS ---
def human_readable_size(size):
    if not size: return "0B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0: return f"{size:.2f}{unit}"
        size /= 1024.0
    return f"{size:.2f}TB"

def time_formatter(seconds):
    if seconds is None or seconds < 0: return "..."
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0: return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"

# --- PROGRESS CALLBACK ---
async def progress_callback(current, total, start_time, file_name):
    global last_update_time, status_message
    now = time.time()
    
    if now - last_update_time < 5: return 
    last_update_time = now
    
    percentage = current * 100 / total if total > 0 else 0
    time_diff = now - start_time
    speed = current / time_diff if time_diff > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0
    
    filled = math.floor(percentage / 10)
    bar = "█" * filled + "░" * (10 - filled)
    
    try:
        await status_message.edit(
            f"⚡️ **High Speed Transfer...**\n"
            f"📂 `{file_name}`\n"
            f"**{bar} {round(percentage, 1)}%**\n"
            f"🚀 `{human_readable_size(speed)}/s` | ⏳ `{time_formatter(eta)}`\n"
            f"💾 `{human_readable_size(current)} / {human_readable_size(total)}`"
        )
    except Exception: pass

# --- ROBUST STREAMER (Speed + Reliability) ---
class RobustHighSpeedStream:
    def __init__(self, client, message, file_size, file_name, start_time):
        self.client = client
        self.message = message
        self.file_size = file_size
        self.name = file_name
        self.start_time = start_time
        self.current_bytes = 0
        
        # 8MB Chunks (Best for Speed)
        self.chunk_size = 8 * 1024 * 1024 
        
        # 10 Chunks Buffer = 80MB RAM Usage (Safe for Free Tier)
        # 288MB risk hai ki server crash ho jaye, 80MB safe aur fast hai
        self.queue = asyncio.Queue(maxsize=10)
        self.downloader_task = asyncio.create_task(self._worker())
        self.buffer = b""

    async def _refresh_file_reference(self):
        """Refreshes the file reference if it expires"""
        try:
            logger.info(f"Refreshing file reference for {self.name}...")
            # Fetch the message again to get fresh media object
            fresh_msgs = await self.client.get_messages(self.message.chat_id, ids=[self.message.id])
            if fresh_msgs:
                self.message = fresh_msgs[0]
                return True
        except Exception as e:
            logger.error(f"Failed to refresh reference: {e}")
        return False

    async def _worker(self):
        offset = 0
        retries = 0
        
        while offset < self.file_size:
            try:
                # Get the correct media location
                location = self.message.media.document if hasattr(self.message.media, 'document') else self.message.media.photo
                
                # Download chunk
                chunk = await self.client.download_file(
                    location,
                    offset=offset,
                    limit=self.chunk_size
                )
                
                if not chunk:
                    # If empty chunk received unexpectedly, retry
                    if retries < 5:
                        retries += 1
                        await asyncio.sleep(1)
                        continue
                    else:
                        break

                await self.queue.put(chunk)
                offset += len(chunk)
                retries = 0 # Reset retries on success
                
            except (errors.FileReferenceExpiredError, errors.MediaEmptyError):
                logger.warning(f"Link expired for {self.name}, refreshing...")
                if await self._refresh_file_reference():
                    continue # Retry the same chunk with new reference
                else:
                    logger.error("Could not refresh file reference, aborting.")
                    break
            except Exception as e:
                logger.error(f"Worker Error: {e}")
                if retries < 5:
                    retries += 1
                    await asyncio.sleep(2)
                else:
                    break
                
        await self.queue.put(None) # End of Stream

    def __len__(self):
        return self.file_size

    async def read(self, size=-1):
        if size == -1: size = self.chunk_size
        
        while len(self.buffer) < size:
            chunk = await self.queue.get()
            
            if chunk is None: 
                await self.queue.put(None) 
                break
                
            self.buffer += chunk
            self.current_bytes += len(chunk)
            
            asyncio.create_task(progress_callback(self.current_bytes, self.file_size, self.start_time, self.name))

        data = self.buffer[:size]
        self.buffer = self.buffer[size:]
        return data

# --- ORIGINAL ATTRIBUTE PRESERVER ---
def get_file_attributes(message):
    attributes = []
    
    # Try to get original filename
    file_name = None
    if message.file and message.file.name:
        file_name = message.file.name
    
    # If no name, ensure we set one based on MIME
    if not file_name:
        mime = message.file.mime_type
        ext = mimetypes.guess_extension(mime) or ""
        # Force extension for known types
        if "video" in mime: ext = ".mp4"
        elif "image" in mime: ext = ".jpg"
        elif "pdf" in mime: ext = ".pdf"
        file_name = f"File_{message.id}{ext}"
    
    # Always add filename attribute first
    attributes.append(DocumentAttributeFilename(file_name=file_name))
    
    # Copy ORIGINAL attributes (Resolution, Duration, etc.)
    if message.media and hasattr(message.media, 'document'):
        for attr in message.media.document.attributes:
            # Filename humne already add kar diya, baaki sab copy karo
            if not isinstance(attr, DocumentAttributeFilename):
                attributes.append(attr)
                
    return attributes, file_name

# --- LINK PARSER ---
def extract_id_from_link(link):
    regex = r"(\d+)$"
    match = re.search(regex, link)
    if match: return int(match.group(1))
    return None

# --- TRANSFER PROCESS ---
async def transfer_process(event, source_id, dest_id, start_msg, end_msg):
    global is_running, status_message
    
    status_message = await event.respond(f"🔥 **Original Quality Engine!**\nSource: `{source_id}`")
    total_processed = 0
    
    try:
        async for message in user_client.iter_messages(source_id, min_id=start_msg-1, max_id=end_msg+1, reverse=True):
            if not is_running:
                await status_message.edit("🛑 **Stopped!**")
                break

            if getattr(message, 'action', None): continue

            try:
                # Refresh message before starting to ensure valid link
                try:
                    fresh_msg_list = await user_client.get_messages(source_id, ids=[message.id])
                    if fresh_msg_list:
                        message = fresh_msg_list[0]
                except: pass

                # Get Correct Info
                attributes, file_name = get_file_attributes(message)
                mime_type = message.file.mime_type if message.file else "application/octet-stream"
                
                await status_message.edit(f"🔍 **Processing:** `{file_name}`")

                if not message.media:
                    await bot_client.send_message(dest_id, message.text)
                else:
                    sent = False
                    start_time = time.time()
                    
                    # 1. DIRECT COPY (Best for Speed & Originality)
                    try:
                        await bot_client.send_file(dest_id, message.media, caption=message.text or "")
                        sent = True
                        await status_message.edit(f"✅ **Fast Copied:** `{file_name}`")
                    except Exception:
                        pass 

                    # 2. ROBUST STREAM (If Direct Fails)
                    if not sent:
                        # Thumbnail
                        thumb = None
                        try: thumb = await user_client.download_media(message, thumb=-1)
                        except: pass
                        
                        # Streamer with Auto-Refresh
                        stream_file = RobustHighSpeedStream(
                            user_client, 
                            message, # Pass full message for refreshing
                            message.file.size,
                            file_name,
                            start_time
                        )
                        
                        # Force Video/Image to NOT be Document (unless user sends as file)
                        force_doc = False
                        if "video" in mime_type or "image" in mime_type:
                            force_doc = False
                        else:
                            force_doc = True
                        
                        await bot_client.send_file(
                            dest_id,
                            file=stream_file,
                            caption=message.text or "",
                            attributes=attributes,
                            thumb=thumb,
                            supports_streaming=True,
                            file_size=message.file.size,
                            force_document=force_doc,
                            mime_type=mime_type
                        )
                        
                        if thumb and os.path.exists(thumb): os.remove(thumb)
                        await status_message.edit(f"✅ **Sent:** `{file_name}`")

                total_processed += 1
                
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
            except Exception as e:
                logger.error(f"Failed {message.id}: {e}")
                try: await bot_client.send_message(event.chat_id, f"❌ **Failed:** `{file_name}`\nReason: `{str(e)[:50]}`")
                except: pass
                continue

        if is_running:
            await status_message.edit(f"✅ **All Files Transferred!**\nTotal: `{total_processed}`")

    except Exception as e:
        await status_message.edit(f"❌ **Error:** {e}")
    finally:
        is_running = False

# --- COMMANDS ---
@bot_client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    await event.respond("🟢 **Original Quality Bot!**\n`/clone Source Dest`")

@bot_client.on(events.NewMessage(pattern='/clone'))
async def clone_init(event):
    global is_running
    if is_running: return await event.respond("⚠️ Busy...")
    try:
        args = event.text.split()
        pending_requests[event.chat_id] = {'source': int(args[1]), 'dest': int(args[2])}
        await event.respond("✅ **Set!** Send Range Link.")
    except: await event.respond("❌ Usage: `/clone -100xxx -100yyy`")

@bot_client.on(events.NewMessage())
async def range_listener(event):
    global current_task, is_running
    if event.chat_id not in pending_requests or "t.me" not in event.text: return
    try:
        links = event.text.strip().split("-")
        msg1, msg2 = extract_id_from_link(links[0]), extract_id_from_link(links[1])
        if msg1 > msg2: msg1, msg2 = msg2, msg1
        
        data = pending_requests.pop(event.chat_id)
        is_running = True
        current_task = asyncio.create_task(transfer_process(event, data['source'], data['dest'], msg1, msg2))
    except Exception as e: await event.respond(f"❌ Error: {e}")

@bot_client.on(events.NewMessage(pattern='/stop'))
async def stop_handler(event):
    global is_running
    is_running = False
    if current_task: current_task.cancel()
    await event.respond("🛑 **Stopped!**")

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    user_client.start()
    loop.create_task(start_web_server())
    bot_client.start(bot_token=BOT_TOKEN)
    bot_client.run_until_disconnected()


              
