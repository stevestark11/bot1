"""
bot.py — File-sharing Telegram Bot (v3)

Owner / admin sends ANY file (document, photo, video, audio, voice,
video_note, sticker, animation) or plain text into the bot via the
"Add Item" flow.  The bot stores the Telegram file_id + type in JSON.
When a member browses a folder, the bot re-sends every item using that
file_id — Telegram serves the actual file from its own CDN, so no raw
file data is ever written to disk.

Key behaviours:
  • ONE channel membership gate (no group required)
  • Every user who interacts is saved to users.json automatically
  • Owner /users  → sends users.json as a document
  • Owner /stats  → quick summary
  • Broadcast sends to every tracked user; per-user errors reported
"""

import os
import asyncio
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from telegram.error import TelegramError, Forbidden, BadRequest

import storage

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN      = os.environ["BOT_TOKEN"]
CHANNEL_ID     = os.environ["CHANNEL_ID"]
CHANNEL_INVITE = os.environ["CHANNEL_INVITE"]
OWNER_ID       = int(os.environ.get("OWNER_ID", 0))

# ── Conversation states ───────────────────────────────────────────────────────

(
    AWAIT_FOLDER_NAME,
    AWAIT_ITEM_FOLDER,
    AWAIT_ITEM_CONTENT,
    AWAIT_BROADCAST_MSG,
) = range(4)

# ── File type detection ───────────────────────────────────────────────────────

# Maps each type name → (getter lambda, display emoji)
FILE_TYPES = {
    "document":   (lambda m: m.document,    "📄"),
    "photo":      (lambda m: m.photo,       "🖼"),
    "video":      (lambda m: m.video,       "🎬"),
    "audio":      (lambda m: m.audio,       "🎵"),
    "voice":      (lambda m: m.voice,       "🎙"),
    "video_note": (lambda m: m.video_note,  "⭕"),
    "sticker":    (lambda m: m.sticker,     "🎭"),
    "animation":  (lambda m: m.animation,   "🎞"),
}

def detect_file(message) -> tuple[str, str, str] | None:
    """
    Returns (file_id, file_type, file_name) or None if the message has no
    recognised attachment.  For photos the largest size is used.
    """
    for ftype, (getter, _) in FILE_TYPES.items():
        obj = getter(message)
        if not obj:
            continue
        if ftype == "photo":
            # obj is a tuple of PhotoSize; pick the largest
            obj = sorted(obj, key=lambda p: p.file_size or 0)[-1]
        file_id = obj.file_id
        file_name = getattr(obj, "file_name", "") or ""
        return file_id, ftype, file_name
    # plain text
    if message.text:
        return "", "text", ""
    return None

async def send_item(bot, chat_id: int, item: dict) -> None:
    """Re-deliver one stored item to a chat using its saved file_id."""
    ftype   = item["file_type"]
    file_id = item["file_id"]
    caption = item.get("caption", "") or ""

    if ftype == "text":
        await bot.send_message(chat_id=chat_id, text=caption or "—")
    elif ftype == "document":
        await bot.send_document(chat_id=chat_id, document=file_id, caption=caption)
    elif ftype == "photo":
        await bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption)
    elif ftype == "video":
        await bot.send_video(chat_id=chat_id, video=file_id, caption=caption)
    elif ftype == "audio":
        await bot.send_audio(chat_id=chat_id, audio=file_id, caption=caption)
    elif ftype == "voice":
        await bot.send_voice(chat_id=chat_id, voice=file_id, caption=caption)
    elif ftype == "video_note":
        await bot.send_video_note(chat_id=chat_id, video_note=file_id)
    elif ftype == "sticker":
        await bot.send_sticker(chat_id=chat_id, sticker=file_id)
    elif ftype == "animation":
        await bot.send_animation(chat_id=chat_id, animation=file_id, caption=caption)
    else:
        await bot.send_message(chat_id=chat_id, text=f"[unknown type: {ftype}]")

def item_label(item: dict) -> str:
    """Short display label for an item (used in delete lists)."""
    emoji = FILE_TYPES.get(item["file_type"], ("", "📎"))[1]
    name  = item.get("file_name") or item.get("caption") or item["file_type"]
    return f"{emoji} {name[:45]}{'…' if len(name) > 45 else ''}"

# ════════════════════════════════════════════════════════════════════════════
# PERMISSION HELPERS
# ════════════════════════════════════════════════════════════════════════════

def is_owner(user_id: int) -> bool:
    return bool(OWNER_ID) and user_id == OWNER_ID


