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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, MessageReactionHandler, filters, ContextTypes
)
from supabase import create_client, Client
from pyrogram import Client as PyClient

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
ALBUM_LOCKS = set()    
ADMIN_ALBUM_CACHE = {} 
ADMIN_BUYER_MSG_MAP = {}
ADMIN_ALBUM_LOCKS = set() 
REGISTERED_USERS_CACHE = set()

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
        # Log ini opsional, biar kamu tahu fiturnya jalan di latar belakang
        logger.info("♻️ Cache user otomatis dibersihkan (Refresh 5 Menitan).")

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

async def cmd_start_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private': return
    if not await check_forcesub(update, context): return
   
    user = update.effective_user
    
    asyncio.create_task(send_admin_log(context, "User Mengakses Bot", user, f"Memulai interaksi dengan bot."))
    asyncio.create_task(bg_register_user(user.id))

    keyboard = [
        [InlineKeyboardButton("🧭 Navigasi Menu", url="https://t.me/decavstore/685")], 
        [InlineKeyboardButton("💬 Testimoni", url="https://t.me/Decavt")], 
        [InlineKeyboardButton("📊 Result", url="https://t.me/decavi"), InlineKeyboardButton("⭐ Honest Review", url="https://t.me/HRdecav")]
    ]
    await update.message.reply_text(
        f"Halo <b>{html.escape(user.first_name)}</b>! Selamat datang di bot pemesanan <b>@DECAVSTORE</b> 🛒\n\nAda yang bisa kami bantu? Silakan langsung ketik pesan di sini ya!", 
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private': return
    if not await check_forcesub(update, context): return
   
    user = update.effective_user
    user_display = f"@{user.username}" if user.username else html.escape(user.first_name)

    text_content = (update.message.text or update.message.caption or "").lower()
    tags = []
    if "teleprem" in text_content: 
        tags.append("@jakesiim, @hughtons, @daisnt") 
    if "star" in text_content or "stars" in text_content: 
        tags.append("@ennter, @leewsol")
    if "custom" in text_content: 
        tags.append("@leewsol, @aqyeela, @hughtons")
    if "manip" in text_content or "manips" in text_content: 
        tags.append("@aqyeela, @hughtons")
        
    tag_str = f"\n🔔 {' '.join(tags)}" if tags else ""

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
            
async def handle_admin_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Meneruskan reaction admin di grup ke pesan asli buyer"""
    reaction_update = update.message_reaction
    if not reaction_update: return
    if reaction_update.chat.id != ADMIN_GROUP_ID: return

    admin_msg_id = reaction_update.message_id

    # Cek apakah pesan yang di-react ada di dalam radar pelacakan bot
    if admin_msg_id in MESSAGE_USER_MAP:
        mapping = MESSAGE_USER_MAP[admin_msg_id]
        
        # Pastikan mapping berbentuk dictionary (bukan sisa cache lama)
        if isinstance(mapping, dict):
            user_id = mapping["user_id"]
            buyer_msg_id = mapping["buyer_msg_id"]
            new_reactions = reaction_update.new_reaction # Ambil emoji barunya
            
            try:
                await context.bot.set_message_reaction(
                    chat_id=user_id,
                    message_id=buyer_msg_id,
                    reaction=new_reactions
                )
            except Exception as e:
                logger.error(f"Gagal meneruskan reaction ke buyer {user_id}: {e}")
            
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
    if not context.args: return await update.message.reply_text("⚠️ <b>Format Salah!</b>\nCara pakai: <code>/fix @username</code>", parse_mode="HTML")

    username = context.args[0].replace('@', '').strip()
    status_msg = await update.message.reply_text(f"⏳ Menghubungi Userbot untuk melacak @{username}...")
    user_id = await resolve_username(username)
    
    if user_id:
        text = f"✅ <b>Sesi Berhasil Dipulihkan!</b>\n\n👤 Username: @{username}\n🆔 User ID: <code>{user_id}</code>\n\n<i>Silakan <b>REPLY</b> pesan ini langsung untuk membalas ke pembeli.</i><a href='tg://user?id={user_id}'>&#8203;</a>"
        await status_msg.edit_text(text, parse_mode="HTML")
    else: await status_msg.edit_text(f"❌ <b>Gagal melacak!</b> Pastikan @{username} benar dan Userbot aktif.", parse_mode="HTML")

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
    
    # Cek apakah command /bc ini me-reply sebuah pesan
    is_reply = bool(update.message.reply_to_message)
    
    # Jika tidak reply pesan DAN tidak ada teks di samping command, beri peringatan
    if not is_reply and not context.args: 
        return await update.message.reply_text("⚠️ <b>Format Salah!</b>\nKetik `/bc teks` ATAU balas/reply pesan yang mau di-broadcast dengan `/bc`", parse_mode="HTML")
        
    if _broadcast_running: return await update.message.reply_text("⚠️ Broadcast sedang berjalan.")

    user_list = await get_all_user_ids()
    if not user_list: return await update.message.reply_text("⚠️ Tidak ada user di database.")

    _broadcast_running = True
    sc, fc, failed_users = 0, 0, []
    status_msg = await update.message.reply_text(f"⏳ Memulai broadcast ke {len(user_list)} user...")
    
    # Ambil teks manual jika tidak mereply pesan
    message_text = ' '.join(context.args) if not is_reply else ""

    try:
        for i in range(0, len(user_list), 10):
            batch = user_list[i : i + 10]
            tasks = []
            
            for uid in batch:
                if is_reply:
                    # Kloning/Copy pesan yang direply (semua format: teks, spoiler, link, media dipertahankan utuh)
                    tasks.append(context.bot.copy_message(
                        chat_id=uid,
                        from_chat_id=update.effective_chat.id,
                        message_id=update.message.reply_to_message.message_id
                    ))
                else:
                    # Kirim pesan teks manual biasa (menggunakan render HTML)
                    tasks.append(context.bot.send_message(
                        chat_id=uid, 
                        text=message_text, 
                        parse_mode="HTML",
                        disable_web_page_preview=True
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
        f"🏆 <b>TOTAL KESELURUHAN: Rp{total_all:,}</b>"
    )

    # Tambahkan list voucher ke pesan jika user punya voucher aktif
    if active_vouchers:
        teks += "\n\n🎟 <b>VOUCHER AKTIF KAMU:</b>\n"
        for v in active_vouchers:
            teks += f" ├ <code>{v['kode']}</code> (Diskon Rp{v['diskon']:,})\n"
        teks += " └ <i>Kasih tau kode ini ke Admin pas mau bayar jajan ya!</i>"
    
    await update.message.reply_text(teks, parse_mode="HTML")
    
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
    """Mencari user yang total belanjanya lebih dari atau sama dengan nominal tertentu"""
    if update.effective_chat.id != ADMIN_GROUP_ID: return
    
    if not context.args:
        return await update.message.reply_text("⚠️ <b>Format Salah!</b>\nGunakan: <code>/minspent [NOMINAL]</code>\nContoh: <code>/minspent 150000</code>", parse_mode="HTML")
        
    try:
        # Hapus titik jika admin ngetik pakai titik (ex: 150.000)
        threshold = int(context.args[0].replace('.', ''))
    except ValueError:
        return await update.message.reply_text("⚠️ Nominal harus berupa angka! (Misal: 150000)")
        
    status_msg = await update.message.reply_text(f"⏳ Mencari user dengan total belanja minimal <b>Rp{threshold:,}</b>...", parse_mode="HTML")
    
    # Ambil semua data loyalty
    res = await db(lambda: supabase.table("loyalty_stats").select("*").execute())
    if not res.data:
        return await status_msg.edit_text("⚠️ Belum ada data transaksi loyalty.")
        
    sultans = []
    # Filter dan hitung manual di background
    for row in res.data:
        total = row.get('teleprem_spent', 0) + row.get('stars_spent', 0) + row.get('profneeds_spent', 0)
        if total >= threshold:
            sultans.append({'user_id': row['user_id'], 'total': total})
            
    if not sultans:
        return await status_msg.edit_text(f"⚠️ Belum ada satupun user yang total belanjanya mencapai <b>Rp{threshold:,}</b>.", parse_mode="HTML")
        
    # Urutkan dari yang belanjanya paling besar
    sultans = sorted(sultans, key=lambda x: x['total'], reverse=True)
    
    teks = f"👑 <b>BUYER DENGAN SPEND >= Rp{threshold:,}</b>\n━━━━━━━━━━━━━━━━━━\n"
    teks += f"📊 Ditemukan: <b>{len(sultans)} User</b>\n\n"
    
    for i, data in enumerate(sultans[:40], 1): # Maksimal tampilkan 40 user 
        teks += f"<b>{i}.</b> <code>{data['user_id']}</code> ━ <b>Rp{data['total']:,}</b>\n"
        
    if len(sultans) > 40:
        teks += f"\n<i>...dan {len(sultans) - 40} user lainnya.</i>"
        
    await status_msg.edit_text(teks, parse_mode="HTML")

# ==========================================
# FITUR REFERAL (TELEPREM)
# ==========================================
async def cmd_referal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan profil referal untuk buyer dengan bahasa yang lebih santai"""
    if not await check_forcesub(update, context): return
    
    if update.effective_chat.id == ADMIN_GROUP_ID and update.message.reply_to_message:
        user_id = await get_target_id(update.message.reply_to_message)
        if not user_id: return
    else:
        if update.effective_chat.type != 'private': return
        user_id = update.effective_user.id

    res = await db(lambda: supabase.table("loyalty_stats").select("referral_count, referral_reward_total").eq("user_id", user_id).execute())
    
    ref_count = 0
    ref_reward = 0
    if res.data:
        ref_count = res.data[0].get("referral_count") or 0
        ref_reward = res.data[0].get("referral_reward_total") or 0

    teks = (
        f"✨ <b>PROFIL REFERAL KAMU</b> ✨\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>ID Referal:</b> <code>{user_id}</code>\n\n"
        f"👥 <b>Bestie yang diajak:</b> <b>{ref_count} Orang</b>\n"
        f"🎁 <b>Total Voucher Didapat:</b> <b>Rp{ref_reward:,}</b>\n\n"
        f"<i>💡 <b>Cara ikutan:</b> Yuk ajak temen kamu buat jajan Teleprem di sini! "
        f"Suruh mereka cantumin ID Referal kamu pas lagi ngisi form order. Nanti temenmu dapet potongan 1k, "
        f"dan kamu dapet voucher diskon 2k yang bisa dipake buat order Manips atau Teleprem lho! Seru kan? 🎉</i>"
    )
    await update.message.reply_text(teks, parse_mode="HTML")

async def cmd_addreferal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin memvalidasi referal, menambah poin, dan menggenerate voucher"""
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

    status_msg = await update.message.reply_text("⏳ Bentar ya, lagi ngecek data orderan member barunya...")

    # 1. VALIDASI
    res_orders = await db(lambda: supabase.table("orders").select("id").eq("user_id", baru_id).eq("status", "success").execute())
    if res_orders.data and len(res_orders.data) > 1:
        return await status_msg.edit_text(
            f"⚠️ <b>REFERAL DITOLAK:</b> Member yang diajak (ID: <code>{baru_id}</code>) udah pernah jajan sebelumnya ({len(res_orders.data)} kali sukses). Ini bukan pengguna baru yaa!", 
            parse_mode="HTML"
        )

    # 2. UPDATE STATS PENGAJAK
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

    # 3. BUAT VOUCHER
    kode = generate_voucher_code()
    await db(lambda: supabase.table("vouchers").insert({
        "kode": kode,
        "user_id": pengajak_id,
        "diskon": nominal_voucher,
        "status": "active"
    }).execute())

    # 4. LAPORAN KE GRUP ADMIN
    await status_msg.edit_text(
        f"✅ <b>REFERAL BERHASIL DIVALIDASI!</b>\n\n"
        f"👤 Pengajak: <code>{pengajak_id}</code>\n"
        f"👤 Member Baru: <code>{baru_id}</code>\n"
        f"🎟 Kode Voucher: <code>{kode}</code>\n"
        f"💰 Nilai: Rp{nominal_voucher:,}\n\n"
        f"<i>Notifikasi DM & voucher lagi meluncur ke ID Pengajak... 🚀</i>",
        parse_mode="HTML"
    )

    # 5. KIRIM NOTIFIKASI KE PENGAJAK
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
    
    application.add_handler(CallbackQueryHandler(handle_broadcast_delete_callback, pattern=r"^delbc_"))
    application.add_handler(CallbackQueryHandler(handle_category_callback, pattern=r"^addcat_"))
    application.add_handler(CallbackQueryHandler(handle_cancelpay_callback, pattern=r"^cancelpay_"))
    
    application.add_handler(MessageHandler(filters.ALL & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_user_message))
    application.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.Chat([ADMIN_GROUP_ID, LOG_GROUP_ID]) & filters.REPLY & ~filters.COMMAND, handle_admin_reply))
    application.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.Chat([ADMIN_GROUP_ID, LOG_GROUP_ID]), handle_edited_admin_message))
    application.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_update))
    application.add_handler(MessageHandler(filters.COMMAND, handle_all_commands))
    application.add_handler(MessageReactionHandler(handle_admin_reaction))

    logger.info("✅ DECAVSTORE Bot V2 (Background Async + Full Features + Anti-Freeze) siap berjalan!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
