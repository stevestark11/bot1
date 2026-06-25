"""
bot.py — Coupon / Log Telegram Bot (v2)

Changes from original:
  • Only ONE channel required (no group gate)
  • Every user who interacts is auto-saved to users.json
  • Owner command /users → sends the users file as a document
  • Owner command /stats → quick summary of user count + coupon count
  • Broadcast already in original — kept and improved
  • Cleaner membership flow, better error messages
  • OWNER_ID env var identifies the bot owner (gets extra commands)
"""

import os
import asyncio
import logging
from pathlib import Path

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
CHANNEL_ID     = os.environ["CHANNEL_ID"]          # e.g. -1001234567890
CHANNEL_INVITE = os.environ["CHANNEL_INVITE"]      # https://t.me/+xxxxxx
OWNER_ID       = int(os.environ.get("OWNER_ID", 0))  # your Telegram user ID

# ── Conversation states ───────────────────────────────────────────────────────

(
    AWAIT_FOLDER_NAME,
    AWAIT_LOG_FOLDER,
    AWAIT_LOG_CONTENT,
    AWAIT_BROADCAST_MSG,
) = range(4)

# ════════════════════════════════════════════════════════════════════════════
# PERMISSION HELPERS
# ════════════════════════════════════════════════════════════════════════════

def is_owner(user_id: int) -> bool:
    """Returns True if this user is the bot owner."""
    return OWNER_ID and user_id == OWNER_ID


async def is_admin(bot, user_id: int) -> bool:
    """
    Returns True if the user is an admin/creator in the channel,
    OR if they are the owner.
    """
    if is_owner(user_id):
        return True
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ("administrator", "creator")
    except TelegramError:
        return False


async def is_member(bot, user_id: int) -> bool:
    """
    Returns True if the user is a member of the channel
    (not left / kicked / banned).
    """
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
    rows = [[InlineKeyboardButton("📂 Browse Logs", callback_data="browse")]]
    if is_adm:
        rows += [
            [
                InlineKeyboardButton("➕ Add Log",      callback_data="add_log"),
                InlineKeyboardButton("📁 New Folder",   callback_data="add_folder"),
            ],
            [
                InlineKeyboardButton("🗑 Delete Log",    callback_data="delete_log"),
                InlineKeyboardButton("🗂 Delete Folder", callback_data="delete_folder"),
            ],
            [InlineKeyboardButton("📣 Broadcast",       callback_data="broadcast")],
        ]
    return InlineKeyboardMarkup(rows)

# ════════════════════════════════════════════════════════════════════════════
# USER TRACKING DECORATOR  (call on every handler entry)
# ════════════════════════════════════════════════════════════════════════════

async def _track(update: Update) -> None:
    """Silently save the user who triggered this update."""
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
    adm = await is_admin(ctx.bot, user.id)
    mem = adm or await is_member(ctx.bot, user.id)

    if not mem:
        await update.message.reply_text(
            "👋 Welcome!\n\n"
            "You need to join our channel to access the logs.\n"
            "Tap the button below, then press *I've Joined*.",
            parse_mode="Markdown",
            reply_markup=join_keyboard(),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        f"👋 Hey {user.first_name}! Choose an option:",
        reply_markup=main_menu_keyboard(adm),
    )
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
# OWNER-ONLY COMMANDS
# ════════════════════════════════════════════════════════════════════════════

async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /users — Owner only.
    Sends the users.json file as a Telegram document so the owner
    can download the full user list at any time.
    """
    await _track(update)
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ This command is only for the bot owner.")
        return

    users = storage.get_users()
    file_path = storage.users_file_path()

    if not users:
        await update.message.reply_text("No users recorded yet.")
        return

    await update.message.reply_document(
        document=open(file_path, "rb"),
        filename="users.json",
        caption=(
            f"👥 *User List*\n\n"
            f"Total users: *{len(users)}*\n"
            f"File: `users.json`"
        ),
        parse_mode="Markdown",
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /stats — Owner only.
    Quick summary: user count, folder count, coupon count.
    """
    await _track(update)
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ This command is only for the bot owner.")
        return

    users   = storage.get_users()
    folders = storage.get_folders()
    coupons = storage.get_all_coupons()

    await update.message.reply_text(
        f"📊 *Bot Stats*\n\n"
        f"👥 Users tracked: *{len(users)}*\n"
        f"📁 Folders: *{len(folders)}*\n"
        f"🗂 Total logs: *{len(coupons)}*",
        parse_mode="Markdown",
    )

# ════════════════════════════════════════════════════════════════════════════
# CALLBACK — check membership
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
            "❌ You haven't joined the channel yet.\n"
            "Join first, then tap *I've Joined* again.",
            parse_mode="Markdown",
            reply_markup=join_keyboard(),
        )
        return

    await query.edit_message_text(
        f"✅ Welcome! Choose an option:",
        reply_markup=main_menu_keyboard(adm),
    )

# ════════════════════════════════════════════════════════════════════════════
# CALLBACK — back to main menu
# ════════════════════════════════════════════════════════════════════════════