async def is_admin(bot, user_id: int) -> bool:
    if is_owner(user_id):
        return True
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ("administrator", "creator")
    except TelegramError:
        return False


async def is_member(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status not in ("left", "kicked", "banned")
    except TelegramError:
        return False

# ════════════════════════════════════════════════════════════════════════════
# KEYBOARD BUILDERS
# ════════════════════════════════════════════════════════════════════════════

def join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_INVITE)],
        [InlineKeyboardButton("✅ I've Joined — Check Again", callback_data="check_membership")],
    ])


def main_menu_keyboard(is_adm: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("📂 Browse Files", callback_data="browse")]]
    if is_adm:
        rows += [
            [
                InlineKeyboardButton("➕ Add File",      callback_data="add_item"),
                InlineKeyboardButton("📁 New Folder",    callback_data="add_folder"),
            ],
            [
                InlineKeyboardButton("🗑 Delete File",   callback_data="delete_item"),
                InlineKeyboardButton("🗂 Delete Folder", callback_data="delete_folder"),
            ],
            [InlineKeyboardButton("📣 Broadcast",        callback_data="broadcast")],
        ]
    return InlineKeyboardMarkup(rows)

# ════════════════════════════════════════════════════════════════════════════
# USER TRACKING
# ════════════════════════════════════════════════════════════════════════════

async def _track(update: Update) -> None:
    user = update.effective_user
    if user and not user.is_bot:
        try:
            storage.track_user(user)
        except Exception as e:
            logger.warning("track_user failed: %s", e)

# ════════════════════════════════════════════════════════════════════════════
# /start
# ════════════════════════════════════════════════════════════════════════════

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    user = update.effective_user
    adm  = await is_admin(ctx.bot, user.id)
    mem  = adm or await is_member(ctx.bot, user.id)

    if not mem:
        await update.message.reply_text(
            "👋 Welcome!\n\nJoin our channel first to access the files.",
            reply_markup=join_keyboard(),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        f"👋 Hey {user.first_name}! Choose an option:",
        reply_markup=main_menu_keyboard(adm),
    )
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
# OWNER COMMANDS
# ════════════════════════════════════════════════════════════════════════════

async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    users     = storage.get_users()
    file_path = storage.users_file_path()
    if not users:
        await update.message.reply_text("No users recorded yet.")
        return
    await update.message.reply_document(
        document=open(file_path, "rb"),
        filename="users.json",
        caption=f"👥 *User List*\n\nTotal: *{len(users)}*",
        parse_mode="Markdown",
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Owner only.")
        return
    users   = storage.get_users()
    folders = storage.get_folders()
    items   = storage.get_all_items()
    await update.message.reply_text(
        f"📊 *Bot Stats*\n\n"
        f"👥 Users: *{len(users)}*\n"
        f"📁 Folders: *{len(folders)}*\n"
        f"📄 Files: *{len(items)}*",
        parse_mode="Markdown",
    )

# ════════════════════════════════════════════════════════════════════════════
# MEMBERSHIP CHECK
# ════════════════════════════════════════════════════════════════════════════

async def check_membership_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query = update.callback_query
    await query.answer()
    user = query.from_user
    adm  = await is_admin(ctx.bot, user.id)
    mem  = adm or await is_member(ctx.bot, user.id)
    if not mem:
        await query.edit_message_text(
            "❌ You haven't joined yet. Join and try again.",
            reply_markup=join_keyboard(),
        )
        return
    await query.edit_message_text(
        "✅ Verified! Choose an option:",
        reply_markup=main_menu_keyboard(adm),
    )

# ════════════════════════════════════════════════════════════════════════════
# BACK TO MENU
# ════════════════════════════════════════════════════════════════════════════

async def main_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query = update.callback_query
    await query.answer()
    adm = await is_admin(ctx.bot, query.from_user.id)
    await query.edit_message_text("Choose an option:", reply_markup=main_menu_keyboard(adm))
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
# BROWSE
# ════════════════════════════════════════════════════════════════════════════

async def browse_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query = update.callback_query
    await query.answer()
    user = query.from_user
    adm  = await is_admin(ctx.bot, user.id)
    mem  = adm or await is_member(ctx.bot, user.id)

    if not mem:
        await query.edit_message_text("Join the channel first.", reply_markup=join_keyboard())
        return

    folders = storage.get_folders()
    if not folders:
        await query.edit_message_text(
            "📭 No folders yet.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]]
            ),
        )
        return

    buttons = [
        [InlineKeyboardButton(
            f"📁 {f['name']}  ({f['item_count']} files)  •  {f['created_at'][:10]}",
            callback_data=f"folder_{f['id']}",
        )]
        for f in folders
    ]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="main_menu")])
    await query.edit_message_text("📂 Choose a folder:", reply_markup=InlineKeyboardMarkup(buttons))


