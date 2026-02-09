"""
Telethon Unread Terabox Link Extractor Bot (Updated)
- Keeps your provided credentials as requested.
- Behavior implemented per your final specification:
  * Home reply keyboard: [ START ] [ LIST ]
  * LIST: hides reply keyboard and shows inline chat pages (Prev/Next)
  * Selecting a chat shows reply keyboard: [ SCAN ] [ CANCEL ]
  * SCAN: scans ALL unread messages (no cap). If unread == 0 -> scans last 1000 messages.
  * Deduplicates links before sending (preserves order).
  * Pagination deletes previous inline page before showing next.
  * After scanning completes, shows the START+LIST reply keyboard again.
  * /list command is not used; LIST action comes from the reply keyboard button.
"""

import re
import asyncio
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------- CONFIG ----------
BOT_TOKEN = "8459822024:AAF1SWxvbr7LD1sy_PJg5ShcBhY9aLUQEss"
API_ID = 27400429
API_HASH = "e4585a30e42079fef123da0c70b5e5a6"
TELETHON_SESSION = "user_session"
CLEANUP_DELAY = 1  # seconds
# ---------------------------

tele_client = TelegramClient(TELETHON_SESSION, API_ID, API_HASH)
bot_sessions = {}
ITEMS_PER_PAGE = 30
REGEX = re.compile(r"https?://(?:www\.)?[^\s]*tera[^\s]*", re.IGNORECASE)


async def ensure_telethon():
    if not tele_client.is_connected():
        await tele_client.connect()
    if not await tele_client.is_user_authorized():
        print("\n🔐 LOGIN REQUIRED")
        phone = input("Enter phone with country code: ")
        await tele_client.send_code_request(phone)
        code = input("Enter the login code: ")
        try:
            await tele_client.sign_in(phone, code)
        except SessionPasswordNeededError:
            pwd = input("Enter 2FA password: ")
            await tele_client.sign_in(password=pwd)


def chunk_links(links, max_chars=4096):
    chunks, current = [], ""
    for link in links:
        if len(current) + len(link) + 1 > max_chars:
            chunks.append(current)
            current = link
        else:
            current = current + ("\n" if current else "") + link
    if current:
        chunks.append(current)
    return chunks


async def safe_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg_id: int):
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception:
        pass


async def delayed_delete(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.chat_id
    msg_id = job.data
    await asyncio.sleep(CLEANUP_DELAY)
    await safe_delete(context, chat_id, msg_id)


async def schedule_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg_id: int):
    if context.job_queue:
        # run once immediately, delayed_delete will wait CLEANUP_DELAY
        context.job_queue.run_once(delayed_delete, 0, chat_id=chat_id, data=msg_id)


async def post_init(application: Application):
    pass


# ------------------ BOT HELPERS ------------------

def home_keyboard():
    return ReplyKeyboardMarkup([["START", "LIST"]], resize_keyboard=True)


def scan_keyboard():
    return ReplyKeyboardMarkup([["SCAN", "CANCEL"]], resize_keyboard=True, one_time_keyboard=True)


async def cleanup_messages(context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg_ids: list):
    for msg_id in msg_ids:
        await schedule_delete(context, chat_id, msg_id)


# ------------------ BOT COMMANDS & HANDLERS ------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    bot_sessions[uid] = {"msg_ids": []}
    # Show home keyboard
    msg = await update.message.reply_text("✅ Bot Ready.", reply_markup=home_keyboard())
    bot_sessions[uid]["msg_ids"] = [msg.id]