async def main_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query = update.callback_query
    await query.answer()
    adm = await is_admin(ctx.bot, query.from_user.id)
    await query.edit_message_text(
        "Choose an option:",
        reply_markup=main_menu_keyboard(adm),
    )
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
# BROWSE — folder list → coupons
# ════════════════════════════════════════════════════════════════════════════

async def browse_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query = update.callback_query
    await query.answer()
    user = query.from_user
    adm  = await is_admin(ctx.bot, user.id)
    mem  = adm or await is_member(ctx.bot, user.id)

    if not mem:
        await query.edit_message_text(
            "You must join the channel first.",
            reply_markup=join_keyboard(),
        )
        return

    folders = storage.get_folders()
    if not folders:
        await query.edit_message_text(
            "📭 No folders yet. Ask an admin to add some!",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]]
            ),
        )
        return

    buttons = [
        [InlineKeyboardButton(
            f"📁 {f['name']}  ({f['coupon_count']} logs)  •  {f['created_at'][:10]}",
            callback_data=f"folder_{f['id']}",
        )]
        for f in folders
    ]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="main_menu")])

    await query.edit_message_text(
        "📂 Choose a folder:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def folder_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query = update.callback_query
    await query.answer()
    folder_id = query.data.split("_", 1)[1]
    folder = storage.get_folder(folder_id)
    logs   = storage.get_coupons(folder_id)

    if not folder:
        await query.edit_message_text("Folder not found.")
        return

    back_btn = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Folders", callback_data="browse")]]
    )

    if not logs:
        await query.edit_message_text(
            f"📁 *{folder['name']}*\n\nNo logs in this folder yet.",
            parse_mode="Markdown",
            reply_markup=back_btn,
        )
        return

    await query.edit_message_text(
        f"📁 *{folder['name']}* — {len(logs)} log(s):",
        parse_mode="Markdown",
        reply_markup=back_btn,
    )

    for log in logs:
        text = log["code"]
        if log.get("description"):
            text += f"\n\n_{log['description']}_"
        await ctx.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            parse_mode="Markdown",
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
# ADMIN — Add log
# ════════════════════════════════════════════════════════════════════════════