async def folder_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query     = update.callback_query
    await query.answer()
    folder_id = query.data.split("_", 1)[1]
    folder    = storage.get_folder(folder_id)
    items     = storage.get_items(folder_id)

    if not folder:
        await query.edit_message_text("Folder not found.")
        return

    back = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Folders", callback_data="browse")]]
    )

    if not items:
        await query.edit_message_text(
            f"📁 *{folder['name']}*\n\nNo files here yet.",
            parse_mode="Markdown",
            reply_markup=back,
        )
        return

    await query.edit_message_text(
        f"📁 *{folder['name']}* — {len(items)} file(s):",
        parse_mode="Markdown",
        reply_markup=back,
    )

    for item in items:
        try:
            await send_item(ctx.bot, query.message.chat_id, item)
        except TelegramError as e:
            logger.error("Failed to send item %s: %s", item["id"], e)
            await ctx.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"⚠️ Could not deliver one file (id: {item['id']}): {e}",
            )

# ════════════════════════════════════════════════════════════════════════════
# ADMIN — Add folder
# ════════════════════════════════════════════════════════════════════════════

async def add_folder_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query = update.callback_query
    if not await is_admin(ctx.bot, query.from_user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("📁 Enter a name for the new folder:")
    return AWAIT_FOLDER_NAME


async def recv_folder_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Name can't be empty. Try again:")
        return AWAIT_FOLDER_NAME
    storage.create_folder(name)
    adm = await is_admin(ctx.bot, update.effective_user.id)
    await update.message.reply_text(
        f"✅ Folder *\"{name}\"* created.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(adm),
    )
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
# ADMIN — Add item (file or text)
# ════════════════════════════════════════════════════════════════════════════

async def add_item_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query = update.callback_query
    if not await is_admin(ctx.bot, query.from_user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    folders = storage.get_folders()
    if not folders:
        await query.edit_message_text(
            "⚠️ No folders exist. Create one first.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📁 New Folder", callback_data="add_folder"),
                    InlineKeyboardButton("⬅️ Back",       callback_data="main_menu"),
                ],
            ]),
        )
        return ConversationHandler.END

    buttons = [
        [InlineKeyboardButton(f"📁 {f['name']}", callback_data=f"pick_folder_{f['id']}")]
        for f in folders
    ]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="main_menu")])
    await query.edit_message_text(
        "Pick a folder to add the file into:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return AWAIT_ITEM_FOLDER


async def pick_folder_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query     = update.callback_query
    await query.answer()
    folder_id = query.data.split("_", 2)[2]
    folder    = storage.get_folder(folder_id)
    if not folder:
        await query.edit_message_text("Folder not found.")
        return ConversationHandler.END
    ctx.user_data["item_folder_id"]   = folder_id
    ctx.user_data["item_folder_name"] = folder["name"]
    await query.edit_message_text(
        f"📤 Send the file (or text) to save into *\"{folder['name']}\"*.\n\n"
        f"Supports: documents, photos, videos, audio, voice messages, stickers, GIFs, and plain text.",
        parse_mode="Markdown",
    )
    return AWAIT_ITEM_CONTENT


async def recv_item_content(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    message   = update.message
    folder_id = ctx.user_data.get("item_folder_id")
    if not folder_id:
        await message.reply_text("Something went wrong. Please start over from the menu.")
        return ConversationHandler.END

    detected = detect_file(message)
    if detected is None:
        await message.reply_text(
            "⚠️ I can't save that type of message. Please send a document, photo, video, audio, voice, sticker, GIF, or plain text."
        )
        return AWAIT_ITEM_CONTENT

    file_id, file_type, file_name = detected
    caption = message.caption or (message.text if file_type == "text" else "")

    try:
        storage.add_item(folder_id, file_id, file_type, caption or "", file_name)
    except KeyError as e:
        await message.reply_text(f"⚠️ Error saving: {e}")
        return ConversationHandler.END

    type_emoji = FILE_TYPES.get(file_type, ("", "📎"))[1]
    adm = await is_admin(ctx.bot, update.effective_user.id)
    await message.reply_text(
        f"✅ {type_emoji} Saved to *\"{ctx.user_data['item_folder_name']}\"*.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(adm),
    )
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
# ADMIN — Delete item
# ════════════════════════════════════════════════════════════════════════════

async def delete_item_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query = update.callback_query
    if not await is_admin(ctx.bot, query.from_user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    await query.answer()

    items = storage.get_all_items()
    if not items:
        await query.edit_message_text(
            "No files to delete.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]]
            ),
        )
        return

    buttons = [
        [InlineKeyboardButton(
            f"{item_label(c)}  ({c['folder_name']})",
            callback_data=f"delitem_{c['id']}",
        )]
        for c in items
    ]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="main_menu")])
    await query.edit_message_text(
        "Select a file to delete:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def confirm_delete_item_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query   = update.callback_query
    await query.answer()
    item_id = query.data.split("_", 1)[1]
    storage.delete_item(item_id)
    adm = await is_admin(ctx.bot, query.from_user.id)
    await query.edit_message_text("✅ File deleted.", reply_markup=main_menu_keyboard(adm))

# ════════════════════════════════════════════════════════════════════════════
# ADMIN — Delete folder
# ════════════════════════════════════════════════════════════════════════════

async def delete_folder_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query = update.callback_query
    if not await is_admin(ctx.bot, query.from_user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    await query.answer()

    folders = storage.get_folders()
    if not folders:
        await query.edit_message_text(
            "No folders to delete.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]]
            ),
        )
        return

    buttons = [
        [InlineKeyboardButton(
            f"🗑 {f['name']}  ({f['item_count']} files)",
            callback_data=f"delfolder_{f['id']}",
        )]
        for f in folders
    ]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="main_menu")])
    await query.edit_message_text(
        "Select a folder to delete *(all files inside will be removed)*:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def confirm_delete_folder_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query     = update.callback_query
    await query.answer()
    folder_id = query.data.split("_", 1)[1]
    folder    = storage.get_folder(folder_id)
    name      = folder["name"] if folder else folder_id
    storage.delete_folder(folder_id)
    adm = await is_admin(ctx.bot, query.from_user.id)
    await query.edit_message_text(
        f"✅ Folder *\"{name}\"* deleted.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(adm),
    )

# ════════════════════════════════════════════════════════════════════════════
# BROADCAST
# ════════════════════════════════════════════════════════════════════════════

async def broadcast_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query = update.callback_query
    if not await is_admin(ctx.bot, query.from_user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    users = storage.get_users()
    if not users:
        await query.edit_message_text(
            "⚠️ No users tracked yet.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]]
            ),
        )
        return ConversationHandler.END

    await query.edit_message_text(
        f"📣 *Broadcast*\n\n"
        f"Found *{len(users)}* users.\n\n"
        f"Send the message to broadcast (text, photo, video, document, etc.).\n\n"
        f"Use /cancel to abort.",
        parse_mode="Markdown",
    )
    return AWAIT_BROADCAST_MSG


async def recv_broadcast_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    if not await is_admin(ctx.bot, update.effective_user.id):
        return ConversationHandler.END

    users   = storage.get_users()
    total   = len(users)
    success = 0
    blocked_users: list[tuple[dict, str]] = []
    failed_users:  list[tuple[dict, str]] = []

    progress = await update.message.reply_text(f"📤 Sending to {total} users… please wait.")

    for i, user in enumerate(users, 1):
        uid   = user["id"]
        uname = f"@{user['username']}" if user.get("username") else f"id:{uid}"
        fname = user.get("first_name", "")
        try:
            await update.message.copy(chat_id=uid)
            success += 1
        except Forbidden as e:
            blocked_users.append((user, str(e)))
            logger.warning("Broadcast BLOCKED — %s (%s): %s", uname, uid, e)
        except BadRequest as e:
            failed_users.append((user, f"BadRequest: {e}"))
            logger.warning("Broadcast BadRequest — %s (%s): %s", uname, uid, e)
        except TelegramError as e:
            failed_users.append((user, f"TelegramError: {e}"))
            logger.warning("Broadcast TelegramError — %s (%s): %s", uname, uid, e)

        if i % 20 == 0:
            try:
                await progress.edit_text(f"📤 Progress: {i}/{total} ({success} delivered so far)…")
            except TelegramError:
                pass

        await asyncio.sleep(0.05)

    # ── Summary ───────────────────────────────────────────────────────────
    await progress.edit_text(
        f"✅ *Broadcast complete!*\n\n"
        f"👥 Total: {total}\n"
        f"✅ Delivered: {success}\n"
        f"🚫 Blocked bot: {len(blocked_users)}\n"
        f"❌ Other errors: {len(failed_users)}",
        parse_mode="Markdown",
    )

    # ── Blocked detail ────────────────────────────────────────────────────
    if blocked_users:
        await _send_error_report(update, "🚫 *Users who blocked the bot:*", blocked_users)

    # ── Failed detail ─────────────────────────────────────────────────────
    if failed_users:
        await _send_error_report(update, "❌ *Users with delivery errors:*", failed_users)

    adm = await is_admin(ctx.bot, update.effective_user.id)
    await update.message.reply_text("Back to menu:", reply_markup=main_menu_keyboard(adm))
    return ConversationHandler.END


async def _send_error_report(update, header: str, entries: list[tuple[dict, str]]):
    """Chunk and send a list of per-user errors without hitting the 4096 char limit."""
    lines = [header + "\n"]
    for u, err in entries:
        uname = f"@{u['username']}" if u.get("username") else f"id:{u['id']}"
        fname = u.get("first_name", "")
        lines.append(f"• {fname} {uname}\n  _{err}_\n")

    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 4000:
            await update.message.reply_text(chunk, parse_mode="Markdown")
            chunk = line
        else:
            chunk += line
    if chunk:
        await update.message.reply_text(chunk, parse_mode="Markdown")

# ════════════════════════════════════════════════════════════════════════════
# CANCEL
# ════════════════════════════════════════════════════════════════════════════

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    adm = await is_admin(ctx.bot, update.effective_user.id)
    await update.message.reply_text("❌ Cancelled.", reply_markup=main_menu_keyboard(adm))
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    storage.init()

    app = Application.builder().token(BOT_TOKEN).build()

    # ── Conversation: add folder ──────────────────────────────────────────
    folder_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_folder_cb, pattern="^add_folder$")],
        states={
            AWAIT_FOLDER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_folder_name)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start",  start),
            CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
        ],
        allow_reentry=True,
    )

    # ── Conversation: add item ────────────────────────────────────────────
    item_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_item_cb, pattern="^add_item$")],
        states={
            AWAIT_ITEM_FOLDER: [
                CallbackQueryHandler(pick_folder_cb, pattern=r"^pick_folder_.+$")
            ],
            AWAIT_ITEM_CONTENT: [
                MessageHandler(
                    (
                        filters.TEXT
                        | filters.PHOTO
                        | filters.VIDEO
                        | filters.Document.ALL
                        | filters.AUDIO
                        | filters.VOICE
                        | filters.VIDEO_NOTE
                        | filters.Sticker.ALL
                        | filters.ANIMATION
                    ) & ~filters.COMMAND,
                    recv_item_content,
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start",  start),
            CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
        ],
        allow_reentry=True,
    )

    # ── Conversation: broadcast ───────────────────────────────────────────
    broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(broadcast_cb, pattern="^broadcast$")],
        states={
            AWAIT_BROADCAST_MSG: [
                MessageHandler(
                    (
                        filters.TEXT
                        | filters.PHOTO
                        | filters.VIDEO
                        | filters.Document.ALL
                        | filters.AUDIO
                        | filters.VOICE
                        | filters.ANIMATION
                    ) & ~filters.COMMAND,
                    recv_broadcast_msg,
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start",  start),
            CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
        ],
        allow_reentry=True,
    )

    # ── Register all handlers ─────────────────────────────────────────────
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("users",  cmd_users))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(folder_conv)
    app.add_handler(item_conv)
    app.add_handler(broadcast_conv)

    app.add_handler(CallbackQueryHandler(check_membership_cb,      pattern="^check_membership$"))
    app.add_handler(CallbackQueryHandler(main_menu_cb,             pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(browse_cb,                pattern="^browse$"))
    app.add_handler(CallbackQueryHandler(folder_cb,                pattern=r"^folder_.+$"))
    app.add_handler(CallbackQueryHandler(delete_item_cb,           pattern="^delete_item$"))
    app.add_handler(CallbackQueryHandler(confirm_delete_item_cb,   pattern=r"^delitem_.+$"))
    app.add_handler(CallbackQueryHandler(delete_folder_cb,         pattern="^delete_folder$"))
    app.add_handler(CallbackQueryHandler(confirm_delete_folder_cb, pattern=r"^delfolder_.+$"))

    logger.info("Bot is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
