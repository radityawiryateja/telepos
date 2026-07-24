import os
import re
import json
import uuid
import logging
import html
import asyncio
import datetime
import builtins
import string
import random
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, MessageReactionHandler, filters, ContextTypes, ConversationHandler
)
from supabase import create_client, Client
from pyrogram import Client as PyClient

WAIT_CONTENT, WAIT_BUTTONS, WAIT_BUTTON_LAYOUT = range(3)
# ==========================================
# KONFIGURASI DAN SETUP
# ==========================================
load_dotenv()

print("DEBUG BOT_TOKEN:", os.environ.get('BOT_TOKEN')[:10] if os.environ.get('BOT_TOKEN') else "KOSONG")
print("DEBUG API_ID:", os.environ.get('API_ID'))
print("DEBUG STRING_SESSION:", os.environ.get('STRING_SESSION')[:10] if os.environ.get('STRING_SESSION') else "KOSONG")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get('BOT_TOKEN')
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL dan SUPABASE_KEY tidak boleh kosong!")

ADMIN_GROUP_ID = int(os.environ.get('ADMIN_GROUP_ID', '0'))
LOG_GROUP_ID = int(os.environ.get('LOG_GROUP_ID', '0'))
CHANNEL_ID = os.environ.get('CHANNEL_ID', '@decavstore')
DISCUSSION_GROUP_ID = int(os.environ.get('DISCUSSION_GROUP_ID', '0'))

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

API_ID = os.environ.get('API_ID')
API_HASH = os.environ.get('API_HASH')
STRING_SESSION = os.environ.get('STRING_SESSION')

userbot = None
if all([API_ID, API_HASH, STRING_SESSION]):
    try:
        userbot = PyClient(
            "decav_userbot",
            api_id=int(API_ID) if str(API_ID).isdigit() else 0,
            api_hash=API_HASH,
            session_string=STRING_SESSION,
            in_memory=True
        )
    except Exception as e:
        logger.error(f"Gagal menginisialisasi client Pyrogram: {e}")

# State & Caches
_broadcast_running = False
BROADCAST_DELETE_CACHE = {}
ADMIN_MEDIA_GROUP_CACHE = {}
USER_MEDIA_GROUP_CACHE = set()
MESSAGE_USER_MAP = {}  
ALBUM_CACHE = {}
REMINDER_CACHE = {}
ALBUM_LOCKS = set()    
ADMIN_ALBUM_CACHE = {} 
ADMIN_BUYER_MSG_MAP = {}
ADMIN_USERNAME_CACHE = {}
USER_STATE_CACHE = {}
CACHE_NOTIF_MAP = {}
ADMIN_ALBUM_LOCKS = set() 
REGISTERED_USERS_CACHE = set()
ADMIN_TAGS_MAP = {
    "teleprem": [5760818847, 5029556300, 8303439452, 5957277504],
    "stars": [8480791253, 8392429387, 5760818847],
    "custom": [8392429387, 6727777532, 5760818847],
    "manips": [6727777532, 5957277504, 5760818847],
    "wording": [5029556300, 8274597438],
    "tarot": [5029556300, 8274597438],
}

async def db(func):
    return await asyncio.to_thread(func)

# ==========================================
# BACKGROUND TASKS
# ==========================================
async def bg_register_user(user_id: int):
    """Mendaftarkan user ke DB di background secara cerdas"""
    # Jika user sudah ada di memori sementara, lewati (hemat kuota database)
    if user_id in REGISTERED_USERS_CACHE: 
        return
        
    try:
        # Daftar/Update di tabel users
        await db(lambda: supabase.table("users").upsert({"user_id": user_id}).execute())
        
        # Cek apakah tabel loyalti sudah ada
        res = await db(lambda: supabase.table("loyalty_stats").select("user_id").eq("user_id", user_id).execute())
        if not res.data: 
            await db(lambda: supabase.table("loyalty_stats").insert({"user_id": user_id, "teleprem_spent": 0, "stars_spent": 0, "profneeds_spent": 0}).execute())
            
        # Masukkan ke cache memori agar pesan berikutnya tidak nge-hit database lagi
        REGISTERED_USERS_CACHE.add(user_id)
    except Exception as e:
        logger.error(f"Gagal background register {user_id}: {e}")
        
async def auto_clear_cache(context: ContextTypes.DEFAULT_TYPE):
    """Membersihkan cache otomatis setiap 5 menit (300 detik)"""
    if REGISTERED_USERS_CACHE:
        REGISTERED_USERS_CACHE.clear()
        
    if ADMIN_USERNAME_CACHE:
        ADMIN_USERNAME_CACHE.clear() # <- TAMBAHIN BARIS INI
        
    logger.info("♻️ Cache user & username admin otomatis dibersihkan.")

# ==========================================
# UTILITIES & RESOLVERS (PYROGRAM USERBOT)
# ==========================================
async def resolve_username(username: str) -> int:
    global userbot
    if not userbot: return None
        
    if not userbot.is_connected:
        original_input = builtins.input
        builtins.input = fake_input 
        try: await userbot.start()
        except Exception as e:
            logger.error(f"Userbot gagal sambung ulang: {e}")
            return None
        finally: builtins.input = original_input

    try:
        user = await userbot.get_users(username)
        return user.id
    except Exception as e:
        logger.error(f"Userbot gagal melacak @{username}: {e}")
        return None

async def get_target_id(reply_to_msg):
    if not reply_to_msg: return None
    
    if reply_to_msg.message_id in MESSAGE_USER_MAP:
        val = MESSAGE_USER_MAP[reply_to_msg.message_id]
        return val["user_id"] if isinstance(val, dict) else val
        
    entities = reply_to_msg.entities or reply_to_msg.caption_entities or []
    for ent in entities:
        if ent.type == 'text_mention' and ent.user: return ent.user.id
        if ent.type == 'text_link' and ent.url and ent.url.startswith("tg://user?id="):
            try: return int(ent.url.split("=")[1])
            except: pass
         
    text = reply_to_msg.text or reply_to_msg.caption or ""
    match_id = re.search(r'(?:#ID|User ID:)\s*(\d+)', text, re.IGNORECASE)
    if match_id: return int(match_id.group(1))
        
    match_username = re.search(r'@([a-zA-Z0-9_]+)', text)
    if match_username:
        username = match_username.group(1)
        return await resolve_username(username) 
        
    return None

async def send_admin_log(context: ContextTypes.DEFAULT_TYPE, action: str, admin_user, details: str):
    if LOG_GROUP_ID == 0: return
    admin_name = admin_user.first_name if admin_user else "Sistem / Auto"
    admin_id = admin_user.id if admin_user else "N/A"
    text = f"🚨 <b>ACTIVITY LOG</b>\n👤 Oleh: {html.escape(admin_name)} (<code>{admin_id}</code>)\n🛠 Aksi: {action}\n📝 Detail: {details}"
    try: await context.bot.send_message(chat_id=LOG_GROUP_ID, text=text, parse_mode="HTML")
    except: pass

async def delete_after_delay(bot, chat_id, message_id, delay=5):
    await asyncio.sleep(delay)
    try: await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except: pass

# ==========================================
# FITUR FORCE SUBSCRIBE & HANDLERS
# ==========================================
async def check_forcesub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not CHANNEL_ID or CHANNEL_ID == '0': return True 
    user_id = update.effective_user.id
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        if member.status in ['left', 'kicked']:
            channel_link = f"https://t.me/{CHANNEL_ID.replace('@', '')}"
            await update.message.reply_text(
                "⚠️ <b>Akses Ditolak!</b>\n\nUntuk menggunakan bot ini, kamu wajib berlangganan channel kami terlebih dahulu.",
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 Join Channel Dulu", url=channel_link)]])
            )
            return False
        return True
    except: return True
    