async def list_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered when user presses LIST (reply keyboard) or via text 'LIST'."""
    uid = update.effective_user.id
    session = bot_sessions.get(uid, {})
    prev_msg_ids = session.get("msg_ids", [])
    chat_id = update.effective_chat.id

    # Schedule deletion of previous tracked messages
    await cleanup_messages(context, chat_id, prev_msg_ids)

    # Hide reply keyboard and show fetching message
    fetching_msg = await update.message.reply_text("⏳ Fetching chats...", reply_markup=ReplyKeyboardRemove())
    await ensure_telethon()

    chats = []
    async for dialog in tele_client.iter_dialogs():
        name = dialog.name or str(dialog.id)
        unread = dialog.unread_count or 0
        chats.append((dialog.id, name, unread))

    bot_sessions[uid] = {
        "chats": chats,
        "page": 0,
        "msg_ids": [fetching_msg.id],
        "page_msg_id": None,  # to track inline page message for deletion
    }

    await send_chat_page(update, uid, context)


async def send_chat_page(chat_or_update, uid, context: ContextTypes.DEFAULT_TYPE):
    """Sends a page of chats as an inline keyboard. Deletes previous page message first."""
    data = bot_sessions.get(uid, {})
    chats = data.get("chats", [])
    page = data.get("page", 0)

    # Determine target for replying and chat id
    if isinstance(chat_or_update, Update):
        target = chat_or_update.message
    else:
        # If passed a Message (like callback.query.message), use it directly
        target = chat_or_update

    if not chats:
        msg = await target.reply_text("❌ No chats found.", reply_markup=home_keyboard())
        data.setdefault("msg_ids", []).append(msg.id)
        bot_sessions[uid] = data
        return

    # Delete previous inline page message if exists
    prev_page_msg_id = data.get("page_msg_id")
    if prev_page_msg_id:
        try:
            await context.bot.delete_message(chat_id=target.chat_id, message_id=prev_page_msg_id)
        except Exception:
            pass
        data["page_msg_id"] = None

    start_index = page * ITEMS_PER_PAGE
    end_index = start_index + ITEMS_PER_PAGE
    sliced = chats[start_index:end_index]
    keyboard = []
    for chat_id, name, unread in sliced:
        label = f"{name} ({unread} unread)" if unread > 0 else name
        keyboard.append([InlineKeyboardButton(label[:60], callback_data=f"SEL:{chat_id}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data="PREV"))
    if end_index < len(chats):
        nav.append(InlineKeyboardButton("Next ▶️", callback_data="NEXT"))
    if nav:
        keyboard.append(nav)

    msg = await target.reply_text("📍 Select chat:", reply_markup=InlineKeyboardMarkup(keyboard))
    data["page_msg_id"] = msg.id

    # Track message for deletion (temp)
    data.setdefault("msg_ids", []).append(msg.id)
    bot_sessions[uid] = data


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data
    session = bot_sessions.get(uid, {})

    if data == "NEXT":
        session["page"] = session.get("page", 0) + 1
        bot_sessions[uid] = session
        # send page (will delete previous page message first)
        await send_chat_page(query.message, uid, context)
    elif data == "PREV":
        session["page"] = session.get("page", 0) - 1
        bot_sessions[uid] = session
        await send_chat_page(query.message, uid, context)
    elif data.startswith("SEL:"):
        chat_id = int(data.split(":", 1)[1])
        session["selected"] = chat_id

        # Try to acknowledge selection by editing the inline message; if fails, send a new one
        success = False
        try:
            await query.edit_message_text(f"✅ Chat Selected\nNow press SCAN", reply_markup=None)
            # delete that selection message after a short delay (it is temp)
            try:
                # record for deletion
                await schedule_delete(context, query.message.chat_id, query.message.id)
            except Exception:
                pass
            success = True
        except Exception:
            pass

        if not success:
            try:
                msg = await query.message.reply_text("✅ Chat Selected\nNow press SCAN")
                # schedule deletion for temp message
                await schedule_delete(context, msg.chat_id, msg.id)
            except Exception:
                pass

        # Clear tracked page messages (we already scheduled deletion of inline page)
        session["page_msg_id"] = None
        session["msg_ids"] = []  # clear temp msg tracking

        # Show SCAN + CANCEL reply keyboard
        chat = await tele_client.get_entity(chat_id)
        chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat_id)
        try:
            await context.bot.send_message(chat_id=query.from_user.id,
                                           text=f"Selected: {chat_name}\nPress SCAN to start scanning.",
                                           reply_markup=scan_keyboard())
        except Exception:
            # fallback to replying in current chat
            await query.message.reply_text(f"Selected: {chat_name}\nPress SCAN to start scanning.", reply_markup=scan_keyboard())

        bot_sessions[uid] = session


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Scan function usable both via /scan command and via 'SCAN' reply keyboard."""
    uid = update.effective_user.id
    session = bot_sessions.get(uid, {})
    chat_id = session.get("selected")

    if not chat_id:
        msg = await update.message.reply_text("❌ No chat selected. Use LIST first.", reply_markup=home_keyboard())
        await schedule_delete(context, msg.chat_id, msg.id)
        return

    # Create a cleanup list for all bot messages in this session (temporary ones)
    cleanup_ids = []
    # Add the /scan or SCAN command message for deletion
    cleanup_ids.append(update.message.id)

    await ensure_telethon()

    try:
        entity = await tele_client.get_entity(chat_id)
    except Exception as e:
        msg = await update.message.reply_text(f"❌ Could not access chat: {str(e)}", reply_markup=home_keyboard())
        await schedule_delete(context, msg.chat_id, msg.id)
        return

    name = getattr(entity, "title", None) or getattr(entity, "username", None) or str(chat_id)

    # Find unread count for this dialog
    unread_count = 0
    async for dialog in tele_client.iter_dialogs():
        if dialog.id == chat_id:
            unread_count = dialog.unread_count or 0
            break

    # If there are unread messages, scan ALL unread (no cap)
    # If 0 unread, scan last 1000 messages
    if unread_count > 0:
        limit = unread_count
        scan_unread = True
    else:
        limit = 1000
        scan_unread = False

    status = f"🔍 Scanning last {limit} message(s) of: {name}"
    if scan_unread:
        status += f" ({unread_count} unread)"
    progress_msg = await update.message.reply_text(status)
    cleanup_ids.append(progress_msg.id)  # Track for deletion

    # Get latest message ID to mark chat as read later
    latest_msg = None
    async for msg in tele_client.iter_messages(entity, limit=1):
        latest_msg = msg
        break

    links = []
    # Iterate messages (limit may be large)
    async for msg in tele_client.iter_messages(entity, limit=limit):
        texts_to_check = []
        if msg.text:
            texts_to_check.append(msg.text)
        if msg.media and hasattr(msg.media, 'caption') and msg.media.caption:
            texts_to_check.append(msg.media.caption)
        if msg.web_preview and hasattr(msg.web_preview, 'url') and msg.web_preview.url:
            texts_to_check.append(msg.web_preview.url)

        if not texts_to_check:
            continue

        full_text = "\n".join(texts_to_check)
        # Clean zero-width and some punctuation that may break regex
        clean_text = re.sub(r'[\u200B-\u200D\uFEFF\u2060]', ' ', full_text)
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()

        for match in REGEX.findall(clean_text):
            link = match.strip()
            if link:
                # Trim trailing punctuation from match
                link = re.split(r'[^\w/:.-]', link)[0]
                if link.startswith("http") and "tera" in link.lower():
                    links.append(link)

    # Deduplicate preserving order
    seen = set()
    unique_links = []
    for l in links:
        if l not in seen:
            seen.add(l)
            unique_links.append(l)

    # Delete progress message (temp)
    await schedule_delete(context, progress_msg.chat_id, progress_msg.id)

    if not unique_links:
        msg_text = "❌ No matching Terabox links found."
        if scan_unread:
            msg_text += " (in unread messages)"
        else:
            msg_text += " (in last 1000 messages)"
        msg = await update.message.reply_text(msg_text, reply_markup=home_keyboard())
        cleanup_ids.append(msg.id)
        # schedule deletion of temp ids (not the final summary/home)
        for mid in cleanup_ids:
            await schedule_delete(context, update.effective_chat.id, mid)
        # Reset session and show home keyboard
        bot_sessions[uid] = {}
        return

    # Send deduplicated links (chunks)
    chunks = chunk_links(unique_links)
    sent_link_msg_ids = []
    for chunk in chunks:
        sent = await update.message.reply_text(chunk)
        # Do NOT add link messages to cleanup list — they should remain
        sent_link_msg_ids.append(sent.id)
        await asyncio.sleep(0.1)

    # Final summary (keep this message)
    summary_msg = await update.message.reply_text(
        f"✅ Processed {name}\n"
        f"🔗 Found {len(unique_links)} Terabox link(s)\n"
        f"📨 Scanned {limit} message(s)",
        reply_markup=home_keyboard()
    )

    # ALWAYS MARK CHAT AS READ
    try:
        if latest_msg:
            await tele_client.send_read_acknowledge(entity, max_id=latest_msg.id)
        else:
            await tele_client.send_read_acknowledge(entity)
    except Exception:
        pass

    # Delete only temporary/session messages
    for mid in cleanup_ids:
        await schedule_delete(context, update.effective_chat.id, mid)

    # Reset session (keep links and summary visible)
    bot_sessions[uid] = {}


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle reply keyboard presses: START, LIST, SCAN, CANCEL"""
    text = (update.message.text or "").strip()
    uid = update.effective_user.id

    # Normalize for matching
    txt_up = text.upper()

    if txt_up == "START":
        # Reset session and show home keyboard
        bot_sessions[uid] = {"msg_ids": []}
        msg = await update.message.reply_text("✅ Bot Ready.", reply_markup=home_keyboard())
        bot_sessions[uid]["msg_ids"] = [msg.id]
        return
    elif txt_up == "LIST":
        # Invoke listing flow (hides home keyboard inside)
        await list_chats(update, context)
        return
    elif txt_up == "SCAN":
        # Trigger scan (only if a chat is selected)
        await scan(update, context)
        return
    elif txt_up == "CANCEL":
        # Cancel current selection and return to home keyboard
        bot_sessions[uid] = {}
        msg = await update.message.reply_text("Cancelled. Back to home.", reply_markup=home_keyboard())
        return
    else:
        # Unknown text -> ignore or inform
        await update.message.reply_text("Unknown action. Use START or LIST.", reply_markup=home_keyboard())
        return


# ------------------ MAIN ------------------

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # Command handlers (keep /start and /scan only)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan))

    # CallbackQuery handler for inline buttons (pagination & selection)
    app.add_handler(CallbackQueryHandler(buttons))

    # Message handler for reply keyboard buttons (START, LIST, SCAN, CANCEL)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("✅ Bot Started")
    app.run_polling()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(ensure_telethon())
    main()