async def add_log_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query = update.callback_query
    if not await is_admin(ctx.bot, query.from_user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    folders = storage.get_folders()
    if not folders:
        await query.edit_message_text(
            "⚠️ No folders exist. Create a folder first.",
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
        "Pick a folder to add the log into:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return AWAIT_LOG_FOLDER


async def pick_folder_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query = update.callback_query
    await query.answer()
    folder_id = query.data.split("_", 2)[2]
    folder    = storage.get_folder(folder_id)
    if not folder:
        await query.edit_message_text("Folder not found.")
        return ConversationHandler.END
    ctx.user_data["log_folder_id"]   = folder_id
    ctx.user_data["log_folder_name"] = folder["name"]
    await query.edit_message_text(
        f"📝 Send the log content to add to *\"{folder['name']}\"*:",
        parse_mode="Markdown",
    )
    return AWAIT_LOG_CONTENT


async def recv_log_content(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    content = update.message.text.strip()
    if not content:
        await update.message.reply_text("Content can't be empty. Send it again:")
        return AWAIT_LOG_CONTENT
    storage.add_coupon(ctx.user_data["log_folder_id"], content, "")
    adm = await is_admin(ctx.bot, update.effective_user.id)
    await update.message.reply_text(
        f"✅ Log saved to *\"{ctx.user_data['log_folder_name']}\"*.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(adm),
    )
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
# ADMIN — Delete log
# ════════════════════════════════════════════════════════════════════════════

async def delete_log_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query = update.callback_query
    if not await is_admin(ctx.bot, query.from_user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    await query.answer()

    logs = storage.get_all_coupons()
    if not logs:
        await query.edit_message_text(
            "No logs to delete.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]]
            ),
        )
        return

    buttons = [
        [InlineKeyboardButton(
            f"🗑 {c['code'][:40]}{'…' if len(c['code']) > 40 else ''}  ({c['folder_name']})",
            callback_data=f"dellog_{c['id']}",
        )]
        for c in logs
    ]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="main_menu")])

    await query.edit_message_text(
        "Select a log to delete:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def confirm_delete_log_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query = update.callback_query
    await query.answer()
    log_id = query.data.split("_", 1)[1]
    storage.delete_coupon(log_id)
    adm = await is_admin(ctx.bot, query.from_user.id)
    await query.edit_message_text(
        "✅ Log deleted.",
        reply_markup=main_menu_keyboard(adm),
    )

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
            f"🗑 {f['name']}  ({f['coupon_count']} logs)",
            callback_data=f"delfolder_{f['id']}",
        )]
        for f in folders
    ]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="main_menu")])

    await query.edit_message_text(
        "Select a folder to delete *(all logs inside will be removed)*:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def confirm_delete_folder_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    query = update.callback_query
    await query.answer()
    folder_id = query.data.split("_", 1)[1]
    folder = storage.get_folder(folder_id)
    name   = folder["name"] if folder else folder_id
    storage.delete_folder(folder_id)
    adm = await is_admin(ctx.bot, query.from_user.id)
    await query.edit_message_text(
        f"✅ Folder *\"{name}\"* and all its logs deleted.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(adm),
    )

# ════════════════════════════════════════════════════════════════════════════
# ADMIN — Broadcast
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
            "⚠️ No users tracked yet. Users are saved automatically when they interact with the bot.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]]
            ),
        )
        return ConversationHandler.END

    await query.edit_message_text(
        f"📣 *Broadcast*\n\n"
        f"Found *{len(users)}* users.\n\n"
        f"Send the message you want to broadcast now.\n"
        f"Supports text, photos, videos, and documents.\n\n"
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

    # Per-failure buckets: list of (user_dict, error_string)
    blocked_users: list[tuple[dict, str]] = []
    failed_users:  list[tuple[dict, str]] = []

    progress = await update.message.reply_text(
        f"📤 Sending to {total} users… please wait."
    )

    for i, user in enumerate(users, 1):
        uid  = user["id"]
        name = user.get("first_name") or ""
        uname = f"@{user['username']}" if user.get("username") else f"id:{uid}"

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

        # Update progress every 20 users so the admin sees it moving
        if i % 20 == 0:
            try:
                await progress.edit_text(
                    f"📤 Progress: {i}/{total} sent… ({success} delivered so far)"
                )
            except TelegramError:
                pass

        await asyncio.sleep(0.05)  # ~20 msg/sec — within Telegram rate limits

    # ── Summary message ───────────────────────────────────────────────────
    summary_lines = [
        f"✅ *Broadcast complete!*\n",
        f"👥 Total users: {total}",
        f"✅ Delivered: {success}",
        f"🚫 Blocked bot: {len(blocked_users)}",
        f"❌ Other errors: {len(failed_users)}",
    ]
    await progress.edit_text("\n".join(summary_lines), parse_mode="Markdown")

    # ── Blocked users detail ──────────────────────────────────────────────
    if blocked_users:
        lines = ["🚫 *Users who blocked the bot:*\n"]
        for u, err in blocked_users:
            uname = f"@{u['username']}" if u.get("username") else f"id:{u['id']}"
            fname = u.get("first_name", "")
            lines.append(f"• {fname} {uname}\n  _{err}_")
        # Telegram message limit is 4096 chars — chunk if needed
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 4000:
                await update.message.reply_text(chunk, parse_mode="Markdown")
                chunk = line + "\n"
            else:
                chunk += line + "\n"
        if chunk:
            await update.message.reply_text(chunk, parse_mode="Markdown")

    # ── Failed users detail ───────────────────────────────────────────────
    if failed_users:
        lines = ["❌ *Users with delivery errors:*\n"]
        for u, err in failed_users:
            uname = f"@{u['username']}" if u.get("username") else f"id:{u['id']}"
            fname = u.get("first_name", "")
            lines.append(f"• {fname} {uname}\n  _{err}_")
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 4000:
                await update.message.reply_text(chunk, parse_mode="Markdown")
                chunk = line + "\n"
            else:
                chunk += line + "\n"
        if chunk:
            await update.message.reply_text(chunk, parse_mode="Markdown")

    adm = await is_admin(ctx.bot, update.effective_user.id)
    await update.message.reply_text("Back to menu:", reply_markup=main_menu_keyboard(adm))
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
# CANCEL
# ════════════════════════════════════════════════════════════════════════════

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _track(update)
    adm = await is_admin(ctx.bot, update.effective_user.id)
    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=main_menu_keyboard(adm),
    )
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

    # ── Conversation: add log ─────────────────────────────────────────────
    log_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_log_cb, pattern="^add_log$")],
        states={
            AWAIT_LOG_FOLDER: [
                CallbackQueryHandler(pick_folder_cb, pattern=r"^pick_folder_.+$")
            ],
            AWAIT_LOG_CONTENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_log_content)
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
                    (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL)
                    & ~filters.COMMAND,
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

    # ── Register handlers ─────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("users",  cmd_users))   # owner only
    app.add_handler(CommandHandler("stats",  cmd_stats))   # owner only
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(folder_conv)
    app.add_handler(log_conv)
    app.add_handler(broadcast_conv)

    app.add_handler(CallbackQueryHandler(check_membership_cb,     pattern="^check_membership$"))
    app.add_handler(CallbackQueryHandler(main_menu_cb,            pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(browse_cb,               pattern="^browse$"))
    app.add_handler(CallbackQueryHandler(folder_cb,               pattern=r"^folder_.+$"))
    app.add_handler(CallbackQueryHandler(delete_log_cb,           pattern="^delete_log$"))
    app.add_handler(CallbackQueryHandler(confirm_delete_log_cb,   pattern=r"^dellog_.+$"))
    app.add_handler(CallbackQueryHandler(delete_folder_cb,        pattern="^delete_folder$"))
    app.add_handler(CallbackQueryHandler(confirm_delete_folder_cb,pattern=r"^delfolder_.+$"))

    logger.info("Bot is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