def get_main_keyboard():
    """Fungsi untuk memanggil keyboard utama kapan saja"""
    keyboard = [
        [KeyboardButton("👤 Profile"), KeyboardButton("🎁 Referal")],
        [KeyboardButton("🗣 Tanya (Admin)")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)
    
async def cmd_start_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private': return
    if not await check_forcesub(update, context): return
   
    user = update.effective_user
    
    asyncio.create_task(send_admin_log(context, "User Mengakses Bot", user, f"Memulai interaksi dengan bot."))
    asyncio.create_task(bg_register_user(user.id))

    # --- PESAN PERTAMA (Inline Keyboard / Link) ---
    inline_keyboard = [
        [InlineKeyboardButton("🧭 Navigasi Menu", url="https://t.me/decavstore/685")], 
        [InlineKeyboardButton("💬 Testimoni", url="https://t.me/Decavt")], 
        [InlineKeyboardButton("📊 Result", url="https://t.me/decavi"), InlineKeyboardButton("⭐ Honest Review", url="https://t.me/HRdecav")]
    ]
    await update.message.reply_text(
        f"Halo <b>{html.escape(user.first_name)}</b>! Selamat datang di bot pemesanan <b>@DECAVSTORE</b> 🛒\n\n"
        f"Ada yang bisa kami bantu? Silakan langsung ketik pesan di sini ya!\n\n"
        f"/profile untuk melihat loyality card\n/referal untuk dapat diskon tambahan", 
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard)
    )

  
    await update.message.reply_text(
        "👇 <i>Atau gunakan tombol di bawah ini untuk menu cepat:</i>",
        parse_mode="HTML", 
        reply_markup=get_main_keyboard()
    )

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private': return
    if not await check_forcesub(update, context): return
    
    if update.message.reply_to_message:
        replied_text = (update.message.reply_to_message.text or update.message.reply_to_message.caption or "")
        match = re.search(r"#ID:(\d+)", replied_text)
        
        if match:
            comment_msg_id = int(match.group(1))
            try:
                if update.message.text:
                    await context.bot.send_message(
                        chat_id=DISCUSSION_GROUP_ID, # Menggunakan variabel .env baru kita
                        text=f"🗣️ <b>Balasan Sender:</b>\n\n{update.message.text_html}",
                        reply_to_message_id=comment_msg_id,
                        parse_mode="HTML"
                    )
                else:
                    await context.bot.copy_message(
                        chat_id=DISCUSSION_GROUP_ID,
                        from_chat_id=user.id,
                        message_id=update.message.message_id,
                        reply_to_message_id=comment_msg_id,
                        caption=f"🗣️ <b>Balasan Sender:</b>\n\n{update.message.caption_html or ''}",
                        parse_mode="HTML"
                    )
                return await update.message.reply_text("✅ Balasan anonim berhasil dikirim ke pengomentar!")
            except Exception as e:
                logger.error(f"Gagal balasan anonim: {e}")
                return await update.message.reply_text("❌ Gagal mengirim balasan anonim, mungkin komentar aslinya sudah dihapus.")
   
    user = update.effective_user
    user_display = f"@{user.username}" if user.username else html.escape(user.first_name)

    text_content = (update.message.text or update.message.caption or "").lower()
    
    # --- TANGKAP TOMBOL KEYBOARD ---
    if text_content == "👤 profile":
        return await cmd_profile(update, context)
    
    if text_content == "🎁 referal":
        return await cmd_referal(update, context)
        
    if text_content == "🗣 tanya (admin)":
        USER_STATE_CACHE[user.id] = "WAITING_MENFESS"
        return await update.message.reply_text(
            "📝 <b>Silakan kirim pesan kamu!</b>\n\n"
            "Bisa berupa Teks, Foto, Video, atau Dokumen. Format asli kamu (bold, italic, spoiler) akan tetap dipertahankan kok!\n\n"
            "<i>Ketik 'Batal' jika tidak jadi mengirim.</i>",
            parse_mode="HTML"
        )
        
    if text_content == "batal" and USER_STATE_CACHE.get(user.id):
        del USER_STATE_CACHE[user.id]
        return await update.message.reply_text("✅ Pengiriman tanya dibatalkan.")
        reply_markup=get_main_keyboard()
    
    # --- LOGIKA PENGIRIMAN MENFESS ---
    # --- LOGIKA PENGIRIMAN MENFESS ---
    if USER_STATE_CACHE.get(user.id) == "WAITING_MENFESS":
        # Hapus state agar tidak terus-terusan mode tanya
        del USER_STATE_CACHE[user.id]

        status_msg = await update.message.reply_text("⏳ Sedang mengirim tanya kamu ke channel...")

        try:
            # Gunakan copy_message agar support ALL MEDIA & FORMAT ASLI dipertahankan!
            sent_msg = await context.bot.copy_message(
                chat_id=CHANNEL_ID,
                from_chat_id=user.id,
                message_id=update.message.message_id
            )

            # Simpan ke database untuk tracking notif komentar nanti
            await db(lambda: supabase.table("menfess_map").insert({
                "post_id": sent_msg.message_id,
                "sender_user_id": user.id
            }).execute())

            # Buat link menuju postingan
            post_url = f"https://t.me/{CHANNEL_ID.replace('@', '')}/{sent_msg.message_id}"
            
            # --- TAMBAHKAN LOG ADMIN DI SINI ---
            if LOG_GROUP_ID != 0:
                log_text = (
                    f"📌 <b>LOG TANYA (MENFESS) BARU</b>\n"
                    f"👤 Pengirim: {user_display}\n" # Ini akan menampilkan @username
                    f"🆔 ID: <code>{user.id}</code>\n"
                    f"🔗 <a href='{post_url}'>Lihat Postingan</a>"
                )
                try:
                    await context.bot.send_message(chat_id=LOG_GROUP_ID, text=log_text, parse_mode="HTML", disable_web_page_preview=True)
                except Exception as e:
                    logger.error(f"Gagal kirim log menfess: {e}")
            # -----------------------------------

            await status_msg.edit_text(
                f"✅ <b>Pesan kamu telah dikirim ke channel.</b> 🪶\n\n"
                f"Nanti kamu akan dapat notifikasi balasan!",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Lihat Pesan Kamu", url=post_url)]])
            )
            return

        except Exception as e:
            logger.error(f"Gagal posting menfess: {e}")
            await status_msg.edit_text("❌ Terjadi kesalahan saat mengirim tanya. Silakan coba lagi nanti.")
            return
    
    admin_ids_to_tag = set()
    if "teleprem" in text_content: 
        admin_ids_to_tag.update(ADMIN_TAGS_MAP["teleprem"])
    if "star" in text_content or "stars" in text_content: 
        admin_ids_to_tag.update(ADMIN_TAGS_MAP["stars"])
    if "custom" in text_content: 
        admin_ids_to_tag.update(ADMIN_TAGS_MAP["custom"])
    if "manip" in text_content or "manips" in text_content: 
        admin_ids_to_tag.update(ADMIN_TAGS_MAP["manips"])
    if "tarot" in text_content: 
        admin_ids_to_tag.update(ADMIN_TAGS_MAP["tarot"])
    if "wording" in text_content: 
        admin_ids_to_tag.update(ADMIN_TAGS_MAP["wording"])

    # 2. Convert User ID jadi Username / Text Mention (Pake Cache)
    tags = []
    for aid in admin_ids_to_tag:
        if aid not in ADMIN_USERNAME_CACHE:
            try:
                # Minta data terbaru si admin ke Telegram
                admin_chat = await context.bot.get_chat(aid)
                if admin_chat.username:
                    ADMIN_USERNAME_CACHE[aid] = f"@{admin_chat.username}"
                else:
                    # Kalo admin ga pake username, tag lewat first name
                    ADMIN_USERNAME_CACHE[aid] = f"<a href='tg://user?id={aid}'>{html.escape(admin_chat.first_name)}</a>"
            except Exception as e:
                # Kalo gagal fetch (misal bot ngelag), fallback ngetag kosongan
                logger.error(f"Gagal narik usn admin {aid}: {e}")
                ADMIN_USERNAME_CACHE[aid] = f"<a href='tg://user?id={aid}'>Admin</a>"
                
        tags.append(ADMIN_USERNAME_CACHE[aid])

    tag_str = f"\n🔔 {', '.join(tags)}" if tags else ""

    # Menggunakan <blockquote expandable> dengan format yang lebih simpel
    user_footer = (
        f"\n\n<blockquote expandable>"
        f"👤 {user_display} (<code>{user.id}</code>)"
        f"{tag_str}"
        f"</blockquote>"
    )

    asyncio.create_task(bg_register_user(user.id))
    if len(MESSAGE_USER_MAP) > 5000: MESSAGE_USER_MAP.clear()

    if update.message.media_group_id:
        mg_id = update.message.media_group_id
        if mg_id not in ALBUM_CACHE: ALBUM_CACHE[mg_id] = []
        ALBUM_CACHE[mg_id].append(update.message)
        
        if mg_id not in ALBUM_LOCKS:
            ALBUM_LOCKS.add(mg_id)
            notif = await update.message.reply_text("⏳ Sedang meneruskan album...")
            await asyncio.sleep(3)
          
            messages = ALBUM_CACHE[mg_id]
            media_group = []
            user_caption = "\n\n".join([msg.caption_html for msg in messages if msg.caption_html])
            
            combined_caption = f"{user_caption}{user_footer}" if user_caption else user_footer
          
            for idx, msg in enumerate(messages):
                cap, pmode = (combined_caption, "HTML") if idx == 0 else ("", None)
                if msg.photo: 
                    media_group.append(InputMediaPhoto(media=msg.photo[-1].file_id, caption=cap, parse_mode=pmode))
                elif msg.video: 
                    media_group.append(InputMediaVideo(media=msg.video.file_id, caption=cap, parse_mode=pmode))
                elif msg.document: 
                    media_group.append(InputMediaDocument(media=msg.document.file_id, caption=cap, parse_mode=pmode))
                elif msg.audio: 
                    media_group.append(InputMediaAudio(media=msg.audio.file_id, caption=cap, parse_mode=pmode))
          
            try:
                sent_messages = await context.bot.send_media_group(chat_id=ADMIN_GROUP_ID, media=media_group)
                for idx, s_msg in enumerate(sent_messages): 
                    MESSAGE_USER_MAP[s_msg.message_id] = {"user_id": user.id, "buyer_msg_id": messages[idx].message_id}
                await notif.edit_text("✅ Album kamu telah diteruskan ke Admin.")
                asyncio.create_task(delete_after_delay(context.bot, user.id, notif.message_id, 5))
            except Exception as e:
                logger.error(f"Error forward album: {e}") 
                await notif.edit_text("⚠️ Gagal mengirim album.")
            del ALBUM_CACHE[mg_id]; ALBUM_LOCKS.remove(mg_id)
    else:
        if update.message.text:
            sent_msg = await context.bot.send_message(chat_id=ADMIN_GROUP_ID, text=f"{update.message.text_html}{user_footer}", parse_mode="HTML", disable_web_page_preview=True)
            MESSAGE_USER_MAP[sent_msg.message_id] = {"user_id": user.id, "buyer_msg_id": update.message.message_id}
        else:
            combined_caption = f"{update.message.caption_html}{user_footer}" if update.message.caption_html else user_footer
            sent_msg = await context.bot.copy_message(chat_id=ADMIN_GROUP_ID, from_chat_id=user.id, message_id=update.message.message_id, caption=combined_caption, parse_mode="HTML")
            MESSAGE_USER_MAP[sent_msg.message_id] = {"user_id": user.id, "buyer_msg_id": update.message.message_id}
          
        notif = await update.message.reply_text("✅ Pesan kamu telah diteruskan ke Admin.")
        asyncio.create_task(delete_after_delay(context.bot, user.id, notif.message_id, 5))

async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in [ADMIN_GROUP_ID, LOG_GROUP_ID]: return
    
    if not update.message.reply_to_message or update.message.reply_to_message.from_user.id != context.bot.id: 
        return
        
    target_user_id = await get_target_id(update.message.reply_to_message) if update.message.reply_to_message else None
     
    if update.message.media_group_id:
        mg_id = update.message.media_group_id
        if target_user_id: ADMIN_MEDIA_GROUP_CACHE[mg_id] = target_user_id
        else: target_user_id = ADMIN_MEDIA_GROUP_CACHE.get(mg_id)
        if not target_user_id: return 
        
        if mg_id not in ADMIN_ALBUM_CACHE: ADMIN_ALBUM_CACHE[mg_id] = []
        ADMIN_ALBUM_CACHE[mg_id].append(update.message)
        
        if mg_id not in ADMIN_ALBUM_LOCKS:
            ADMIN_ALBUM_LOCKS.add(mg_id)
            await asyncio.sleep(10)
            messages = ADMIN_ALBUM_CACHE[mg_id]
            media_group = []
            for msg in messages:
                cap, pmode = (msg.caption_html, "HTML") if msg.caption_html else ("", None)
                if msg.photo: 
                    media_group.append(InputMediaPhoto(media=msg.photo[-1].file_id, caption=cap, parse_mode=pmode, show_caption_above_media=True))
                elif msg.video: 
                    media_group.append(InputMediaVideo(media=msg.video.file_id, caption=cap, parse_mode=pmode, show_caption_above_media=True))
                elif msg.document: 
                    media_group.append(InputMediaDocument(media=msg.document.file_id, caption=cap, parse_mode=pmode))
                elif msg.audio: 
                    media_group.append(InputMediaAudio(media=msg.audio.file_id, caption=cap, parse_mode=pmode))
          
            try:
                await context.bot.send_media_group(chat_id=target_user_id, media=media_group)
                notif = await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ Balasan album terkirim ke User ID {target_user_id}.")
                asyncio.create_task(delete_after_delay(context.bot, update.effective_chat.id, notif.message_id, 5))
            except Exception as e:
                logger.error(f"Error reply album: {e}")
                await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Gagal mengirim balasan album ke pembeli.")
            del ADMIN_ALBUM_CACHE[mg_id]; ADMIN_ALBUM_LOCKS.remove(mg_id)
    else:
        if not target_user_id:
            if update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id:
                await update.message.reply_text("⚠️ Gagal membalas! Gunakan <code>/fix @username</code> jika session chat terputus.", parse_mode="HTML")
            return

        try:
            copy_kwargs = {"chat_id": target_user_id, "from_chat_id": update.effective_chat.id, "message_id": update.message.message_id}
            if not update.message.text: copy_kwargs["show_caption_above_media"] = True
            
            # Tangkap hasil copy_message
            sent_msg = await context.bot.copy_message(**copy_kwargs)
            
            # Simpan relasi ID pesan admin dengan pesan buyer
            ADMIN_BUYER_MSG_MAP[update.message.message_id] = {
                "user_id": target_user_id,
                "buyer_msg_id": sent_msg.message_id
            }
            # Cegah memory leak
            if len(ADMIN_BUYER_MSG_MAP) > 5000: ADMIN_BUYER_MSG_MAP.clear()

            try:
                await update.message.set_reaction(reaction="👍")
            except:
                pass # Abaikan jika grup membatasi fitur reaction
        except Exception as e: 
            logger.error(f"Gagal reply: {e}")
            await update.message.reply_text("⚠️ Gagal mengirim balasan ke pembeli.")
            
async def handle_reaction_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reaction_update = update.message_reaction
    if not reaction_update: return
    
    # 1. Sinkronisasi Reaction Admin ke Buyer (Fitur Lamamu)
    if reaction_update.chat.id == ADMIN_GROUP_ID:
        admin_msg_id = reaction_update.message_id
        if admin_msg_id in MESSAGE_USER_MAP:
            mapping = MESSAGE_USER_MAP[admin_msg_id]
            if isinstance(mapping, dict):
                try:
                    await context.bot.set_message_reaction(
                        chat_id=mapping["user_id"],
                        message_id=mapping["buyer_msg_id"],
                        reaction=reaction_update.new_reaction
                    )
                except Exception as e:
                    logger.error(f"Gagal meneruskan reaction ke buyer: {e}")
        return

    # 2. Sinkronisasi Reaction Sender ke Komentar Grup Diskusi (Fitur Tanya Baru)
    if reaction_update.chat.type == 'private':
        notif_msg_id = reaction_update.message_id
        # Cek apakah reaction diberikan ke pesan notifikasi komentar
        if notif_msg_id in CACHE_NOTIF_MAP:
            comment_msg_id = CACHE_NOTIF_MAP[notif_msg_id]
            try:
                # Teruskan reaction ke pesan asli pengomentar di grup
                await context.bot.set_message_reaction(
                    chat_id=DISCUSSION_GROUP_ID,
                    message_id=comment_msg_id,
                    reaction=reaction_update.new_reaction
                )
            except Exception as e:
                logger.error(f"Gagal meneruskan reaction ke grup diskusi: {e}")

# ==========================================
# FITUR DISKUSI & NOTIFIKASI KOMENTAR
# ==========================================
async def handle_discussion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return

    # 1. Menangkap Postingan Otomatis dari Channel ke Grup Diskusi
    if msg.is_automatic_forward and msg.forward_origin and msg.forward_origin.type == "channel":
        post_id = msg.forward_origin.message_id
        
        # Cek apakah ini benar dari channel kita
        origin_chat = msg.forward_origin.chat
        if origin_chat.username and ("@" + origin_chat.username.lower() == CHANNEL_ID.lower()):
            try:
                # Update database dengan ID pesan yang ada di grup diskusi
                await db(lambda: supabase.table("menfess_map").update({
                    "discussion_message_id": msg.message_id
                }).eq("post_id", post_id).execute())
            except Exception as e:
                logger.error(f"Gagal update discussion_message_id: {e}")
        return

    # 2. Menangkap Balasan/Komentar Member
    if msg.reply_to_message and msg.from_user.id != context.bot.id:
        try:
            replied_msg = msg.reply_to_message
            
            # Menentukan ID Thread (Post Tanya aslinya di grup diskusi)
            discussion_msg_id = msg.message_thread_id or replied_msg.message_id
            
            is_valid_trigger = False
            
            # Jika membalas komen anonim "Balasan Sender:" dari bot
            if replied_msg.from_user.id == context.bot.id and "Balasan Sender:" in (replied_msg.text or replied_msg.caption or ""):
                is_valid_trigger = True
            # Jika membalas langsung ke postingan tanya utamanya
            elif discussion_msg_id == replied_msg.message_id:
                is_valid_trigger = True
                
            if is_valid_trigger:
                # Tarik data dari database untuk mencari siapa pengirim aslinya
                res = await db(lambda: supabase.table("menfess_map").select("sender_user_id, post_id").eq("discussion_message_id", discussion_msg_id).execute())
                
                if res.data:
                    sender_uid = res.data[0]["sender_user_id"]
                    post_id = res.data[0]["post_id"]
                    
                    # Dapatkan nama/username pengomentar
                    commenter = f"@{msg.from_user.username}" if msg.from_user.username else html.escape(msg.from_user.first_name)
                    
                    # Buat link menuju komentar
                    link = f"https://t.me/{CHANNEL_ID.replace('@', '')}/{post_id}?comment={msg.message_id}"
                    
                    notif_text = (
                        f"📬 <b>{commenter}</b> berkomentar di pertanyaan kamu!\n\n"
                        f"<i>(Balas/reply pesan ini jika kamu ingin membalas komentarnya secara anonim)</i>\n\n"
                        f"<code>#ID:{msg.message_id}</code>"
                    )
                    
                    # Kirim notifikasi ke DM sender
                    notif_msg = await context.bot.send_message(
                        chat_id=sender_uid,
                        text=notif_text,
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Lihat Balasan", url=link)]])
                    )
                    
                    # Simpan ID notif ke memori untuk fitur sinkronisasi Reaction (React)
                    CACHE_NOTIF_MAP[notif_msg.message_id] = msg.message_id

        except Exception as e:
            logger.error(f"Gagal mengirim notif komentar: {e}")
            
