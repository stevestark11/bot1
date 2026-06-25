# 🎟 Coupon / Log Bot v2

A Telegram bot that drops exclusive log/coupon codes to channel members.
Users must join your **private channel** before accessing codes.
Every user who interacts is automatically tracked in `users.json`.
All data is stored in JSON files on a **Railway Volume** — no database needed.

---

## ✨ What's new in v2

| Feature | Details |
|---|---|
| 🔐 Channel-only gate | Only one channel required (no group needed) |
| 👤 Auto user tracking | Every user who interacts is saved to `users.json` automatically |
| 📥 `/users` command | Owner downloads the full `users.json` file on demand |
| 📊 `/stats` command | Owner gets a quick count of users, folders, and logs |
| 👑 Owner ID | Set `OWNER_ID` in env to get exclusive owner commands |
| 📣 Broadcast | Send a message to every tracked user (text, photo, video, document) |

---

## 🚀 Deploy to Railway

### Step 1 — Create your Telegram Bot

1. Open Telegram → search `@BotFather`
2. Send `/newbot` → follow the prompts → copy the **bot token**
3. Send `/setprivacy` → select your bot → set to **Disable**

### Step 2 — Add the bot as Admin to your Channel

- Add the bot to your **private channel** as an Admin
- It needs "Add Members" permission to call `getChatMember`

### Step 3 — Get your Chat ID

Add `@userinfobot` to your channel. It will reply with the numeric ID (e.g. `-1001234567890`).

### Step 4 — Get your private invite link

Channel → Info → Invite Links → Create New Link → copy `https://t.me/+xxxx`
Use a link with **no expiry and no member limit**.

### Step 5 — Get your Owner ID

Send `/start` to `@userinfobot` in a private chat. Copy your numeric user ID.

### Step 6 — Push to GitHub & Deploy to Railway

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USERNAME/coupon-bot.git
git push -u origin main
```

Then on Railway: **New Project → Deploy from GitHub → select repo**.

### Step 7 — Add a Volume (critical)

1. Railway service → **+ Add Volume**
2. Mount Path: `/data`
3. Click Add

### Step 8 — Set environment variables

```
BOT_TOKEN       = your_token_from_botfather
CHANNEL_ID      = -1001234567890
CHANNEL_INVITE  = https://t.me/+xxxxxxxxxxxxxx
OWNER_ID        = 123456789
```

### Step 9 — Deploy

Railway auto-deploys. Check Logs and confirm:

```
Storage ready — coupons: /data/coupons.json | users: /data/users.json
Bot is running…
```

---

## 📱 Commands

| Command | Who | What |
|---|---|---|
| `/start` | Everyone | Opens the bot menu |
| `/cancel` | Everyone | Cancels current action |
| `/users` | Owner only | Sends `users.json` file as document |
| `/stats` | Owner only | Shows user/folder/log counts |

---

## 🗂 How data is stored

**`/data/coupons.json`** — all folders and logs
**`/data/users.json`** — all users who have ever interacted with the bot

Users are tracked with: `id`, `username`, `first_name`, `last_name`, `first_seen`, `last_seen`.

Writes use an **atomic rename** (write `.tmp` → rename) so files are never corrupted.

---

## 👮 Admin Workflow

Admins are auto-detected — anyone who is admin/creator in the channel gets admin buttons.

1. `/start` → extra admin buttons appear
2. **📁 New Folder** → type a name
3. **➕ Add Log** → pick folder → type content
4. **🗑 Delete Log** → pick from list
5. **🗂 Delete Folder** → deletes folder + all logs inside
6. **📣 Broadcast** → send a message to all tracked users

## 👤 User Workflow

1. `/start`
2. Not a member → sees join button (your private invite link)
3. Joins → taps **✅ I've Joined** → re-checked
4. Verified → browses folders → taps folder → sees all logs