# ==========================================
# CUSTOM COMMANDS & SISANYA (TIDAK ADA PERUBAHAN)
# ==========================================
async def cmd_setpayment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_ID: return
    args = update.message.text.split(maxsplit=2)
    if len(args) < 3: return await update.message.reply_text("Format: /setpayment namacommand wording")
    await db(lambda: supabase.table("dynamic_commands").upsert({"command_name": args[1].lower().replace('/', ''), "kategori": "global", "tipe": "pay", "wording": args[2]}).execute())
    await update.message.reply_text(f"✅ Command `{args[1]}` (Payment) disimpan.", parse_mode="Markdown")

async def cmd_setafterpay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_ID: return
    args = update.message.text.split(maxsplit=2)
    if len(args) < 3: return await update.message.reply_text("Format: /setafterpay namacommand wording")
    await db(lambda: supabase.table("dynamic_commands").upsert({"command_name": args[1].lower().replace('/', ''), "kategori": "global", "tipe": "afterpay", "wording": args[2]}).execute())
    await update.message.reply_text(f"✅ Command `{args[1]}` (Afterpay) disimpan.", parse_mode="Markdown")

async def cmd_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in [ADMIN_GROUP_ID, LOG_GROUP_ID]: return
    
    # Update panduan format peringatan
    if not context.args: 
        return await update.message.reply_text("⚠️ <b>Format Salah!</b>\nCara pakai: <code>/fix @username</code> atau <code>/fix ID_USER</code>", parse_mode="HTML")

    input_data = context.args[0].strip()

    # Cek apakah input murni angka (berarti itu User ID)
    if input_data.isdigit():
        user_id = int(input_data)
        display_name = f"ID {user_id}"
        status_msg = await update.message.reply_text(f"⏳ Menyiapkan sesi balasan untuk {display_name}...")
    else:
        # Jika bukan angka, berarti username
        username = input_data.replace('@', '')
        display_name = f"@{username}"
        status_msg = await update.message.reply_text(f"⏳ Menghubungi Userbot untuk melacak {display_name}...")
        user_id = await resolve_username(username)
    
    # Eksekusi pesan akhir jika user_id didapatkan
    if user_id:
        text = (
            f"✅ <b>Sesi Berhasil Dipulihkan!</b>\n\n"
            f"👤 Target: {display_name}\n"
            f"🆔 User ID: <code>{user_id}</code>\n\n"
            f"<i>Silakan <b>REPLY</b> pesan ini langsung untuk membalas ke pembeli.</i><a href='tg://user?id={user_id}'>&#8203;</a>"
        )
        await status_msg.edit_text(text, parse_mode="HTML")
    else: 
        await status_msg.edit_text(f"❌ <b>Gagal melacak!</b> Pastikan {display_name} benar dan Userbot aktif.", parse_mode="HTML")

async def cmd_grupid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(f"🆔 <b>Informasi Chat</b>\n\n📌 Tipe: <code>{chat.type}</code>\n🏷️ Nama: {chat.title or chat.first_name}\n🔢 ID: <code>{chat.id}</code>", parse_mode="HTML")

async def handle_all_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    if update.effective_chat.id != ADMIN_GROUP_ID and not await check_forcesub(update, context): return
 
    text_pesan = update.message.text or update.message.caption
    if not text_pesan: return
    cmd_name, args = text_pesan.split()[0].lower().replace('/', ''), text_pesan.split()[1:]

    res = await db(lambda: supabase.table("dynamic_commands").select("*").eq("command_name", cmd_name).execute())
    if not res.data: return
        
    cmd_data = res.data[0]
    tipe, safe_wording = cmd_data['tipe'], html.escape(cmd_data['wording'])

    if tipe == 'pay':
        if update.effective_chat.id == ADMIN_GROUP_ID:
            if not update.message.reply_to_message: return await update.message.reply_text("⚠️ Reply pesan dari buyer untuk ngirim tagihan.")
            target_user_id = await get_target_id(update.message.reply_to_message)
            if not target_user_id: return await update.message.reply_text("⚠️ Gagal melacak ID! Bot kehilangan jejak pesan ini.")
        else: target_user_id = update.effective_user.id

        if not args or not args[0].replace('.', '').isdigit(): return await update.message.reply_text(f"⚠️ Masukkan jumlah. Contoh: /{cmd_name} 15000")
        jumlah = int(args[0].replace('.', ''))
        
        # 1. TANGKAP ID PESAN TAGIHAN KE BUYER
        buyer_bill_msg = await context.bot.send_message(chat_id=target_user_id, text=f"{safe_wording}\n\n<b>Jumlah Tagihan:</b> Rp{jumlah:,}", parse_mode="HTML")
        
        if update.effective_chat.id == ADMIN_GROUP_ID:
            admin_id = update.effective_user.id
            keyboard = [[InlineKeyboardButton(kat.upper(), callback_data=f"addcat_{target_user_id}_{jumlah}_{kat}_{admin_id}")] for kat in ["teleprem", "stars", "profneeds"]]
            keyboard.append([InlineKeyboardButton("❌ CANCEL", callback_data=f"cancelpay_{target_user_id}_{jumlah}_{admin_id}")])
            
            # 2. TANGKAP ID PESAN MENU ADMIN
            admin_menu_msg = await update.message.reply_text(f"✅ Tagihan Rp{jumlah:,} terkirim.\n👇 <b>Pilih kategori (PENDING):</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
            
            # 3. SIMPAN KEDUANYA DI MEMORI
            ADMIN_BUYER_MSG_MAP[admin_menu_msg.message_id] = {
                "user_id": target_user_id,
                "buyer_msg_id": buyer_bill_msg.message_id
            }

    elif tipe == 'afterpay':
        if update.effective_chat.id != ADMIN_GROUP_ID: return
        if not update.message.reply_to_message: return await update.message.reply_text("⚠️ Reply pesan bukti TF user.")
        target_user_id = await get_target_id(update.message.reply_to_message)
        if not target_user_id: return await update.message.reply_text("⚠️ Gagal melacak ID!")
         
        res_orders = await db(lambda: supabase.table("orders").select("*").eq("user_id", target_user_id).eq("status", "pending").execute())
        if res_orders.data:
            await db(lambda: supabase.table("orders").update({"status": "success"}).eq("user_id", target_user_id).eq("status", "pending").execute())
            total_semua, kategori_totals = 0, {}
            for order in res_orders.data:
                total_semua += order['jumlah']; kategori_totals[order['kategori']] = kategori_totals.get(order['kategori'], 0) + order['jumlah']
         
            await context.bot.send_message(chat_id=target_user_id, text=safe_wording, parse_mode="HTML")
            await update.message.reply_text(f"✅ Orderan selesai! Total Rp{total_semua:,} tercatat.")
         
            for kat, total_kategori in kategori_totals.items():
                col_name = f"{kat}_spent"
                res_loyalty = await db(lambda: supabase.table("loyalty_stats").select(col_name).eq("user_id", target_user_id).execute())
                current_spent = res_loyalty.data[0][col_name] if res_loyalty.data else 0
                new_total = current_spent + total_kategori
                await db(lambda: supabase.table("loyalty_stats").upsert({"user_id": target_user_id, col_name: new_total}).execute())
                if current_spent < 100000 and new_total >= 100000:
                    await context.bot.send_message(chat_id=target_user_id, text=f"🎉 <b>SELAMAT!</b> Belanja <code>{kat}</code> kamu mencapai Rp{new_total:,}.\nKamu berhak mendapat hadiah loyalitas!", parse_mode="HTML")
        else: await update.message.reply_text("⚠️ Tidak ada orderan pending untuk user ini.", parse_mode="HTML")
            
async def handle_edited_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Abaikan jika bukan pesan yang diedit atau bukan dari grup admin
    if not update.edited_message: return
    if update.edited_message.chat.id not in [ADMIN_GROUP_ID, LOG_GROUP_ID]: return
    
    admin_msg_id = update.edited_message.message_id
    
    # Cek apakah pesan yang diedit ada di dalam sistem pelacak kita
    if admin_msg_id in ADMIN_BUYER_MSG_MAP:
        mapping = ADMIN_BUYER_MSG_MAP[admin_msg_id]
        user_id = mapping["user_id"]
        buyer_msg_id = mapping["buyer_msg_id"]

        try:
            if update.edited_message.text:
                # Jika pesan berupa Teks
                await context.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=buyer_msg_id,
                    text=update.edited_message.text_html,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
            else:
                # Jika pesan berupa Media (Foto/Video/Dokumen) dengan Caption
                caption_text = update.edited_message.caption_html if update.edited_message.caption else ""
                await context.bot.edit_message_caption(
                    chat_id=user_id,
                    message_id=buyer_msg_id,
                    caption=caption_text,
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Gagal edit pesan buyer (mungkin buyer sudah menghapus pesannya): {e}")

async def handle_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.callback_query.data.split('_')
    target_user_id = int(parts[1])
    jumlah = int(parts[2])
    kategori = parts[3]
    admin_id = int(parts[4])
    
    await db(lambda: supabase.table("orders").insert({
        "user_id": target_user_id, 
        "kategori": kategori, 
        "jumlah": jumlah, 
        "status": "pending",
        "admin_id": admin_id
    }).execute())
    
    await update.callback_query.edit_message_text(
        f"✅ <b>TRANSAKSI PENDING TERCATAT!</b>\n"
        f"👤 User ID: <code>{target_user_id}</code>\n"
        f"💰 Tagihan: Rp{jumlah:,}\n"
        f"🏷 Kategori: <b>{kategori.upper()}</b>\n"
        f"👮 Diproses oleh Admin ID: <code>{admin_id}</code>", 
        parse_mode="HTML"
    )

# ==========================================
# BROADCAST & UTILITIES
# ==========================================
async def get_all_user_ids():
    all_ids = []
    page_size = 1000
    offset = 0
    while True:
        res = await db(lambda o=offset: supabase.table("users").select("user_id").range(o, o + page_size - 1).execute())
        if not res.data: break
        all_ids.extend(row["user_id"] for row in res.data)
        if len(res.data) < page_size: break
        offset += page_size
    return all_ids

async def safe_forward(context, chat_id, from_chat_id, message_id):
    try: return await context.bot.forward_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id)
    except: return await context.bot.copy_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id)

async def process_broadcast_failures(context: ContextTypes.DEFAULT_TYPE, chat_id: int, failed_ids: list, broadcast_name: str):
    if not failed_ids: return
    failed_data = {}
    for i in range(0, len(failed_ids), 200):
        batch = failed_ids[i:i+200]
        try:
            res = await db(lambda b=batch: supabase.table("loyalty_stats").select("*").in_("user_id", b).execute())
            for row in (res.data or []):
                failed_data[row["user_id"]] = row.get("teleprem_spent", 0) + row.get("stars_spent", 0) + row.get("profneeds_spent", 0)
        except: pass

    to_delete = [uid for uid in failed_ids if failed_data.get(uid, 0) == 0]
    file_content = "\n".join([f"ID {uid} | Total History Belanja Rp{failed_data.get(uid, 0)}" for uid in failed_ids]).encode('utf-8')
    caption = f"📄 Terdapat {len(failed_ids)} user yang gagal menerima {broadcast_name}."

    reply_markup = None
    if to_delete:
        task_id = str(uuid.uuid4())[:8]
        BROADCAST_DELETE_CACHE[task_id] = to_delete
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"🛠️ Hapus {len(to_delete)} User Pasif", callback_data=f"delbc_{task_id}")]])
        caption += f"\n\n🚨 Ada {len(to_delete)} user tidak aktif yang belum pernah belanja."

    await context.bot.send_document(chat_id=chat_id, document=file_content, filename=f"failed_{broadcast_name}.txt", caption=caption, reply_markup=reply_markup)

async def handle_broadcast_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    to_delete = BROADCAST_DELETE_CACHE.pop(query.data.split('_')[1], None)
    if not to_delete: return await query.edit_message_caption(f"{query.message.caption}\n\n❌ Data kadaluarsa.")

    await query.edit_message_caption(f"{query.message.caption}\n\n✅ SQL berhasil di-generate!")
    sql_query = f"-- Jalankan di SQL Editor Supabase\nDELETE FROM users WHERE user_id IN ({', '.join(str(uid) for uid in to_delete)});"
    if len(sql_query) > 3500: await context.bot.send_document(chat_id=query.message.chat_id, document=sql_query.encode('utf-8'), filename="delete_users.sql")
    else: await context.bot.send_message(chat_id=query.message.chat_id, text=f"Copy & Run di Supabase:\n\n<code>{sql_query}</code>", parse_mode="HTML")

async def broadcast_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _broadcast_running
    if update.effective_chat.id != ADMIN_GROUP_ID or not context.args: return await update.message.reply_text("Format: /broadcastfw link_pesan_telegram")
    if _broadcast_running: return await update.message.reply_text("⚠️ Broadcast sedang berjalan.")

    match = re.search(r"t\.me/([a-zA-Z0-9_]+)/(\d+)", context.args[0])
    if not match: return await update.message.reply_text("❌ Link tidak valid!")
    channel_username, message_id = match.groups()

    user_list = await get_all_user_ids()
    if not user_list: return await update.message.reply_text("⚠️ Tidak ada user di database.")

    _broadcast_running = True
    sc, fc, failed_users = 0, 0, []
    status_msg = await update.message.reply_text(f"⏳ Memulai broadcast forward ke {len(user_list)} user...")

    try:
        for i in range(0, len(user_list), 10):
            batch = user_list[i : i + 10]
            results = await asyncio.gather(*[safe_forward(context, uid, f"@{channel_username}", int(message_id)) for uid in batch], return_exceptions=True)
            for idx, res in enumerate(results):
                if isinstance(res, Exception): fc += 1; failed_users.append(batch[idx])
                else: sc += 1
            if (i + 10) % 40 == 0:
                try: await status_msg.edit_text(f"⏳ Proses... ({min(i + 10, len(user_list))}/{len(user_list)})\n✅ {sc} | ❌ {fc}")
                except: pass
            await asyncio.sleep(2.0)
    finally: _broadcast_running = False

    await status_msg.edit_text(f"✅ Broadcast FW Selesai!\n👥 Target: {len(user_list)}\n✅ Berhasil: {sc}\n❌ Gagal: {fc}")
    await process_broadcast_failures(context, update.effective_chat.id, failed_users, "broadcast_forward")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _broadcast_running
    if update.effective_chat.id != ADMIN_GROUP_ID: return
    
    # Memastikan admin me-reply sebuah pesan
    if not update.message.reply_to_message: 
        return await update.message.reply_text("⚠️ <b>Format Salah!</b>\nHarap balas/reply pesan yang ingin di-broadcast dengan perintah <code>/bc</code>", parse_mode="HTML")
        
    if _broadcast_running: return await update.message.reply_text("⚠️ Broadcast sedang berjalan.")

    user_list = await get_all_user_ids()
    if not user_list: return await update.message.reply_text("⚠️ Tidak ada user di database.")

    _broadcast_running = True
    sc, fc, failed_users = 0, 0, []
    status_msg = await update.message.reply_text(f"⏳ Memulai broadcast ke {len(user_list)} user...")

    try:
        for i in range(0, len(user_list), 10):
            batch = user_list[i : i + 10]
            tasks = []
            
            for uid in batch:
                # Kloning/Copy pesan yang direply dan tambahkan keyboard utama di setiap pesan
                tasks.append(context.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=update.effective_chat.id,
                    message_id=update.message.reply_to_message.message_id,
                    reply_markup=get_main_keyboard()
                ))
                    
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for idx, res in enumerate(results):
                if isinstance(res, Exception): fc += 1; failed_users.append(batch[idx])
                else: sc += 1
                
            if (i + 10) % 40 == 0:
                try: await status_msg.edit_text(f"⏳ Proses... ({min(i + 10, len(user_list))}/{len(user_list)})\n✅ {sc} | ❌ {fc}")
                except: pass
            await asyncio.sleep(2.0)
            
    finally: _broadcast_running = False

    await status_msg.edit_text(f"✅ Broadcast Selesai!\n👥 Target: {len(user_list)}\n✅ Berhasil: {sc}\n❌ Gagal: {fc}")
    await process_broadcast_failures(context, update.effective_chat.id, failed_users, "broadcast")
    
async def cmd_addloyalty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_ID: return
    
    target_user_id = await get_target_id(update.message.reply_to_message) if update.message.reply_to_message else None
    
    args = context.args
    if not target_user_id:
        if len(args) >= 3 and args[0].isdigit():
            target_user_id = int(args[0])
            args = args[1:]
        else:
            return await update.message.reply_text("⚠️ <b>Format Salah!</b>\nReply pesan buyer, atau gunakan:\n<code>/addloyalty [USER_ID] [kategori] [jumlah]</code>", parse_mode="HTML")

    if len(args) < 2:
        return await update.message.reply_text("⚠️ <b>Format:</b> <code>/addloyalty [kategori] [jumlah]</code>\nPilihan Kategori: teleprem / stars / profneeds", parse_mode="HTML")

    kategori = args[0].lower()
    if kategori not in ['teleprem', 'stars', 'profneeds']:
        return await update.message.reply_text("⚠️ Kategori tidak valid! Pilih: teleprem / stars / profneeds")

    try:
        jumlah = int(args[1].replace('.', ''))
    except ValueError:
        return await update.message.reply_text("⚠️ Jumlah harus berupa angka (tanpa huruf)!")

    col_name = f"{kategori}_spent"
    
    res = await db(lambda: supabase.table("loyalty_stats").select(col_name).eq("user_id", target_user_id).execute())
    current_spent = res.data[0][col_name] if res.data else 0
    new_total = current_spent + jumlah

    await db(lambda: supabase.table("loyalty_stats").upsert({"user_id": target_user_id, col_name: new_total}).execute())

    await update.message.reply_text(
        f"✅ <b>Loyalti Manual Berhasil Diinput!</b>\n\n"
        f"👤 User ID: <code>{target_user_id}</code>\n"
        f"🏷 Kategori: <b>{kategori.upper()}</b>\n"
        f"💰 Ditambahkan: Rp{jumlah:,}\n"
        f"📊 Total Sekarang: Rp{new_total:,}",
        parse_mode="HTML"
    )

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"🎉 <b>Yey! Ada tambahan riwayat belanjamu!</b>\n\nAdmin baru saja menginput pesanan terdahulumu sebesar <b>Rp{jumlah:,}</b> untuk kategori <b>{kategori.upper()}</b>.\n\nTotal belanjamu di kategori ini sekarang: <b>Rp{new_total:,}</b>\n\n<i>Ketik /profile untuk melihat total keseluruhan statusmu!</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text("⚠️ <i>Catatan: Gagal mengirim notifikasi ke buyer (mungkin bot diblokir oleh user).</i>", parse_mode="HTML")

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin_group = update.effective_chat.id == ADMIN_GROUP_ID
    
    if is_admin_group:
        target_user_id = await get_target_id(update.message.reply_to_message) if update.message.reply_to_message else None
        if not target_user_id:
            return await update.message.reply_text("⚠️ Reply pesan user untuk melihat status loyaltinya.")
    else:
        target_user_id = update.effective_user.id
        if not await check_forcesub(update, context): return

    # Tarik data loyalty
    res_stats = await db(lambda: supabase.table("loyalty_stats").select("*").eq("user_id", target_user_id).execute())
    stats = res_stats.data[0] if res_stats.data else {"teleprem_spent": 0, "stars_spent": 0, "profneeds_spent": 0}
    
    teleprem = stats.get("teleprem_spent", 0)
    stars = stats.get("stars_spent", 0)
    profneeds = stats.get("profneeds_spent", 0)
    total_all = teleprem + stars + profneeds

    # Tarik data voucher yang HANYA berstatus 'active'
    res_vouchers = await db(lambda: supabase.table("vouchers").select("kode, diskon").eq("user_id", target_user_id).eq("status", "active").execute())
    active_vouchers = res_vouchers.data or []

    header = f"USER <code>{target_user_id}</code>" if is_admin_group else "KAMU"
    
    teks = (
        f"👤 <b>PROFIL LOYALTI {header}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💎 <b>Telegram Premium:</b> Rp{teleprem:,}\n"
        f"⭐ <b>Telegram Stars:</b> Rp{stars:,}\n"
        f"🛒 <b>Kebutuhan Profil:</b> Rp{profneeds:,}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🏆 <b>TOTAL KESELURUHAN: Rp{total_all:,}</b>\n\n"
        f"Jika pernah order tapi belum ada loyalty card bisa isi ini dulu t.me/decavstore/9141"
    )

    # Tambahkan list voucher ke pesan jika user punya voucher aktif
    if active_vouchers:
        teks += "\n\n🎟 <b>VOUCHER AKTIF KAMU:</b>\n"
        for v in active_vouchers:
            teks += f" ├ <code>{v['kode']}</code> (Diskon Rp{v['diskon']:,})\n"
        teks += " └ <i>Cantumkan kode ini di form order ya!</i>\n"
        teks += " └ <i>Vouch diatas 4k hanya berlaku untuk teleprem</i>"
    
    await update.message.reply_text(teks, parse_mode="HTML", disable_web_page_preview=True, reply_markup=get_main_keyboard())
    
async def handle_cancelpay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split('_')
    target_user_id = int(parts[1])
    jumlah = int(parts[2])
    
    # Ambil ID pesan menu admin (pesan tempat tombol ini berada)
    admin_msg_id = query.message.message_id

    # 1. HAPUS PESAN TAGIHAN DI CHAT BUYER
    if admin_msg_id in ADMIN_BUYER_MSG_MAP:
        buyer_msg_id = ADMIN_BUYER_MSG_MAP[admin_msg_id]["buyer_msg_id"]
        try:
            await context.bot.delete_message(chat_id=target_user_id, message_id=buyer_msg_id)
            del ADMIN_BUYER_MSG_MAP[admin_msg_id] # Bersihkan dari memori
        except Exception as e:
            logger.error(f"Gagal hapus tagihan buyer: {e}")

    # 2. Kirim pesan permintaan maaf (Opsional, tapi bagus untuk kesopanan)
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text="⚠️ <b>MOHON MAAF</b>\nTagihan sebelumnya dibatalkan karena ada kesalahan nominal/pesanan. Mohon tunggu tagihan revisi dari Admin kami ya kak 🙏",
            parse_mode="HTML"
        )
    except: pass

    # 3. Edit pesan admin agar jelas sudah batal dan dihapus
    await query.edit_message_text(
        f"❌ <b>TAGIHAN DIBATALKAN ADMIN</b>\n👤 User ID: <code>{target_user_id}</code>\n💰 Nominal Batal: Rp{jumlah:,}\n\n<i>✅ Pesan tagihan yang salah telah otomatis terhapus dari chat buyer.</i>",
        parse_mode="HTML"
    )

async def handle_channel_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if LOG_GROUP_ID == 0: return
    msg = update.channel_post or update.edited_channel_post
    if not msg or (msg.chat.username and "@" + msg.chat.username.lower() != CHANNEL_ID.lower()): return 

    admin_name = msg.from_user.first_name if msg.from_user else f"Signature: {msg.author_signature or 'Anonim'}"
    action = "Mengedit Pesan" if update.edited_channel_post else "Memposting Pesan"
    await context.bot.send_message(
        chat_id=LOG_GROUP_ID, 
        text=f"🚨 <b>CHANNEL ACTIVITY LOG</b>\n👤 Oleh: {html.escape(admin_name)}\n🛠 Aksi: {action}\n📝 Isi: {(msg.text or msg.caption or '[Media]')[:100]}...", 
        parse_mode="HTML"
    )
    
async def cmd_laporanptpt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_ID: 
        return

    if context.args:
        tanggal_input = context.args[0]
        try:
            start_date = datetime.datetime.strptime(tanggal_input, "%d/%m/%Y")
            start_date_iso = start_date.isoformat() + "Z"
            keterangan_waktu = f"Sejak {tanggal_input}"
        except ValueError:
            return await update.message.reply_text("⚠️ <b>Format tanggal salah!</b>\nGunakan format: DD/MM/YYYY\nContoh: <code>/laporanptpt 10/09/2026</code>", parse_mode="HTML")
    else:
        start_date = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        start_date_iso = start_date.isoformat() + "Z"
        keterangan_waktu = "7 Hari Terakhir"

    status_msg = await update.message.reply_text(f"⏳ Sedang merekap data {keterangan_waktu} beserta perhitungan profit & PTPT...")

    res = await db(lambda: supabase.table("orders")
                   .select("*")
                   .gte("created_at", start_date_iso)
                   .eq("status", "success")
                   .execute())
    
    if not res.data:
        return await status_msg.edit_text(f"⚠️ Belum ada data transaksi sukses untuk periode {keterangan_waktu}.")

    rekap_admin = {}
    for order in res.data:
        admin_id = order.get("admin_id")
        if not admin_id:
            continue
            
        jumlah = order.get("jumlah", 0)
        kategori = order.get("kategori", "lainnya").lower()
        
        if admin_id not in rekap_admin:
            rekap_admin[admin_id] = {"total_omzet": 0, "kategori": {}}
            
        if kategori not in rekap_admin[admin_id]["kategori"]:
            rekap_admin[admin_id]["kategori"][kategori] = {"omzet": 0, "trx": 0}
            
        rekap_admin[admin_id]["total_omzet"] += jumlah
        rekap_admin[admin_id]["kategori"][kategori]["omzet"] += jumlah
        rekap_admin[admin_id]["kategori"][kategori]["trx"] += 1

    teks = f"📊 <b>REKAP PTPT ({keterangan_waktu.upper()})</b>\n"
    teks += "━━━━━━━━━━━━━━━━━━\n"
    
    total_omzet_semua = 0
    total_ptpt_semua = 0

    for admin_id, data_admin in rekap_admin.items():
        try:
            chat = await context.bot.get_chat(admin_id)
            nama_admin = html.escape(chat.first_name)
            username_admin = f" (@{chat.username})" if chat.username else ""
        except:
            nama_admin = "Unknown Admin"
            username_admin = ""

        teks += f"👮 <b>{nama_admin}</b>{username_admin} (<code>{admin_id}</code>)\n"
        
        ptpt_admin = 0
        
        for kat, d_kat in data_admin["kategori"].items():
            omzet = d_kat["omzet"]
            trx = d_kat["trx"]
            
            if kat == "teleprem":
                profit = omzet - (trx * 48000)
                ptpt = profit * 0.08
                emoji = "💎"
            elif kat == "stars":
                profit = trx * 2000
                ptpt = profit * 0.08
                emoji = "⭐"
            elif kat == "profneeds":
                profit = omzet
                ptpt = profit * 0.08
                emoji = "🛒"
            else:
                profit = 0
                ptpt = 0
                emoji = "🏷"
            
            ptpt_admin += ptpt
            
            teks += f"├ {emoji} <b>{kat.capitalize()}</b> | {trx} Take\n"
            teks += f"│  ├ Omzet: Rp{omzet:,}\n"
            teks += f"│  ├ Profit: Rp{int(profit):,}\n"
            teks += f"│  └ PTPT (8%): Rp{int(ptpt):,}\n"
            
        teks += f"├ 💰 <b>Total Omzet: Rp{data_admin['total_omzet']:,}</b>\n"
        teks += f"└ 💸 <b>PTPT Disetor: Rp{int(ptpt_admin):,}</b>\n\n"
        
        total_omzet_semua += data_admin["total_omzet"]
        total_ptpt_semua += ptpt_admin

    teks += "━━━━━━━━━━━━━━━━━━\n"
    teks += f"🏆 <b>TOTAL OMZET TIM: Rp{total_omzet_semua:,}</b>\n"
    teks += f"🏦 <b>TOTAL PTPT TIM: Rp{int(total_ptpt_semua):,}</b>"

    await status_msg.edit_text(teks, parse_mode="HTML")

async def cmd_syncadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_ID: 
        return
        
    global userbot
    if not userbot or not userbot.is_connected:
        return await update.message.reply_text("⚠️ Userbot tidak aktif!")

    status_msg = await update.message.reply_text("⏳ Menarik data orderan dan mencocokkan command payment dari DB...", parse_mode="HTML")

    try:
        res_cmd = await db(lambda: supabase.table("dynamic_commands")
                           .select("command_name")
                           .eq("tipe", "pay")
                           .execute())
                           
        valid_commands = [f"/{cmd['command_name'].lower()}" for cmd in (res_cmd.data or [])]
        
        if not valid_commands:
            return await status_msg.edit_text("⚠️ Tidak ada command tipe 'pay' di database untuk dicocokkan.")

        res_orders = await db(lambda: supabase.table("orders").select("id, created_at").is_("admin_id", "null").execute())
        
        if not res_orders.data:
            return await status_msg.edit_text("✅ Semua data orderan sudah memiliki admin_id. Tidak ada yang perlu disinkronkan.")

        berhasil_update = 0
        
        for order in res_orders.data:
            created_str = order['created_at'].replace('Z', '+00:00')
            waktu_order = datetime.datetime.fromisoformat(created_str)
            
            async for msg in userbot.get_chat_history(ADMIN_GROUP_ID, limit=20, offset_date=waktu_order):
                if msg.from_user and not msg.from_user.is_bot and msg.text:
                    cmd_used = msg.text.split()[0].lower()
                    
                    if cmd_used in valid_commands:
                        admin_id = msg.from_user.id
                        
                        await db(lambda o_id=order['id'], a_id=admin_id: supabase.table("orders")
                                 .update({"admin_id": a_id})
                                 .eq("id", o_id)
                                 .execute())
                        
                        berhasil_update += 1
                        break 
            
            await asyncio.sleep(0.5)

        await status_msg.edit_text(
            f"✅ <b>SINKRONISASI PINTAR SELESAI!</b>\n\n"
            f"🎯 Berhasil melacak dan mencatat <b>{berhasil_update}</b> admin (khusus pembuat tagihan) ke data lama.", 
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error sync admin: {e}")
        await status_msg.edit_text(f"❌ Terjadi kesalahan saat sinkronisasi: {e}")
        
# ==========================================
# FITUR VOUCHER & CEK LOYALTY (NEW)
# ==========================================
async def cmd_allloyalty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mengecek semua data loyalty buyer dan mengurutkannya dari yang belanja paling banyak (Top 20)"""
    if update.effective_chat.id != ADMIN_GROUP_ID: return
    
    status_msg = await update.message.reply_text("⏳ Sedang menarik data loyalty semua pelanggan...")
    
    res = await db(lambda: supabase.table("loyalty_stats").select("*").execute())
    if not res.data:
        return await status_msg.edit_text("⚠️ Belum ada data loyalty sama sekali.")
        
    # Urutkan berdasarkan total belanja tertinggi
    stats = sorted(res.data, key=lambda x: (x.get('teleprem_spent', 0) + x.get('stars_spent', 0) + x.get('profneeds_spent', 0)), reverse=True)
    
    teks = "🏆 <b>TOP 20 LOYALTY BUYERS</b> 🏆\n━━━━━━━━━━━━━━━━━━\n"
    for i, row in enumerate(stats[:20], 1):
        total = row.get('teleprem_spent', 0) + row.get('stars_spent', 0) + row.get('profneeds_spent', 0)
        teks += f"<b>{i}.</b> ID: <code>{row['user_id']}</code> | Total: <b>Rp{total:,}</b>\n"
        
    await status_msg.edit_text(teks, parse_mode="HTML")

def generate_voucher_code(length=6):
    """Generate kode acak kombinasi huruf kapital dan angka"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

async def cmd_createvoucher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Membuat voucher baru dan otomatis mengirim notifikasi ke user"""
    if update.effective_chat.id != ADMIN_GROUP_ID: return
    
    # Format: /createvoucher [user_id] [nominal_diskon]
    if len(context.args) < 2:
        return await update.message.reply_text("⚠️ <b>Format Salah!</b>\nGunakan: <code>/createvoucher [USER_ID] [NOMINAL_DISKON]</code>\nContoh: <code>/createvoucher 123456789 15000</code>", parse_mode="HTML")
        
    user_id = int(context.args[0])
    try:
        diskon = int(context.args[1].replace('.', ''))
    except ValueError:
        return await update.message.reply_text("⚠️ Nominal diskon harus berupa angka!")
        
    kode = generate_voucher_code()
    
    # Simpan ke Supabase
    await db(lambda: supabase.table("vouchers").insert({
        "kode": kode,
        "user_id": user_id,
        "diskon": diskon,
        "status": "active"
    }).execute())
    
    await update.message.reply_text(f"✅ <b>Voucher Berhasil Dibuat!</b>\n\n🎟 Kode: <code>{kode}</code>\n👤 Target: <code>{user_id}</code>\n💰 Potongan: Rp{diskon:,}", parse_mode="HTML")
    
    # Kirim Notifikasi ke Buyer
    try:
        pesan_buyer = (
            f"🎉 <b>SELAMAT! KAMU MENDAPATKAN VOUCHER DISKON!</b> 🎉\n\n"
            f"Sebagai bentuk apresiasi dari kami, ini ada hadiah buat kamu:\n\n"
            f"🎟 <b>Kode Voucher:</b> <code>{kode}</code>\n"
            f"💸 <b>Potongan Harga:</b> Rp{diskon:,}\n\n"
            f"<i>Silakan tunjukkan kode voucher ini dengan me-reply pesan ini ke Admin saat kamu mau memesan ya!</i>"
        )
        await context.bot.send_message(chat_id=user_id, text=pesan_buyer, parse_mode="HTML")
        await update.message.reply_text("📩 Notifikasi voucher berhasil dikirim ke buyer!")
    except Exception as e:
        await update.message.reply_text("⚠️ <i>Gagal mengirim notifikasi ke buyer (Mungkin bot diblokir oleh user, tapi voucher tetap aktif).</i>", parse_mode="HTML")

async def cmd_checkvoucher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mengecek status voucher, bisa dipakai admin atau buyer"""
    if not context.args:
        return await update.message.reply_text("⚠️ <b>Format Salah!</b>\nGunakan: <code>/checkvoucher [KODE_VOUCHER]</code>", parse_mode="HTML")
        
    kode = context.args[0].upper()
    res = await db(lambda: supabase.table("vouchers").select("*").eq("kode", kode).execute())
    
    if not res.data:
        return await update.message.reply_text("❌ <b>Voucher tidak ditemukan atau kode salah!</b>", parse_mode="HTML")
        
    v = res.data[0]
    status_icon = "✅ <b>BISA DIPAKAI (ACTIVE)</b>" if v['status'] == 'active' else "❌ <b>SUDAH TERPAKAI (USED)</b>"
    
    teks = (
        f"🎟 <b>INFO VOUCHER: <code>{kode}</code></b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 Pemilik (User ID): <code>{v.get('user_id', 'Umum')}</code>\n"
        f"💰 Diskon: <b>Rp{v['diskon']:,}</b>\n"
        f"📊 Status: {status_icon}"
    )
    await update.message.reply_text(teks, parse_mode="HTML")

async def cmd_usevoucher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tandai voucher sebagai sudah dipakai (Admin Only)"""
    if update.effective_chat.id != ADMIN_GROUP_ID: return
    
    if not context.args:
        return await update.message.reply_text("⚠️ <b>Format Salah!</b>\nGunakan: <code>/usevoucher [KODE_VOUCHER]</code>", parse_mode="HTML")
        
    kode = context.args[0].upper()
    res = await db(lambda: supabase.table("vouchers").select("*").eq("kode", kode).execute())
    
    if not res.data:
        return await update.message.reply_text("❌ Voucher tidak ditemukan!")
        
    if res.data[0]['status'] == 'used':
        return await update.message.reply_text("⚠️ <b>Voucher ini sudah pernah di-redeem / dipakai sebelumnya!</b>", parse_mode="HTML")
        
    # Update status jadi 'used'
    await db(lambda: supabase.table("vouchers").update({"status": "used"}).eq("kode", kode).execute())
    
    await update.message.reply_text(f"✅ Voucher <code>{kode}</code> berhasil digunakan! Statusnya sekarang telah diubah menjadi <b>Terpakai (Used)</b>.", parse_mode="HTML")
    
async def cmd_listvouchered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mengecek list user yang pernah menerima voucher dan rekapnya"""
    if update.effective_chat.id != ADMIN_GROUP_ID: return
    
    status_msg = await update.message.reply_text("⏳ Menarik data user penerima voucher...")
    
    res = await db(lambda: supabase.table("vouchers").select("*").execute())
    if not res.data:
        return await status_msg.edit_text("⚠️ Belum ada history voucher yang dibagikan ke buyer.")
        
    user_vouchers = {}
    for v in res.data:
        uid = v.get('user_id')
        if not uid: continue
        
        # Inisialisasi dictionary jika user belum ada di map
        if uid not in user_vouchers:
            user_vouchers[uid] = {'total_dapet': 0, 'total_diskon': 0, 'used': 0}
        
        # Tambah statistik voucher user tersebut
        user_vouchers[uid]['total_dapet'] += 1
        user_vouchers[uid]['total_diskon'] += v.get('diskon', 0)
        if v.get('status') == 'used':
            user_vouchers[uid]['used'] += 1
            
    if not user_vouchers:
        return await status_msg.edit_text("⚠️ Belum ada user yang terdata menerima voucher.")
        
    # Urutkan berdasarkan total diskon terbesar yang pernah diterima
    sorted_users = sorted(user_vouchers.items(), key=lambda x: x[1]['total_diskon'], reverse=True)
    
    teks = "🎟 <b>DAFTAR PENERIMA VOUCHER</b>\n━━━━━━━━━━━━━━━━━━\n"
    for i, (uid, data) in enumerate(sorted_users[:30], 1): # Limit 30 user agar pesan tidak terlalu panjang
        teks += (f"<b>{i}.</b> ID: <code>{uid}</code>\n"
                 f"├ 🎁 Diterima: {data['total_dapet']} Voucher ({data['used']} Terpakai)\n"
                 f"└ 💰 Total Nilai: Rp{data['total_diskon']:,}\n\n")
                 
    if len(sorted_users) > 30:
        teks += f"<i>...dan {len(sorted_users) - 30} user lainnya.</i>"
        
    await status_msg.edit_text(teks, parse_mode="HTML")

async def cmd_minspent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mencari user yang total belanjanya lebih dari nominal tertentu beserta total voucher yang didapat"""
    if update.effective_chat.id != ADMIN_GROUP_ID: return
    
    if not context.args:
        return await update.message.reply_text("⚠️ <b>Format Salah!</b>\nGunakan: <code>/minspent [NOMINAL]</code>\nContoh: <code>/minspent 150000</code>", parse_mode="HTML")
        
    try:
        # Hapus titik jika admin ngetik pakai titik (ex: 150.000)
        threshold = int(context.args[0].replace('.', ''))
    except ValueError:
        return await update.message.reply_text("⚠️ Nominal harus berupa angka! (Misal: 150000)")
        
    status_msg = await update.message.reply_text(f"⏳ Mencari user dengan total belanja minimal <b>Rp{threshold:,}</b>...", parse_mode="HTML")
    
    # 1. Tarik data loyalty dan voucher secara berbarengan biar lebih cepat
    res_loyalty, res_voucher = await asyncio.gather(
        db(lambda: supabase.table("loyalty_stats").select("*").execute()),
        db(lambda: supabase.table("vouchers").select("user_id, diskon").execute())
    )
    
    if not res_loyalty.data:
        return await status_msg.edit_text("⚠️ Belum ada data transaksi loyalty.")
        
    # 2. Bikin mapping total diskon voucher per user ID
    voucher_map = {}
    if res_voucher.data:
        for v in res_voucher.data:
            uid = v.get('user_id')
            if uid:
                voucher_map[uid] = voucher_map.get(uid, 0) + v.get('diskon', 0)
                
    sultans = []
    # 3. Filter dan hitung gabungan datanya
    for row in res_loyalty.data:
        total = row.get('teleprem_spent', 0) + row.get('stars_spent', 0) + row.get('profneeds_spent', 0)
        if total >= threshold:
            uid = row['user_id']
            sultans.append({
                'user_id': uid,
                'total': total,
                'total_diskon': voucher_map.get(uid, 0) # Ambil dari mapping
            })
            
    if not sultans:
        return await status_msg.edit_text(f"⚠️ Belum ada satupun user yang total belanjanya mencapai <b>Rp{threshold:,}</b>.", parse_mode="HTML")
        
    # Urutkan dari yang belanjanya paling besar
    sultans = sorted(sultans, key=lambda x: x['total'], reverse=True)
    
    teks = f"👑 <b>BUYER DENGAN SPEND >= Rp{threshold:,}</b>\n━━━━━━━━━━━━━━━━━━\n"
    teks += f"📊 Ditemukan: <b>{len(sultans)} User</b>\n\n"
    
    for i, data in enumerate(sultans[:40], 1): # Maksimal tampilkan 40 user 
        # Tambahkan info diskon kalau dia pernah dapet voucher
        diskon_info = f"\n  └ 🎟 Total Diskon Didapat: <b>Rp{data['total_diskon']:,}</b>" if data['total_diskon'] > 0 else ""
        
        teks += f"<b>{i}.</b> <code>{data['user_id']}</code> ━ <b>Rp{data['total']:,}</b>{diskon_info}\n"
        
    if len(sultans) > 40:
        teks += f"\n<i>...dan {len(sultans) - 40} user lainnya.</i>"
        
    await status_msg.edit_text(teks, parse_mode="HTML")

# ==========================================
# FITUR REFERAL (TELEPREM) - ANTI AKUN BODONG
# ==========================================
async def cmd_referal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan profil referal untuk buyer dengan validasi ketat"""
    if not await check_forcesub(update, context): return
    
    if update.effective_chat.id == ADMIN_GROUP_ID and update.message.reply_to_message:
        user_id = await get_target_id(update.message.reply_to_message)
        if not user_id: return
    else:
        if update.effective_chat.type != 'private': return
        user_id = update.effective_user.id

    # 🔒 KEAMANAN 1: Cek apakah user sudah pernah jajan sukses di kita
    res_orders = await db(lambda: supabase.table("orders").select("id").eq("user_id", user_id).eq("status", "success").limit(1).execute())
    if not res_orders.data:
        pesan_tolak = (
            "Ups, maaf yaa sayang! 🥺\n\n"
            "Kamu belum bisa ikutan program referal karena sistem mencatat kamu belum pernah jajan di @DECAVSTORE.\n\n"
            "Yuk jajan dulu minimal 1 kali buat nge-<i>unlock</i> fitur invite dan dapetin voucher diskonnya!\n\n"
            "Silakan cek /profile jika ini sebuah kesalahan atau kekeliruan."
        )
        return await update.message.reply_text(pesan_tolak, parse_mode="HTML")

    # Ambil data referal jika lolos pengecekan
    res = await db(lambda: supabase.table("loyalty_stats").select("referral_count, referral_reward_total").eq("user_id", user_id).execute())
    
    ref_count = 0
    ref_reward = 0
    if res.data:
        ref_count = res.data[0].get("referral_count") or 0
        ref_reward = res.data[0].get("referral_reward_total") or 0

    teks = (
        f"✨ <b>PROFIL REFERAL KAMU</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>ID Referal:</b> <code>{user_id}</code>\n\n"
        f"👥 <b>Bestie yang diajak:</b> <b>{ref_count} Orang</b>\n"
        f"🎁 <b>Total Voucher Didapat:</b> <b>Rp{ref_reward:,}</b>\n\n"
        f"<i>💡 <b>Cara ikutan:</b> Yuk cavs, ajak temen kamu buat jajan Teleprem di sini! "
        f"Suruh mereka cantumin ID Referalnya kamu pas lagi ngisi form order. Nanti temenmu dapet potongan 1k, "
        f"dan kamu dapet voucher diskon 2k yang bisa dipake buat order Manips atau Teleprem lho!</i>"
    )
    await update.message.reply_text(teks, parse_mode="HTML", reply_markup=get_main_keyboard())

async def cmd_addreferal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin memvalidasi referal dengan proteksi akun bodong"""
    if update.effective_chat.id != ADMIN_GROUP_ID: return

    if len(context.args) < 3:
        return await update.message.reply_text(
            "⚠️ <b>Format Salah!</b>\nGunakan: <code>/addreferal [ID_PENGAJAK] [ID_YANG_DIAJAK] [NOMINAL_VOUCHER]</code>\n"
            "Contoh: <code>/addreferal 111222 999888 2000</code>", 
            parse_mode="HTML"
        )

    try:
        pengajak_id = int(context.args[0])
        baru_id = int(context.args[1])
        nominal_voucher = int(context.args[2].replace('.', ''))
    except ValueError:
        return await update.message.reply_text("⚠️ ID dan Nominal harus berupa angka!")

    status_msg = await update.message.reply_text("⏳ Bentar ya, lagi ngecek kelayakan pengajak dan orderan member barunya...")

    # 🔒 KEAMANAN 1: Validasi Pengajak (Harus punya riwayat order 'success' minimal 1)
    res_pengajak_orders = await db(lambda: supabase.table("orders").select("id").eq("user_id", pengajak_id).eq("status", "success").limit(1).execute())
    if not res_pengajak_orders.data:
        return await status_msg.edit_text(
            f"⚠️ <b>REFERAL DITOLAK:</b> Pengajak (ID: <code>{pengajak_id}</code>) tercatat belum pernah jajan di DECAVSTORE!\n"
            f"<i>Sistem memblokir proses ini karena terdeteksi indikasi akun bodong.</i>", 
            parse_mode="HTML"
        )

    # 🔒 KEAMANAN 2: Validasi Member Baru (Maksimal cuma boleh punya 1 order success yang barusan dibuat)
    res_orders = await db(lambda: supabase.table("orders").select("id").eq("user_id", baru_id).eq("status", "success").execute())
    if res_orders.data and len(res_orders.data) > 1:
        return await status_msg.edit_text(
            f"⚠️ <b>REFERAL DITOLAK:</b> Member yang diajak (ID: <code>{baru_id}</code>) udah pernah jajan sebelumnya ({len(res_orders.data)} kali sukses). Ini bukan pengguna baru yaa!", 
            parse_mode="HTML"
        )

    # 3. UPDATE STATS PENGAJAK
    res_pengajak = await db(lambda: supabase.table("loyalty_stats").select("referral_count, referral_reward_total").eq("user_id", pengajak_id).execute())
    
    ref_count = 1
    ref_reward = nominal_voucher
    
    if res_pengajak.data:
        curr_count = res_pengajak.data[0].get("referral_count") or 0
        curr_reward = res_pengajak.data[0].get("referral_reward_total") or 0
        ref_count = curr_count + 1
        ref_reward = curr_reward + nominal_voucher

    await db(lambda: supabase.table("loyalty_stats").upsert({
        "user_id": pengajak_id,
        "referral_count": ref_count,
        "referral_reward_total": ref_reward
    }).execute())

    # 4. BUAT VOUCHER
    kode = generate_voucher_code()
    await db(lambda: supabase.table("vouchers").insert({
        "kode": kode,
        "user_id": pengajak_id,
        "diskon": nominal_voucher,
        "status": "active"
    }).execute())

    # 5. LAPORAN KE GRUP ADMIN
    await status_msg.edit_text(
        f"✅ <b>REFERAL BERHASIL DIVALIDASI!</b>\n\n"
        f"👤 Pengajak: <code>{pengajak_id}</code>\n"
        f"👤 Member Baru: <code>{baru_id}</code>\n"
        f"🎟 Kode Voucher: <code>{kode}</code>\n"
        f"💰 Nilai: Rp{nominal_voucher:,}\n\n"
        f"<i>Notifikasi DM & voucher lagi meluncur ke ID Pengajak... 🚀</i>",
        parse_mode="HTML"
    )

    # 6. KIRIM NOTIFIKASI KE PENGAJAK
    try:
        pesan_pengajak = (
            f"🎉 <b>YAY! ADA YANG PAKE KODE REFERAL KAMU NIH!</b> 🎉\n\n"
            f"Makasih banyak yaa udah ngajakin temen kamu jajan Teleprem di @DECAVSTORE 🥰\n"
            f"Sebagai tanda cinta dari admin, ini ada voucher diskon spesial buat kamu:\n\n"
            f"🎟 <b>Kode Voucher:</b> <code>{kode}</code>\n"
            f"💸 <b>Potongan Harga:</b> Rp{nominal_voucher:,}\n\n"
            f"⚠️ <b>Catatan Penting:</b> Voucher ini cuma bisa kamu tukerin buat order <b>Manips</b> atau <b>Teleprem</b> aja ya kak!\n\n"
            f"<i>Cek sisa voucher aktif kamu dengan ketik /profile, atau ketik /referal buat liat total temen yang udah kamu ajak.</i>"
        )
        await context.bot.send_message(chat_id=pengajak_id, text=pesan_pengajak, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text("⚠️ <i>Gagal mengirim notifikasi DM ke pengajak (Mungkin botnya belum di-start sama dia). Tapi data & vouchernya tetep aman tersimpan!</i>", parse_mode="HTML")
        
def get_one_month_later(dt):
    """Fungsi bantu buat nge-set waktu exactly 1 bulan ke depan"""
    month = dt.month + 1
    year = dt.year
    if month == 13:
        month = 1
        year += 1
    day = dt.day
    while True:
        try:
            return dt.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1 # Handle kalau misal bulan depan gak ada tanggal 31

async def cmd_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ngecek orang yang beli teleprem 1 bulan lalu buat di-remind"""
    if update.effective_chat.id != ADMIN_GROUP_ID: return
    
    status_msg = await update.message.reply_text("⏳ Bentar, lagi ngecek data teleprem bulan lalu...")
    
    # Ambil waktu sekarang di WIB (UTC+7)
    wib = datetime.timezone(datetime.timedelta(hours=7))
    now_wib = datetime.datetime.now(wib)
    
    # Tarik data order teleprem success yang dibuat dalam 60 hari terakhir biar enteng
    dua_bulan_lalu = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=60)).isoformat()
    res = await db(lambda: supabase.table("orders").select("user_id, created_at").eq("kategori", "teleprem").eq("status", "success").gte("created_at", dua_bulan_lalu).execute())
    
    today_users = []
    tmrw_users = []
    next_closest_date = None
    
    if res.data:
        for row in res.data:
            # Parse created_at dari UTC ke WIB
            created_utc = datetime.datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
            created_wib = created_utc.astimezone(wib)
            
            # Tambah 1 bulan untuk tanggal expired-nya
            expire_wib = get_one_month_later(created_wib)
            
            if expire_wib.date() == now_wib.date():
                today_users.append({'uid': row['user_id'], 'expire': expire_wib})
            elif expire_wib.date() == (now_wib + datetime.timedelta(days=1)).date():
                tmrw_users.append({'uid': row['user_id'], 'expire': expire_wib})
            elif expire_wib.date() > now_wib.date():
                # Nyari kapan ada yang expired selanjutnya
                if not next_closest_date or expire_wib < next_closest_date:
                    next_closest_date = expire_wib
                    
    # Kalau hari ini sama besok KOSONG
    if not today_users and not tmrw_users:
        if next_closest_date:
            tgl = next_closest_date.strftime('%d %b %Y')
            return await status_msg.edit_text(f"⚠️ Gak ada yang habis hari ini atau besok.\n\nEh baru ada yang beli teleprem bulan kemarin tu tanggal segini nih, bakal habis di: <b>{tgl}</b>", parse_mode="HTML")
        else:
            return await status_msg.edit_text("⚠️ Belum ada data pembelian teleprem sama sekali bulan kemarin.")

    # Kalau ada data, simpan ke memori buat dieksekusi sama tombol
    task_id = str(uuid.uuid4())[:8]
    REMINDER_CACHE[task_id] = {'today': today_users, 'tomorrow': tmrw_users}
    
    teks = "🔔 <b>DATA REMINDER TELEPREM</b>\n━━━━━━━━━━━━━━━━━━\n"
    
    if today_users:
        teks += "🔴 <b>HABIS HARI INI:</b>\n"
        for u in today_users:
            if now_wib > u['expire']:
                teks += f" ├ <code>{u['uid']}</code> (Udah Expired!)\n"
            else:
                diff = u['expire'] - now_wib
                hours, rem = divmod(diff.seconds, 3600)
                mins = rem // 60
                teks += f" ├ <code>{u['uid']}</code> (Sisa {hours}j {mins}m)\n"
                
    if tmrw_users:
        teks += "\n🟡 <b>HABIS BESOK:</b>\n"
        for u in tmrw_users:
            teks += f" ├ <code>{u['uid']}</code> (Jam {u['expire'].strftime('%H:%M')})\n"
            
    # Buat tombol inlinenya
    keyboard = []
    if today_users:
        keyboard.append([InlineKeyboardButton("📢 Peringatkan Hari Ini", callback_data=f"remind_{task_id}_today")])
    if tmrw_users:
        keyboard.append([InlineKeyboardButton("📢 Peringatkan Besok", callback_data=f"remind_{task_id}_tmrw")])
    if today_users and tmrw_users:
        keyboard.append([InlineKeyboardButton("📢 Peringatkan Keduanya", callback_data=f"remind_{task_id}_all")])
        
    await status_msg.edit_text(teks, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def handle_reminder_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nanganin pas tombol peringatkan diklik admin"""
    query = update.callback_query
    parts = query.data.split('_')
    task_id = parts[1]
    action = parts[2]
    
    if task_id not in REMINDER_CACHE:
        return await query.edit_message_text(f"{query.message.text}\n\n❌ Sesi reminder ini udah basi/kadaluarsa.")
        
    cache = REMINDER_CACHE[task_id]
    wib = datetime.timezone(datetime.timedelta(hours=7))
    now_wib = datetime.datetime.now(wib)
    
    targets = []
    if action in ['today', 'all']:
        targets.extend([('today', u) for u in cache['today']])
    if action in ['tmrw', 'all']:
        targets.extend([('tmrw', u) for u in cache['tomorrow']])
        
    await query.edit_message_text(f"{query.message.text}\n\n⏳ <i>Sedang ngirim {len(targets)} DM...</i>", parse_mode="HTML")
    
    sc, fc = 0, 0
    for dtype, u in targets:
        uid = u['uid']
        expire = u['expire']
        
        # Wording sesuai kondisi waktu expired-nya
        if dtype == 'today':
            if now_wib > expire:
                msg = "Halo! eh teleprem kamu udah abis ya? wkwkw. yuk ppj sekarang biar fiturnya balik lagi! 🥰"
            else:
                diff = expire - now_wib
                h, r = divmod(diff.seconds, 3600)
                m = r // 60
                msg = f"Halo sayang! eh teleprem kamu bentar lagi abis nih, sisa {h} jam {m} menit. mau perpanjang dari sekarang ga? 🥰"
        else:
            msg = "Halo! sekedar ngingetin, eh teleprem kamu besok abis nih. mau perpanjangan dari sekarang ga biar ga putus? 🥰"
            
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
            sc += 1
        except:
            fc += 1
            
    # Hapus datanya dari cache biar ga keklik dua kali
    del REMINDER_CACHE[task_id]
    
    await query.edit_message_text(f"{query.message.text}\n\n✅ <b>Reminder Selesai Di-blast!</b>\nBerhasil: {sc} | Gagal (bot diblok): {fc}", parse_mode="HTML")
        
async def get_id_via_ghclone(username: str) -> int:
    """Fungsi pembantu untuk mencari ID via bot pihak ketiga"""
    global userbot
    try:
        # Kirim perintah ke bot ghclone
        await userbot.send_message("@ghclone1bot", f".info @{username}")
        await asyncio.sleep(2.5) # Kasih jeda waktu agar bot merespons
        
        # Tarik 2 pesan terakhir dari chat userbot dengan ghclone1bot
        async for bot_reply in userbot.get_chat_history("@ghclone1bot", limit=2):
            if bot_reply.text and "👤" in bot_reply.text:
                match = re.search(r'👤\s*(\d+)', bot_reply.text)
                if match:
                    return int(match.group(1))
    except Exception as e:
        logger.error(f"Gagal melacak ID via ghclone: {e}")
    return None

async def cmd_scrap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_ID: return
    
    global userbot
    if not userbot or not userbot.is_connected:
        return await update.message.reply_text("⚠️ Userbot tidak aktif! Bot tidak bisa melakukan scrapping.")

    raw_text = ' '.join(context.args)
    if '-' not in raw_text:
        return await update.message.reply_text("⚠️ <b>Format Salah!</b>\nGunakan: <code>/scrap DD/MM/YYYY - DD/MM/YYYY</code>\nContoh: <code>/scrap 01/06/2026 - 26/06/2026</code>", parse_mode="HTML")
        
    start_str, end_str = [x.strip() for x in raw_text.split('-', 1)]
    
    try:
        start_date = datetime.datetime.strptime(start_str, "%d/%m/%Y")
        end_date = datetime.datetime.strptime(end_str, "%d/%m/%Y").replace(hour=23, minute=59, second=59)
    except ValueError:
        return await update.message.reply_text("⚠️ Format tanggal salah! Pastikan menggunakan format <b>DD/MM/YYYY</b>.", parse_mode="HTML")

    status_msg = await update.message.reply_text(f"⏳ Meminta userbot mencari 'format teleprem'...\n<i>Mengecek username via @ghclone1bot, ini akan memakan waktu lumayan lama.</i>", parse_mode="HTML")

    found_links = []
    sql_queries = [
        f"-- SQL Hasil Scrapping Format Teleprem ({start_str} s/d {end_str})",
        "-- NOTE: Kolom 'jumlah' di-set 0 sementara. Jika mau mass-update harganya, jalankan:",
        "-- UPDATE orders SET jumlah = 48000 WHERE kategori = 'teleprem' AND jumlah = 0;\n"
    ]
    
    chat_id_str = str(ADMIN_GROUP_ID).replace("-100", "")
    processed_count = 0
    
    try:
        async for msg in userbot.search_messages(ADMIN_GROUP_ID, query="format teleprem"):
            if msg.date > end_date:
                continue 
                
            if start_date <= msg.date <= end_date:
                # 1. Simpan Link
                link = f"https://t.me/c/{chat_id_str}/{msg.id}"
                found_links.append(link)
                
                # 2. Ekstrak Username & ID
                msg_text = msg.text or msg.caption or ""
                username_match = re.search(r'@([a-zA-Z0-9_]+)', msg_text)
                
                user_id = None
                if username_match:
                    username = username_match.group(1)
                    user_id = await get_id_via_ghclone(username)
                
                # 3. Buat Query SQL
                if user_id:
                    created_at = msg.date.isoformat() + "Z"
                    sql_queries.append(
                        f"INSERT INTO orders (user_id, kategori, jumlah, status, created_at) "
                        f"VALUES ({user_id}, 'teleprem', 0, 'success', '{created_at}');"
                    )
                else:
                    found_username = username_match.group(1) if username_match else 'TIDAK DITEMUKAN'
                    sql_queries.append(f"-- ❌ Gagal melacak ID. Username: @{found_username} | Link: {link}")
                
                # Update status tiap 5 data biar admin tau botnya ga mati
                processed_count += 1
                if processed_count % 5 == 0:
                    try: await status_msg.edit_text(f"⏳ Sedang memproses data... ({processed_count} pesan ditemukan)")
                    except: pass
                    
            elif msg.date < start_date:
                break 
    except Exception as e:
        return await status_msg.edit_text(f"❌ Error saat scrapping: {e}")

    if not found_links:
        return await status_msg.edit_text(f"⚠️ Tidak ditemukan satupun pesan yang mengandung kata 'format teleprem' pada periode {start_str} - {end_str}.")

    # Balik urutan agar yang tertua diproses duluan
    found_links.reverse()
    sql_queries.reverse()
    
    teks_header = f"📊 <b>HASIL SCRAPPING FORMAT TELEPREM</b>\n📅 {start_str} - {end_str}\n🔍 Total: {len(found_links)} pesan\n\n"
    
    # Generate file output
    link_content = teks_header.encode('utf-8') + "\n".join([f"{i+1}. {l}" for i, l in enumerate(found_links)]).encode('utf-8')
    sql_content = "\n".join(sql_queries).encode('utf-8')

    # Kirim hasil
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=link_content,
        filename=f"Links_Teleprem_{start_str.replace('/','')}.txt",
        caption=f"{teks_header}✅ <b>Proses Selesai!</b>\nBerikut adalah file Link dan query SQL yang siap dimasukkan ke Supabase.",
        parse_mode="HTML"
    )
    
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=sql_content,
        filename=f"Insert_Supabase_{start_str.replace('/','')}.sql"
    )
    
    await status_msg.delete()
        
async def cmd_fixmanual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_ID: return
    
    global userbot
    if not userbot or not userbot.is_connected:
        return await update.message.reply_text("⚠️ Userbot tidak aktif!")

    status_msg = await update.message.reply_text("⏳ Mengeksekusi pencarian manual untuk sisa data yang gagal...\n<i>Tunggu sebentar ya...</i>", parse_mode="HTML")
    
    # Mapping Message ID dari link -> Username valid yang baru
    # Format link sebelumnya: https://t.me/c/3138903380/[MESSAGE_ID]
    manual_data = {
        40537: "NINGENINGYIZHUO", # @owlnja
        40572: "shaceyn",         # @klttenjpg
        40693: "mrscedes",        # @puembaik
        40760: "wZhaoqYufan",     # @ParkqWonbinl
        40789: "cxpired",         # @distfract
        40967: "jForge",          # @Vigorf
        41030: "Jkungkook",       # Fragment
        41190: "wmiist",          # @xfootbaII
        41520: "Brsngsekk",       # @brsgsek
        41959: "gleeamy"          # @ceallalily
    }
    
    sql_queries = [
        "-- SQL Hasil Fix Manual Data Nyangkut",
        "-- NOTE: Kolom jumlah di-set 0. Jangan lupa di-UPDATE massal kalau perlu.\n"
    ]
    
    # Iterasi satu-satu
    for msg_id, target_usn in manual_data.items():
        try:
            # 1. Tarik pesannya via Message ID buat dapetin tanggal order
            msg = await userbot.get_messages(ADMIN_GROUP_ID, msg_id)
            if not msg or msg.empty:
                sql_queries.append(f"-- ❌ Pesan ID {msg_id} udah dihapus atau ga ketemu di grup.")
                continue
                
            created_at = msg.date.isoformat() + "Z"
            
            # 2. Cari ID via ghclone menggunakan username baru
            user_id = await get_id_via_ghclone(target_usn)
            
            # 3. Format query ke SQL
            if user_id:
                sql_queries.append(
                    f"INSERT INTO orders (user_id, kategori, jumlah, status, created_at) "
                    f"VALUES ({user_id}, 'teleprem', 0, 'success', '{created_at}');"
                )
            else:
                sql_queries.append(f"-- ❌ Masih Gagal! Username: @{target_usn} (Bisa jadi bot limit/akun fragmen) | Link Msg: https://t.me/c/3138903380/{msg_id}")
                
            await asyncio.sleep(3) # Kasih jeda agak panjang biar bot ghclone gak ngambek
            
        except Exception as e:
            sql_queries.append(f"-- ❌ Error saat ngeproses Msg ID {msg_id}: {e}")
            
    # Compile text jadi dokumen
    sql_content = "\n".join(sql_queries).encode('utf-8')
    
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=sql_content,
        filename=f"Fix_Manual_Teleprem.sql",
        caption="✅ <b>Proses Fix Manual Selesai!</b>\nYang batal beli sama <i>@decavstore</i> udah otomatis di-skip.",
        parse_mode="HTML"
    )
    await status_msg.delete()
    
# ==========================================
# FITUR EDIT PESAN BROADCAST (/editbbc)
# ==========================================
async def cmd_editbbc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_GROUP_ID: return ConversationHandler.END
    if not context.args:
        await update.message.reply_text("⚠️ <b>Format Salah!</b>\nGunakan: <code>/editbbc [link_pesan]</code>", parse_mode="HTML")
        return ConversationHandler.END

    link = context.args[0]
    match_private = re.search(r"t\.me/c/(\d+)/(\d+)", link)
    match_public = re.search(r"t\.me/([a-zA-Z0-9_]+)/(\d+)", link)
    
    if match_private:
        target_chat = f"-100{match_private.group(1)}"
        target_msg_id = int(match_private.group(2))
    elif match_public:
        target_chat = f"@{match_public.group(1)}"
        target_msg_id = int(match_public.group(2))
    else:
        await update.message.reply_text("❌ Link tidak valid! Pastikan itu link pesan Telegram.")
        return ConversationHandler.END

    context.user_data['edit_target_chat'] = target_chat
    context.user_data['edit_target_msg_id'] = target_msg_id

    # Tambahkan tombol Skip Content di sini
    keyboard = [[InlineKeyboardButton("⏭️ Skip (Biarkan Teks & Media Lama)", callback_data="skip_content")]]
    
    await update.message.reply_text(
        "📝 <b>Silakan kirimkan Teks dan Media terbaru!</b>\n\n"
        "💡 <i>Tips:</i>\n"
        "• Jika kamu hanya mengirim Teks, media di pesan lama <b>tidak akan dihapus</b>.\n"
        "• Jika kamu mengirim Foto/Video baru, media lama akan terganti.\n\n"
        "<i>Tekan tombol Skip di bawah jika kamu HANYA ingin mengubah tombol inline-nya saja.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAIT_CONTENT

async def handle_editbbc_skip_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # Beri tanda bahwa admin men-skip input konten (kosong)
    context.user_data['new_msg'] = None
    
    keyboard = [[InlineKeyboardButton("⏭️ Skip (Tanpa Tombol/Hapus Tombol)", callback_data="skip_buttons")]]
    await query.edit_message_text(
        "⚙️ <b>Apakah mau menambahkan tombol inline?</b>\n\n"
        "Jika <b>IYA</b>, balas dengan format ini (Teks | Link):\n"
        "<code>Order Sekarang | https://t.me/botkamu\nHubungi Admin | https://t.me/admin</code>\n\n"
        "Jika <b>TIDAK</b> (atau mau hapus tombol lama), silakan pencet tombol Skip di bawah ini.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )
    return WAIT_BUTTONS

async def handle_editbbc_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Simpan pesan baru untuk dieksekusi nanti
    context.user_data['new_msg'] = update.message

    keyboard = [[InlineKeyboardButton("⏭️ Skip (Tanpa Tombol)", callback_data="skip_buttons")]]
    await update.message.reply_text(
        "⚙️ <b>Apakah mau menambahkan tombol inline?</b>\n\n"
        "Jika <b>IYA</b>, balas dengan format ini (Teks | Link):\n"
        "<code>Order Sekarang | https://t.me/botkamu\nHubungi Admin | https://t.me/admin</code>\n\n"
        "Jika <b>TIDAK</b>, silakan pencet tombol Skip di bawah ini.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )
    return WAIT_BUTTONS

def parse_inline_buttons(text: str, columns: int = 1):
    """Mengubah teks menjadi layout tombol inline dengan kustomisasi kolom"""
    buttons = []
    for line in text.split('\n'):
        if '|' in line:
            parts = line.split('|', 1)
            btn_text = parts[0].strip()
            btn_url = parts[1].strip()
            if not btn_url.startswith('http'): 
                btn_url = 'https://' + btn_url
            buttons.append(InlineKeyboardButton(btn_text, url=btn_url))
            
    # Pecah list tombol jadi beberapa baris sesuai jumlah kolom yang di-request
    keyboard = [buttons[i:i + columns] for i in range(0, len(buttons), columns)]
    return InlineKeyboardMarkup(keyboard) if keyboard else None

async def execute_editbbc(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_markup=None):
    target_chat = context.user_data['edit_target_chat']
    target_msg_id = context.user_data['edit_target_msg_id']
    new_msg = context.user_data.get('new_msg') # Gunakan .get() agar aman jika isinya None
    
    reply_target = update.message if update.message else update.callback_query.message

    try:
        # LOGIKA BARU: Jika skip konten, HANYA update tombolnya saja
        if new_msg is None:
            await context.bot.edit_message_reply_markup(
                chat_id=target_chat, 
                message_id=target_msg_id, 
                reply_markup=reply_markup
            )
            
        # Jika admin mengirim media baru (Foto, Video, Dokumen)
        elif new_msg.photo or new_msg.video or new_msg.document or new_msg.audio:
            if new_msg.photo:
                media = InputMediaPhoto(media=new_msg.photo[-1].file_id, caption=new_msg.caption_html, parse_mode="HTML")
            elif new_msg.video:
                media = InputMediaVideo(media=new_msg.video.file_id, caption=new_msg.caption_html, parse_mode="HTML")
            elif new_msg.document:
                media = InputMediaDocument(media=new_msg.document.file_id, caption=new_msg.caption_html, parse_mode="HTML")
            elif new_msg.audio:
                media = InputMediaAudio(media=new_msg.audio.file_id, caption=new_msg.caption_html, parse_mode="HTML")

            await context.bot.edit_message_media(chat_id=target_chat, message_id=target_msg_id, media=media, reply_markup=reply_markup)
            
        else:
            # Jika admin hanya mengirim teks
            try:
                await context.bot.edit_message_caption(
                    chat_id=target_chat, message_id=target_msg_id, 
                    caption=new_msg.text_html, parse_mode="HTML", reply_markup=reply_markup
                )
            except Exception as e:
                err_msg = str(e).lower()
                if any(x in err_msg for x in ["no caption", "no media", "not modified"]):
                    await context.bot.edit_message_text(
                        chat_id=target_chat, message_id=target_msg_id, 
                        text=new_msg.text_html, parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=True
                    )
                else:
                    raise e
                    
        await reply_target.reply_text(f"✅ <b>Pesan berhasil diperbarui!</b>", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Gagal editbbc: {e}")
        await reply_target.reply_text(f"❌ <b>Gagal mengedit pesan!</b>\n<i>Error: {e}</i>", parse_mode="HTML")
    finally:
        context.user_data.clear()

async def handle_editbbc_buttons_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Simpan teks tombolnya dulu, jangan langsung di-eksekusi
    context.user_data['raw_buttons'] = update.message.text
    
    await update.message.reply_text(
        "🔢 <b>Mau berapa tombol menyamping (kolom)?</b>\n\n"
        "Balas dengan angka saja, misal:\n"
        "<code>1</code> (Untuk tombol atas-bawah)\n"
        "<code>2</code> (Untuk 2 tombol per baris)\n"
        "<code>3</code> (Biar jejer 3, cocok buat emoji)\n\n"
        "<i>Ketik /cancel jika ingin membatalkan.</i>",
        parse_mode="HTML"
    )
    return WAIT_BUTTON_LAYOUT

async def handle_editbbc_layout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Konversi input jadi angka
        columns = int(update.message.text)
        if columns < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Harus angka minimal 1 ya! Silakan masukkan angka lagi:")
        return WAIT_BUTTON_LAYOUT

    raw_text = context.user_data.get('raw_buttons', '')
    reply_markup = parse_inline_buttons(raw_text, columns)
    
    if not reply_markup:
        await update.message.reply_text("❌ Format tombol salah! Proses dibatalkan.", parse_mode="HTML")
        context.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text(f"⏳ Memproses update pesan dengan layout {columns} kolom...")
    await execute_editbbc(update, context, reply_markup)
    return ConversationHandler.END

async def handle_editbbc_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✅ Memproses update pesan tanpa tombol...")
    await execute_editbbc(update, context, None)
    return ConversationHandler.END

async def cmd_cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Proses edit dibatalkan.")
    context.user_data.clear()
    return ConversationHandler.END
        
async def cmd_tarik(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in [ADMIN_GROUP_ID, LOG_GROUP_ID]: return
    
    # Harus reply ke pesan yang mau ditarik
    if not update.message.reply_to_message:
        notif = await update.message.reply_text("⚠️ Reply pesan balasan yang ingin ditarik/dihapus dari buyer.")
        asyncio.create_task(delete_after_delay(context.bot, update.effective_chat.id, notif.message_id, 5))
        return

    target_msg_id = update.message.reply_to_message.message_id
    
    # Cek apakah pesan tersebut ada di dalam memori pelacak bot kita
    if target_msg_id in ADMIN_BUYER_MSG_MAP:
        mapping = ADMIN_BUYER_MSG_MAP[target_msg_id]
        user_id = mapping["user_id"]
        buyer_msg_id = mapping["buyer_msg_id"]

        try:
            # 1. Hapus pesan di chat buyer
            await context.bot.delete_message(chat_id=user_id, message_id=buyer_msg_id)
            
            # 2. Hapus pesan command /del itu sendiri biar grup tetap rapi
            await update.message.delete()
            
            # 3. Hapus pesan balasan admin di grup
            await update.message.reply_to_message.delete()
            
            # Hapus dari memori agar tidak menumpuk
            del ADMIN_BUYER_MSG_MAP[target_msg_id]
            
            # Kirim notif sukses lalu hapus notifnya otomatis dalam 3 detik
            notif = await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ Pesan berhasil ditarik dari chat buyer (ID: <code>{user_id}</code>).", parse_mode="HTML")
            asyncio.create_task(delete_after_delay(context.bot, update.effective_chat.id, notif.message_id, 3))
            
        except Exception as e:
            logger.error(f"Gagal menarik pesan: {e}")
            notif = await update.message.reply_text("⚠️ Gagal menarik pesan. (Mungkin pesan sudah terlalu lama atau sudah dihapus user).")
            asyncio.create_task(delete_after_delay(context.bot, update.effective_chat.id, notif.message_id, 5))
    else:
        notif = await update.message.reply_text("⚠️ Pesan ini tidak terdaftar di sistem. Bot tidak bisa menariknya.")
        asyncio.create_task(delete_after_delay(context.bot, update.effective_chat.id, notif.message_id, 5))
    
# ==========================================
# ASYNC HOOKS & MAIN
# ==========================================
def fake_input(prompt):
    raise RuntimeError("Pyrogram mencoba memblokir terminal, membatalkan paksa agar Bot utama tidak nge-hang!")

async def post_init(application: Application):
    global userbot
    if not userbot: return logger.warning("Kredensial Userbot kosong di .env atau gagal diinisialisasi.")
        
    original_input = builtins.input
    builtins.input = fake_input 
    try:
        await userbot.start()
        logger.info("✅ Userbot berhasil tersambung pada proses startup!")
    except Exception as e: logger.error(f"❌ Userbot gagal online saat startup: {e}")
    finally: builtins.input = original_input

async def post_shutdown(application: Application):
    if userbot and userbot.is_connected:
        try: await userbot.stop()
        except: pass

def main():
    if not BOT_TOKEN: return logger.error("BOT_TOKEN tidak ditemukan!")
    
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30.0) 
        .read_timeout(30.0)    
        .write_timeout(30.0)   
        .pool_timeout(30.0)    
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    
    application.job_queue.run_repeating(auto_clear_cache, interval=300, first=300)
    
    # === TAMBAHKAN BLOK INI ===
    editbbc_handler = ConversationHandler(
        entry_points=[CommandHandler('editbbc', cmd_editbbc)],
        states={
            WAIT_CONTENT: [
                MessageHandler(filters.ALL & ~filters.COMMAND, handle_editbbc_content),
                # Tambahkan handler skip ini:
                CallbackQueryHandler(handle_editbbc_skip_content, pattern="^skip_content$")
            ],
            WAIT_BUTTONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_editbbc_buttons_text),
                CallbackQueryHandler(handle_editbbc_skip, pattern="^skip_buttons$")
            ],
            WAIT_BUTTON_LAYOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_editbbc_layout)]
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel_edit)],
        per_chat=True,
        per_user=True
    )
    application.add_handler(editbbc_handler)
    # =========================

    application.add_handler(CommandHandler(['start', 'inbox'], cmd_start_inbox))
    application.add_handler(CommandHandler(['bc', 'broadcast'], broadcast))
    application.add_handler(CommandHandler('broadcastfw', broadcast_forward))
    application.add_handler(CommandHandler('setpayment', cmd_setpayment))
    application.add_handler(CommandHandler('setafterpay', cmd_setafterpay))
    application.add_handler(CommandHandler('fix', cmd_fix))
    application.add_handler(CommandHandler('grupid', cmd_grupid))
    application.add_handler(CommandHandler('addloyalty', cmd_addloyalty))
    application.add_handler(CommandHandler('laporanptpt', cmd_laporanptpt))
    application.add_handler(CommandHandler('syncadmin', cmd_syncadmin))
    application.add_handler(CommandHandler('profile', cmd_profile))
    application.add_handler(CommandHandler(['tarik', 'del'], cmd_tarik))
    application.add_handler(CommandHandler('allloyalty', cmd_allloyalty))
    application.add_handler(CommandHandler('createvoucher', cmd_createvoucher))
    application.add_handler(CommandHandler(['checkvoucher', 'cekvoucher'], cmd_checkvoucher))
    application.add_handler(CommandHandler('usevoucher', cmd_usevoucher))
    application.add_handler(CommandHandler(['listvouchered', 'listvoucher'], cmd_listvouchered))
    application.add_handler(CommandHandler(['minspent', 'ceksultan'], cmd_minspent))
    application.add_handler(CommandHandler(['referal', 'referral'], cmd_referal))
    application.add_handler(CommandHandler(['addreferal', 'accreferal'], cmd_addreferal))
    application.add_handler(CommandHandler(['reminder', 'cekppj'], cmd_reminder))
    application.add_handler(CommandHandler('scrap', cmd_scrap))
    application.add_handler(CommandHandler('fixmanual', cmd_fixmanual))
    
    application.add_handler(CallbackQueryHandler(handle_broadcast_delete_callback, pattern=r"^delbc_"))
    application.add_handler(CallbackQueryHandler(handle_category_callback, pattern=r"^addcat_"))
    application.add_handler(CallbackQueryHandler(handle_cancelpay_callback, pattern=r"^cancelpay_"))
    application.add_handler(CallbackQueryHandler(handle_reminder_cb, pattern=r"^remind_"))
    
    application.add_handler(MessageHandler(filters.ALL & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_user_message))
    application.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.Chat([ADMIN_GROUP_ID, LOG_GROUP_ID]) & filters.REPLY & ~filters.COMMAND, handle_admin_reply))
    application.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.Chat([ADMIN_GROUP_ID, LOG_GROUP_ID]), handle_edited_admin_message))
    application.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_update))
    application.add_handler(MessageHandler(filters.Chat(DISCUSSION_GROUP_ID), handle_discussion))
    application.add_handler(MessageHandler(filters.COMMAND, handle_all_commands))
    application.add_handler(MessageReactionHandler(handle_reaction_sync))

    logger.info("✅ DECAVSTORE Bot V2 (Background Async + Full Features + Anti-Freeze) siap berjalan!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
