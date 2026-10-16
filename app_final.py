"""
سیستم چندین ربات تلگرامی با پنل شیشه‌ای توضیحی
نسخه با تفکیک !help (ساده) و !panel (شیشه‌ای)
"""
import asyncio
import html
import os
import sys
import time
import random
import socket
import traceback
import re
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TimedOut, NetworkError
try:
    # FloodWait exists in some Telegram library versions, but newer versions
    # expose the same rate-limit behavior through RetryAfter.
    from telegram.error import FloodWait
except ImportError:
    FloodWait = RetryAfter
from telegram.ext import Application, ContextTypes, MessageHandler, filters, CallbackQueryHandler
import aiosqlite

MAIN_BOT_TOKEN = (os.getenv("MAIN_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
MAIN_ADMIN_ID = 8801803105
DEFAULT_COMMAND_PREFIX = "!"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "bots.db")
START_TIME = time.time()
BACKUP_INTERVAL_SECONDS = 3600
BACKUP_RETENTION_SECONDS = 3600
BACKUP_PREFIX = "full_backup_"
backup_task = None
backup_current_path = None

socket.setdefaulttimeout(60)


SMALL_CAPS_MAP = str.maketrans({
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ꜰ",
    "g": "ɢ", "h": "ʜ", "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ",
    "m": "ᴍ", "n": "ɴ", "o": "ᴏ", "p": "ᴘ", "q": "ǫ", "r": "ʀ",
    "s": "ꜱ", "t": "ᴛ", "u": "ᴜ", "v": "ᴠ", "w": "ᴡ", "x": "x",
    "y": "ʏ", "z": "ᴢ",
    "A": "ᴀ", "B": "ʙ", "C": "ᴄ", "D": "ᴅ", "E": "ᴇ", "F": "ꜰ",
    "G": "ɢ", "H": "ʜ", "I": "ɪ", "J": "ᴊ", "K": "ᴋ", "L": "ʟ",
    "M": "ᴍ", "N": "ɴ", "O": "ᴏ", "P": "ᴘ", "Q": "ǫ", "R": "ʀ",
    "S": "ꜱ", "T": "ᴛ", "U": "ᴜ", "V": "ᴠ", "W": "ᴡ", "X": "x",
    "Y": "ʏ", "Z": "ᴢ",
})

def small_caps(text: str) -> str:
    """Decorative small-cap text that needs no Telegram Markdown parsing."""
    return str(text).translate(SMALL_CAPS_MAP)

def _box_title(text: str, width: int = 29) -> str:
    """Center a title inside the fixed-width Unicode box."""
    title = str(text)[:width]
    left = max(0, (width - len(title)) // 2)
    right = max(0, width - len(title) - left)
    return (" " * left) + title + (" " * right)

_PROTECTED_REPLY_TOKEN_RE = re.compile(
    r"![A-Za-z][A-Za-z0-9_]*|https?://\S+|@[A-Za-z0-9_]{3,}|<[^>\n]+>|`[^`\n]*`|\b\d{5,}:[A-Za-z0-9_-]{20,}\b"
)


def _small_caps_preserving_literals(text: str) -> str:
    """Style English prose while keeping copyable commands, IDs and links intact."""
    protected = []

    def hold(match):
        protected.append(match.group(0))
        return f"\uFFF0{len(protected) - 1}\uFFF1"

    converted = small_caps(_PROTECTED_REPLY_TOKEN_RE.sub(hold, str(text)))
    for index, literal in enumerate(protected):
        converted = converted.replace(f"\uFFF0{index}\uFFF1", literal)
    return converted


def format_bot_reply(text: str) -> str:
    """Give command replies a consistent plain-text frame without breaking commands."""
    if not isinstance(text, str) or not text.strip():
        return text

    # Avoid adding extra borders to messages that already have the ping/help style.
    if text.startswith("╭─────────────────────────────╮"):
        return "\n".join(_small_caps_preserving_literals(line) for line in text.splitlines())

    styled_lines = []
    for line in text.splitlines() or [text]:
        if not line.strip():
            styled_lines.append("")
        else:
            styled_lines.append("╰┈➤ " + _small_caps_preserving_literals(line))
    body = "\n".join(styled_lines)

    # Telegram text messages have a length limit; don't add decorative overhead
    # when the original response is already long.
    if len(body) > 3600:
        return _small_caps_preserving_literals(text)

    title = _box_title("ʙᴏᴛ ʀᴇꜱᴘᴏɴꜱᴇ")
    framed = f"╭─────────────────────────────╮\n│{title}│\n╰─────────────────────────────╯\n{body}"
    return framed if len(framed) < 4000 else _small_caps_preserving_literals(text)


async def reply_styled(message, text, *args, **kwargs):
    """Send a consistently styled bot response; keep explicit parse modes intact."""
    if isinstance(text, str) and kwargs.get("parse_mode") is None:
        text = format_bot_reply(text)
        kwargs["parse_mode"] = None
    return await message.reply_text(text, *args, **kwargs)


async def edit_styled(message, text, *args, **kwargs):
    """Apply the same response style when an inline-panel message is edited."""
    if isinstance(text, str) and kwargs.get("parse_mode") is None:
        text = format_bot_reply(text)
        kwargs["parse_mode"] = None
    return await message.edit_text(text, *args, **kwargs)


def validate_configuration():
    missing = []
    if not MAIN_BOT_TOKEN:
        missing.append("MAIN_BOT_TOKEN")
    if MAIN_ADMIN_ID <= 0:
        missing.append("MAIN_ADMIN_ID")
    if missing:
        raise RuntimeError("Missing required configs: " + ", ".join(missing))

def get_uptime():
    uptime_seconds = int(time.time() - START_TIME)
    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    minutes = (uptime_seconds % 3600) // 60
    return f"{days}d {hours}h {minutes}m"

COMMANDS_DESC = {
    "ping": {
        "short": "🏓 بررسی وضعیت بات",
        "full": "📌 **!ping**\nبررسی وضعیت بات و نمایش:\n• نام بات\n• زمان تأخیر (Latency)\n• زمان آنلاین بودن (Uptime)"
    },
    "help": {
        "short": "📋 پنل ساده",
        "full": "📌 **!help**\nنمایش پنل ساده دو ستونی با دستورات (بدون توضیحات اضافی)."
    },
    "panel": {
        "short": "📱 پنل شیشه‌ای",
        "full": "📌 **!panel**\nنمایش پنل شیشه‌ای تعاملی با تمام دستورات و توضیحات کامل آنها."
    },
    "token": {
        "short": "➕ افزودن بات",
        "full": "📌 **!token [توکن]**\nافزودن یک بات جدید به سیستم با توکن داده‌شده.\nمثال: `!token 123456:ABC...`"
    },
    "deltoken": {
        "short": "➖ حذف بات",
        "full": "📌 **!deltoken [توکن]**\nحذف یک بات از سیستم (به جز بات اصلی).\nمثال: `!deltoken 123456:ABC...`"
    },
    "cleartoken": {
        "short": "🗑️ پاک کردن بات‌ها",
        "full": "📌 **!cleartoken**\nحذف تمام بات‌های اضافی (بات اصلی حفظ می‌شود)."
    },
    "admin": {
        "short": "👑 افزودن ادمین",
        "full": "📌 **!admin [آیدی]** یا ریپلای روی پیام کاربر\nافزودن کاربر به عنوان ادمین برای مدیریت بات‌ها.\nمثال: `!admin 123456789`"
    },
    "listadmin": {
        "short": "📋 لیست ادمین‌ها",
        "full": "📌 **!listadmin**\nنمایش لیست تمام ادمین‌های فعال."
    },
    "deladmin": {
        "short": "❌ حذف ادمین",
        "full": "📌 **!deladmin [آیدی]** یا ریپلای روی پیام ادمین\nحذف یک کاربر از لیست ادمین‌ها.\nمثال: `!deladmin 123456789`"
    },
    "clearadmin": {
        "short": "🗑️ پاک کردن ادمین‌ها",
        "full": "📌 **!clearadmin**\nحذف تمام ادمین‌ها از سیستم."
    },
    "addfosh": {
        "short": "📝 افزودن پیام به صف",
        "full": "📌 **!addfosh [متن]** یا ریپلای روی پیام\nافزودن پیام به صف ارسال.\n• با ریپلای: متن پیام ریپلای شده به صف اضافه می‌شود.\n• با نوشتن متن: هر خط به عنوان یک پیام جداگانه اضافه می‌شود."
    },
    "addfoshtext": {
        "short": "📝 افزودن متن چندخطی به‌صورت یک پیام",
        "full": "📌 **!addfoshtext [متن]** یا ریپلای روی پیام\nافزودن یک متن چندخطی به صف به‌عنوان **یک فحش/یک پیام واحد**.\nبرخلاف **!addfosh**، خط‌های جدید جداگانه ثبت نمی‌شوند و همه متن در یک آیتم ذخیره می‌شود."
    },
    "delfosh": {
        "short": "❌ حذف پیام از صف",
        "full": "📌 **!delfosh [شماره]**\nحذف یک پیام از صف بر اساس شماره index.\nبرای مشاهده شماره‌ها از !listfosh استفاده کنید."
    },
    "listfosh": {
        "short": "📋 نمایش صف",
        "full": "📌 **!listfosh**\nنمایش لیست کامل پیام‌های موجود در صف به همراه شماره index."
    },
    "clearfosh": {
        "short": "🗑️ پاک کردن صف",
        "full": "📌 **!clearfosh**\nحذف تمام پیام‌های موجود در صف."
    },
    "gpid": {
        "short": "🆔 آیدی گروه",
        "full": "📌 **!gpid**\nنمایش آیدی عددی گروه فعلی."
    },
    "setgp": {
        "short": "🎯 تنظیم گروه هدف",
        "full": "📌 **!setgp [آیدی گروه]**\nتنظیم گروهی که پیام‌ها به آن ارسال شوند.\nاگر آیدی وارد نشود، همان گروه فعلی تنظیم می‌شود."
    },
    "settime": {
        "short": "⏱️ تنظیم فاصله ارسال",
        "full": "📌 **!settime [ثانیه]**\nتنظیم فاصله زمانی بین ارسال هر پیام (حداقل ۰.۵ ثانیه).\nمثال: `!settime 2` (هر ۲ ثانیه یک پیام)"
    },
    "setid": {
        "short": "🏷️ افزودن تگ",
        "full": "📌 **!setid [آیدی]** یا ریپلای روی پیام کاربر\nافزودن کاربر به لیست تگ‌ها. هنگام ارسال، نام کاربر با علامت تگ منشن می‌شود."
    },
    "delid": {
        "short": "❌ حذف تگ",
        "full": "📌 **!delid [آیدی]** یا ریپلای روی پیام کاربر\nحذف یک کاربر از لیست تگ‌ها."
    },
    "clearid": {
        "short": "🗑️ پاک کردن تگ‌ها",
        "full": "📌 **!clearid**\nحذف تمام کاربران از لیست تگ‌ها."
    },
    "symbol": {
        "short": "🔣 تغییر علامت تگ",
        "full": "📌 **!symbol [علامت]**\nتغییر علامت نمایشی برای تگ‌ها (پیش‌فرض: 𒀽).\nمثال: `!symbol ⚡`"
    },
    "bankai": {
        "short": "▶️ شروع ارسال",
        "full": "📌 **!bankai**\nشروع ارسال پیام‌های موجود در صف به گروه هدف.\nقبل از استفاده مطمئن شوید صف خالی نیست و تنظیمات گروه انجام شده است."
    },
    "satk": {
        "short": "⏹️ توقف ارسال",
        "full": "📌 **!satk**\nتوقف ارسال پیام‌ها (متوقف کردن FOSH)."
    },
    "status": {
        "short": "📊 وضعیت سیستم",
        "full": "📌 **!status**\nنمایش وضعیت کامل:\n• وضعیت ارسال (در حال اجرا/متوقف)\n• تعداد پیام‌های صف\n• فاصله ارسال\n• وضعیت Round Robin\n• زمان آنلاین بودن\n• آیدی گروه"
    },
    "info": {
        "short": "📈 آمار منشن‌ها",
        "full": "📌 **!info**\nنمایش آمار تعداد دفعات منشن شدن هر کاربر در همین گروه و همین ریموت.\nفقط منشن‌هایی که توسط ارسال‌های FOSH ثبت شده‌اند در آمار حساب می‌شوند."
    },
    "lockreply": {
        "short": "🔒 تنظیم هدف ریپلای",
        "full": "📌 **!lockreply** (با ریپلای روی پیام)\nتنظیم یک پیام به عنوان هدف ریپلای برای تمام پیام‌های صف.\nهمه پیام‌ها به‌صورت ریپلای به آن پیام ارسال می‌شوند."
    },
    "setreply": {
        "short": "↩️ فعال‌سازی Auto Reply",
        "full": "📌 **!setreply** (با ریپلای روی پیام کاربر)\nکاربر را برای ریپلای خودکار فعال می‌کند.\nهر وقت همان کاربر در همان گروه پیام متنی بفرستد، یکی از پیام‌های اضافه‌شده با **!addfosh** به همان پیام ریپلای می‌شود.\nاگر صف !addfosh خالی باشد، هیچ چیزی ارسال نمی‌شود."
    },
    "clearreply": {
        "short": "🚫 پاک کردن Auto Reply",
        "full": "📌 **!clearreply**\nتمام کاربران ثبت‌شده در Auto Reply همین بات را پاک می‌کند و Auto Reply را خاموش می‌کند."
    },
    "clearlockreply": {
        "short": "🔓 حذف قفل ریپلای",
        "full": "📌 **!clearlockreply**\nهدف !lockreply را برای همین گروه و همین ربات پاک می‌کند.\nبعد از اجرا، پیام‌های صف دیگر به پیام قفل‌شده ریپلای نمی‌شوند."
    },
    "setname": {
        "short": "✏️ تغییر نام بات",
        "full": "📌 **!setname [نام جدید]**\nتغییر نام نمایشی بات.\nمثال: `!setname MyBot`"
    },
    "setbio": {
        "short": "✏️ تغییر بیوگرافی",
        "full": "📌 **!setbio [متن جدید]**\nتغییر بیوگرافی (description) بات.\nمثال: `!setbio This is my bot`"
    },
    "setshort": {
        "short": "✏️ تغییر توضیح کوتاه",
        "full": "📌 **!setshort [متن کوتاه]**\nتغییر short description بات.\nحداکثر ۱۲۰ کاراکتر."
    },
    "setphoto": {
        "short": "🖼️ تغییر عکس پروفایل",
        "full": "📌 **!setphoto**\nروی یک عکس ریپلای کنید و این دستور را بفرستید تا عکس پروفایل همان ریموت تغییر کند."
    },
    "clearphoto": {
        "short": "🗑️ پاک کردن عکس پروفایل",
        "full": "📌 **!clearphoto**\nعکس پروفایل تمام بات‌های مدیریت‌شده را پاک می‌کند."
    },
    "squtalon": {
        "short": "🔄 فعال‌سازی نوبت‌دهی",
        "full": "📌 **!squtalon**\nفعال‌سازی حالت Round Robin (ارسال نوبتی بین بات‌ها).\nهر بات به نوبت پیام‌های خود را ارسال می‌کند.\nحداقل دو بات در گروه نیاز است."
    },
    "squtalof": {
        "short": "⏹️ غیرفعال‌سازی نوبت‌دهی",
        "full": "📌 **!squtalof**\nغیرفعال‌سازی حالت Round Robin.\nبات‌ها به‌صورت مستقل ارسال می‌کنند."
    },
    "backup": {
        "short": "💾 Snapshot کامل ریموت‌ها",
        "full": "📌 **!backup**\nSnapshot وضعیت همان لحظهٔ همه ریموت‌ها را به پیوی Owner اصلی می‌فرستد: توکن/Owner/ادمین، لیست فحش‌ها، گروه مقصد، زمان ارسال، تگ‌ها، Auto Reply، Round Robin و وضعیت FOSH.\n❌ سورس و فایل‌های پروژه داخل بکاپ نیستند.\nریپلای روی همین بکاپ + `!backup` فقط همین وضعیت ریموت‌ها را جایگزین و دوباره اجرا می‌کند."
    }
}

def _fancy_command(name: str) -> str:
    """Convert ASCII command labels to mathematical bold Unicode for Telegram buttons."""
    normal = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    bold = "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘂𝘃𝘄𝘅𝘆𝘇𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟕𝟴𝟵"
    return name.translate(str.maketrans(normal, bold))


COMMAND_ORDER = [
    "ping", "help", "token", "deltoken",
    "cleartoken", "admin", "listadmin", "deladmin",
    "clearadmin", "addfosh", "addfoshtext", "delfosh", "listfosh",
    "clearfosh", "gpid", "setgp", "settime",
    "setid", "delid", "clearid", "symbol",
    "bankai", "satk", "status", "info", "lockreply", "clearlockreply", "setreply", "clearreply",
    "setname", "setbio", "setshort", "setphoto", "clearphoto", "squtalon", "squtalof",
    "backup", "panel"
]


def get_glass_panel(include_remote_admin_limits: bool = False) -> InlineKeyboardMarkup:
    """Two-column English command panel. Clicking a command shows its full description."""
    keyboard = []
    row = []

    for cmd in COMMAND_ORDER:
        row.append(
            InlineKeyboardButton(
                _fancy_command(cmd.upper()),
                callback_data=f"desc_{cmd}"
            )
        )
        if len(row) == 2:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    if include_remote_admin_limits:
        keyboard.append([
            InlineKeyboardButton(
                "👑 تنظیم سقف ادمین ریموت‌ها",
                callback_data="panel_admin_limits"
            )
        ])

    return InlineKeyboardMarkup(keyboard)


def get_simple_panel() -> InlineKeyboardMarkup:
    """Two-column help panel. One tap copies the command directly to Telegram clipboard."""
    keyboard = []
    row = []

    for cmd in COMMAND_ORDER:
        command_text = f"!{cmd}"
        row.append(
            InlineKeyboardButton(
                _fancy_command(cmd.upper()),
                copy_text=CopyTextButton(text=command_text)
            )
        )
        if len(row) == 2:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    return InlineKeyboardMarkup(keyboard)

async def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS bots (
            bot_id INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL, username TEXT,
            is_active INTEGER DEFAULT 1, command_prefix TEXT DEFAULT '!'
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS admins (
            admin_id INTEGER, bot_id INTEGER,
            PRIMARY KEY (admin_id, bot_id),
            FOREIGN KEY (bot_id) REFERENCES bots(bot_id)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS group_settings (
            group_id INTEGER, bot_id INTEGER, target_group_id INTEGER,
            send_interval REAL DEFAULT 1.0, tag_symbol TEXT DEFAULT '𒀽',
            reply_target_msg_id INTEGER DEFAULT NULL,
            reply_target_user_id INTEGER DEFAULT NULL,
            reply_target_chat_id INTEGER DEFAULT NULL,
            round_robin_enabled INTEGER DEFAULT 0,
            PRIMARY KEY (group_id, bot_id),
            FOREIGN KEY (bot_id) REFERENCES bots(bot_id)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS tag_ids (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, bot_id INTEGER, group_id INTEGER,
            FOREIGN KEY (bot_id) REFERENCES bots(bot_id)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS mention_counts (
            user_id INTEGER, bot_id INTEGER, group_id INTEGER, count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, bot_id, group_id),
            FOREIGN KEY (bot_id) REFERENCES bots(bot_id)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS queued_messages (
            msg_id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, bot_id INTEGER,
            content TEXT, order_index INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reply_to_msg_id INTEGER DEFAULT NULL,
            FOREIGN KEY (bot_id) REFERENCES bots(bot_id)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS auto_reply_settings (
            bot_id INTEGER PRIMARY KEY, is_enabled INTEGER DEFAULT 0,
            scope_type TEXT DEFAULT 'group', scope_group_id INTEGER,
            FOREIGN KEY (bot_id) REFERENCES bots(bot_id)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS remote_owners (
            bot_id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL,
            expires_at TEXT DEFAULT NULL, max_bots INTEGER DEFAULT 1,
            admin_limit INTEGER DEFAULT 5,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (bot_id) REFERENCES bots(bot_id)
        )""")
        # Migrate existing databases without overwriting any configured limits.
        cur = await db.execute("PRAGMA table_info(remote_owners)")
        remote_owner_columns = [row[1] for row in await cur.fetchall()]
        if "admin_limit" not in remote_owner_columns:
            await db.execute(
                "ALTER TABLE remote_owners ADD COLUMN admin_limit INTEGER DEFAULT 5"
            )
        await db.execute("""CREATE TABLE IF NOT EXISTS remote_bot_links (
            bot_id INTEGER PRIMARY KEY,
            root_bot_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (bot_id) REFERENCES bots(bot_id)
        )""")
        await db.execute("""
            INSERT OR IGNORE INTO remote_bot_links (bot_id, root_bot_id, owner_id)
            SELECT bot_id, bot_id, owner_id FROM remote_owners
        """)
        await db.execute("""CREATE TABLE IF NOT EXISTS remote_creation_sessions (
            admin_id INTEGER PRIMARY KEY, target_user_id INTEGER DEFAULT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS auto_reply_users (
            user_id INTEGER, bot_id INTEGER,
            PRIMARY KEY (user_id, bot_id),
            FOREIGN KEY (bot_id) REFERENCES bots(bot_id)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS auto_reply_texts (
            text_id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, bot_id INTEGER,
            FOREIGN KEY (bot_id) REFERENCES bots(bot_id)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS reply_message_cache (
            cache_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL, bot_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL, text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, bot_id, message_id),
            FOREIGN KEY (bot_id) REFERENCES bots(bot_id)
        )""")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_reply_cache_lookup "
            "ON reply_message_cache (chat_id, bot_id, user_id, created_at DESC)"
        )
        await db.execute("""CREATE TABLE IF NOT EXISTS fosh_status (
            group_id INTEGER, bot_id INTEGER, is_sending INTEGER DEFAULT 0,
            PRIMARY KEY (group_id, bot_id)
        )""")
        cur = await db.execute("PRAGMA table_info(round_robin_state)")
        rr_columns = [row[1] for row in await cur.fetchall()]
        if rr_columns and "scope_root_id" not in rr_columns:
            await db.execute("DROP TABLE round_robin_state")

        await db.execute("""CREATE TABLE IF NOT EXISTS round_robin_state (
            group_id INTEGER,
            scope_root_id INTEGER,
            last_bot_id INTEGER,
            last_send_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (group_id, scope_root_id)
        )""")
        try:
            await db.execute("ALTER TABLE queued_messages ADD COLUMN reply_to_msg_id INTEGER DEFAULT NULL")
        except:
            pass
        try:
            await db.execute("ALTER TABLE group_settings ADD COLUMN reply_target_msg_id INTEGER DEFAULT NULL")
        except:
            pass
        try:
            await db.execute("ALTER TABLE group_settings ADD COLUMN reply_target_user_id INTEGER DEFAULT NULL")
        except:
            pass
        try:
            await db.execute("ALTER TABLE group_settings ADD COLUMN reply_target_chat_id INTEGER DEFAULT NULL")
        except:
            pass
        try:
            await db.execute("ALTER TABLE group_settings ADD COLUMN round_robin_enabled INTEGER DEFAULT 0")
        except:
            pass
        # Indexes used in hot sender/scheduler paths.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_queue_group_bot_order "
            "ON queued_messages (group_id, bot_id, order_index)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fosh_status_group_state "
            "ON fosh_status (group_id, is_sending, bot_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_group_settings_group_bot "
            "ON group_settings (group_id, bot_id)"
        )
        await db.commit()

async def add_bot(token, username=None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO bots (token, username) VALUES (?, ?)",
            (token, username)
        )
        await db.commit()
        if cur.lastrowid:
            return cur.lastrowid

        cur = await db.execute(
            "SELECT bot_id FROM bots WHERE token = ? LIMIT 1",
            (token,)
        )
        row = await cur.fetchone()
        return row[0] if row else None

async def get_bot_by_token(token):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM bots WHERE token = ?", (token,))
        row = await cur.fetchone()
        if row:
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        return None

async def get_remote_scope(bot_id):
    """Return the isolated owner/root scope for a remote bot."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT bot_id, root_bot_id, owner_id FROM remote_bot_links WHERE bot_id = ? LIMIT 1",
            (bot_id,)
        )
        row = await cur.fetchone()
        if row:
            return {"bot_id": row[0], "root_bot_id": row[1], "owner_id": row[2]}
        cur = await db.execute(
            "SELECT bot_id, owner_id FROM remote_owners WHERE bot_id = ? LIMIT 1",
            (bot_id,)
        )
        row = await cur.fetchone()
        if row:
            return {"bot_id": row[0], "root_bot_id": row[0], "owner_id": row[1]}
        return None


async def link_remote_bot(bot_id, root_bot_id, owner_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO remote_bot_links (bot_id, root_bot_id, owner_id) VALUES (?, ?, ?)",
            (bot_id, root_bot_id, owner_id)
        )
        await db.commit()


async def get_remote_root_id(bot_id):
    """Return the root namespace for a bot. Main Bot is its own namespace."""
    scope = await get_remote_scope(bot_id)
    return scope["root_bot_id"] if scope else bot_id


async def count_remote_bots(root_bot_id):
    """Count child bots only; the remote manager/root is not in the quota."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """SELECT COUNT(*)
               FROM remote_bot_links
               WHERE root_bot_id = ? AND bot_id != ?""",
            (root_bot_id, root_bot_id)
        )
        row = await cur.fetchone()
        return int(row[0] or 0)


async def get_remote_bot_ids(root_bot_id, include_root=True):
    """Return every bot in a remote namespace.

    The root bot is included explicitly because older databases may not have
    a self-link in remote_bot_links. This keeps namespace-wide settings such
    as !settime consistent for the root and every child remote.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        if include_root:
            cur = await db.execute(
                """SELECT bot_id FROM remote_bot_links WHERE root_bot_id = ?
                   UNION SELECT ? AS bot_id ORDER BY bot_id""",
                (root_bot_id, root_bot_id)
            )
        else:
            cur = await db.execute(
                "SELECT bot_id FROM remote_bot_links WHERE root_bot_id = ? AND bot_id != ? ORDER BY bot_id",
                (root_bot_id, root_bot_id)
            )
        return [row[0] for row in await cur.fetchall()]


async def get_all_bots():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """SELECT b.*
               FROM bots b
               WHERE b.is_active = 1
                 AND (
                     b.token = ?
                     OR NOT EXISTS (
                         SELECT 1
                         FROM remote_bot_links l
                         WHERE l.bot_id = b.bot_id
                     )
                     OR EXISTS (
                         SELECT 1
                         FROM remote_bot_links l
                         JOIN bots root ON root.bot_id = l.root_bot_id
                         WHERE l.bot_id = b.bot_id
                           AND root.is_active = 1
                     )
                 )""",
            (MAIN_BOT_TOKEN,)
        )
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in rows]


async def set_remote_scope_active(root_bot_id, is_active):
    """Synchronize lifecycle state for a remote root and all of its children."""
    state = 1 if is_active else 0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE bots
               SET is_active = ?
               WHERE bot_id IN (
                   SELECT bot_id
                   FROM remote_bot_links
                   WHERE root_bot_id = ?
               )""",
            (state, root_bot_id)
        )
        await db.execute(
            """UPDATE bots
               SET is_active = ?
               WHERE bot_id = ?
                 AND token != ?""",
            (state, root_bot_id, MAIN_BOT_TOKEN)
        )
        await db.commit()


async def get_remote_scope_state(root_bot_id):
    """Return whether the root is active; missing roots are treated as inactive."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT is_active FROM bots WHERE bot_id = ? LIMIT 1",
            (root_bot_id,)
        )
        row = await cur.fetchone()
        return bool(row and row[0])

async def delete_bot_by_token(token):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT bot_id FROM bots WHERE token = ? LIMIT 1", (token,))
        row = await cur.fetchone()
        if row:
            await db.execute("DELETE FROM remote_bot_links WHERE bot_id = ?", (row[0],))
            await db.execute("DELETE FROM remote_owners WHERE bot_id = ?", (row[0],))
        await db.execute("DELETE FROM bots WHERE token = ?", (token,))
        await db.commit()

async def clear_all_tokens_except_main(main_token):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM remote_bot_links")
        await db.execute("DELETE FROM remote_owners")
        await db.execute("DELETE FROM bots WHERE token != ?", (main_token,))
        await db.execute("UPDATE bots SET command_prefix = '!' WHERE command_prefix IS NOT NULL")
        await db.commit()

async def update_bot_prefix(bot_id, prefix):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE bots SET command_prefix = ? WHERE bot_id = ?", (prefix, bot_id))
        await db.commit()

async def get_bot_prefix(bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT command_prefix FROM bots WHERE bot_id = ?", (bot_id,))
        row = await cur.fetchone()
        return row[0] if row else "!"

async def add_admin(admin_id, bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO admins (admin_id, bot_id) VALUES (?, ?)", (admin_id, bot_id))
        await db.commit()

async def get_global_admins():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT DISTINCT admin_id FROM admins")
        return [row[0] for row in await cur.fetchall()]

async def add_global_admin(admin_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO admins (admin_id, bot_id) "
            "SELECT ?, bot_id FROM bots WHERE is_active = 1",
            (admin_id,),
        )
        await db.commit()

async def delete_global_admin(admin_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE admin_id = ?", (admin_id,))
        await db.commit()

async def clear_global_admins():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins")
        await db.commit()

async def get_group_settings(group_id, bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM group_settings WHERE group_id = ? AND bot_id = ?", (group_id, bot_id))
        row = await cur.fetchone()
        if row:
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        return None

async def create_or_update_group_settings(group_id, bot_id, **kwargs):
    async with aiosqlite.connect(DB_PATH) as db:
        existing = await get_group_settings(group_id, bot_id)
        if existing:
            set_clause = ", ".join([f"{k} = ?" for k in kwargs])
            values = list(kwargs.values()) + [group_id, bot_id]
            await db.execute(f"UPDATE group_settings SET {set_clause} WHERE group_id = ? AND bot_id = ?", values)
        else:
            cols = ", ".join(["group_id", "bot_id"] + list(kwargs.keys()))
            phs = ", ".join(["?"] * (2 + len(kwargs)))
            vals = [group_id, bot_id] + list(kwargs.values())
            await db.execute(f"INSERT INTO group_settings ({cols}) VALUES ({phs})", vals)
        await db.commit()

async def add_tag_id(user_id, bot_id, group_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO tag_ids (user_id, bot_id, group_id) VALUES (?, ?, ?)", (user_id, bot_id, group_id))
        await db.commit()

async def get_tag_ids(bot_id, group_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM tag_ids WHERE bot_id = ? AND group_id = ?", (bot_id, group_id))
        return [row[0] for row in await cur.fetchall()]

async def increment_mention_counts(user_ids, bot_id, group_id):
    if not user_ids:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        for user_id in user_ids:
            await db.execute(
                "INSERT INTO mention_counts (user_id, bot_id, group_id, count) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(user_id, bot_id, group_id) DO UPDATE SET count = count + 1",
                (user_id, bot_id, group_id)
            )
        await db.commit()

async def get_mention_counts(bot_id, group_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, count FROM mention_counts WHERE bot_id = ? AND group_id = ? ORDER BY count DESC, user_id ASC",
            (bot_id, group_id)
        )
        return await cur.fetchall()

async def delete_tag_id(user_id, bot_id, group_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tag_ids WHERE user_id = ? AND bot_id = ? AND group_id = ?", (user_id, bot_id, group_id))
        await db.execute("DELETE FROM mention_counts WHERE user_id = ? AND bot_id = ? AND group_id = ?", (user_id, bot_id, group_id))
        await db.commit()

async def clear_tag_ids(bot_id, group_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tag_ids WHERE bot_id = ? AND group_id = ?", (bot_id, group_id))
        await db.execute("DELETE FROM mention_counts WHERE bot_id = ? AND group_id = ?", (bot_id, group_id))
        await db.commit()

async def add_queued_message(content, group_id, bot_id, reply_to_msg_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COALESCE(MAX(order_index), 0) + 1 FROM queued_messages WHERE group_id = ? AND bot_id = ?", (group_id, bot_id))
        row = await cur.fetchone()
        order_idx = row[0] if row else 1
        cur = await db.execute(
            "INSERT INTO queued_messages (content, group_id, bot_id, order_index, reply_to_msg_id) VALUES (?, ?, ?, ?, ?)",
            (content, group_id, bot_id, order_idx, reply_to_msg_id)
        )
        await db.commit()
        return cur.lastrowid

async def get_queued_messages(group_id, bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM queued_messages WHERE group_id = ? AND bot_id = ? ORDER BY order_index", (group_id, bot_id))
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in rows]

async def delete_queued_message_by_index(order_index, group_id, bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM queued_messages WHERE order_index = ? AND group_id = ? AND bot_id = ?", (order_index, group_id, bot_id))
        await db.commit()


async def cache_reply_text_message(chat_id, bot_id, user_id, message_id, text):
    """Cache recent normal text messages for deleted lock-target recovery."""
    text = (text or "").strip()
    if not text or not user_id or not message_id:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO reply_message_cache "
            "(chat_id, bot_id, user_id, message_id, text) VALUES (?, ?, ?, ?, ?)",
            (chat_id, bot_id, user_id, message_id, text)
        )
        await db.execute(
            "DELETE FROM reply_message_cache WHERE chat_id = ? AND bot_id = ? "
            "AND cache_id NOT IN (SELECT cache_id FROM reply_message_cache "
            "WHERE chat_id = ? AND bot_id = ? ORDER BY cache_id DESC LIMIT 500)",
            (chat_id, bot_id, chat_id, bot_id)
        )
        await db.commit()

async def get_cached_reply_candidates(chat_id, bot_id, user_id, exclude_message_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        query = (
            "SELECT message_id, text FROM reply_message_cache "
            "WHERE chat_id = ? AND bot_id = ? AND user_id = ?"
        )
        params = [chat_id, bot_id, user_id]
        if exclude_message_id is not None:
            query += " AND message_id != ?"
            params.append(exclude_message_id)
        query += " ORDER BY cache_id DESC LIMIT 50"
        cur = await db.execute(query, tuple(params))
        return await cur.fetchall()

async def delete_cached_reply_message(chat_id, bot_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM reply_message_cache WHERE chat_id = ? AND bot_id = ? AND message_id = ?",
            (chat_id, bot_id, message_id)
        )
        await db.commit()

async def clear_queued_messages(group_id, bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM queued_messages WHERE group_id = ? AND bot_id = ?", (group_id, bot_id))
        await db.commit()

async def set_fosh_status(group_id, bot_id, is_sending):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO fosh_status (group_id, bot_id, is_sending) VALUES (?, ?, ?)", (group_id, bot_id, is_sending))
        await db.commit()

async def get_fosh_status(group_id, bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT is_sending FROM fosh_status WHERE group_id = ? AND bot_id = ?", (group_id, bot_id))
        row = await cur.fetchone()
        return row[0] if row else 0

async def get_round_robin_enabled(group_id, bot_id):
    settings = await get_group_settings(group_id, bot_id)
    return settings.get('round_robin_enabled', 0) if settings else 0

async def set_round_robin_enabled(group_id, bot_id, enabled):
    await create_or_update_group_settings(group_id, bot_id, round_robin_enabled=1 if enabled else 0)

async def get_active_bots_in_group(group_id, bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        current_scope = await get_remote_scope(bot_id)
        if current_scope:
            cur = await db.execute("""
                SELECT DISTINCT gs.bot_id, b.token, b.username
                FROM group_settings gs
                JOIN bots b ON gs.bot_id = b.bot_id
                JOIN remote_bot_links l ON l.bot_id = b.bot_id
                WHERE gs.group_id = ? AND b.is_active = 1
                  AND l.root_bot_id = ?
                ORDER BY gs.bot_id
            """, (group_id, current_scope["root_bot_id"]))
        else:
            cur = await db.execute("""
                SELECT DISTINCT gs.bot_id, b.token, b.username
                FROM group_settings gs
                JOIN bots b ON gs.bot_id = b.bot_id
                WHERE gs.group_id = ? AND b.is_active = 1
                ORDER BY gs.bot_id
            """, (group_id,))
        rows = await cur.fetchall()
        return [{'bot_id': row[0], 'token': row[1], 'username': row[2]} for row in rows]

async def get_round_robin_state(group_id, bot_id):
    scope_root_id = await get_remote_root_id(bot_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """SELECT last_bot_id, last_send_time
               FROM round_robin_state
               WHERE group_id = ? AND scope_root_id = ?""",
            (group_id, scope_root_id)
        )
        row = await cur.fetchone()
        if row:
            return {'last_bot_id': row[0], 'last_send_time': row[1]}
        return None


async def update_round_robin_state(group_id, bot_id):
    """Persist a millisecond-resolution, timezone-aware round-robin timestamp."""
    scope_root_id = await get_remote_root_id(bot_id)
    last_send_time = datetime.now().astimezone().isoformat(timespec="milliseconds")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO round_robin_state
               (group_id, scope_root_id, last_bot_id, last_send_time)
               VALUES (?, ?, ?, ?)""",
            (group_id, scope_root_id, bot_id, last_send_time)
        )
        await db.commit()


async def get_next_bot_in_round_robin(group_id, current_bot_id):
    bots = await get_active_bots_in_group(group_id, current_bot_id)
    bot_ids = sorted({int(b['bot_id']) for b in bots})
    if len(bot_ids) < 2:
        return None
    try:
        current_index = bot_ids.index(int(current_bot_id))
    except ValueError:
        current_index = -1
    return bot_ids[(current_index + 1) % len(bot_ids)]


async def _get_round_robin_participant_ids(group_id, bot_id):
    """Only include bots that are actively sending with Round Robin enabled and have queued text."""
    scope = await get_remote_scope(bot_id)
    async with aiosqlite.connect(DB_PATH) as db:
        common = """
            SELECT DISTINCT gs.bot_id
            FROM group_settings gs
            JOIN bots b ON b.bot_id = gs.bot_id AND b.is_active = 1
            JOIN fosh_status fs
              ON fs.bot_id = gs.bot_id AND fs.group_id = gs.group_id AND fs.is_sending = 1
            WHERE gs.group_id = ?
              AND COALESCE(gs.round_robin_enabled, 0) = 1
              AND EXISTS (
                  SELECT 1 FROM queued_messages q
                  WHERE q.group_id = gs.group_id AND q.bot_id = gs.bot_id
                    AND q.content IS NOT NULL AND TRIM(q.content) != ''
              )
        """
        params = [group_id]
        if scope:
            common += " AND EXISTS (SELECT 1 FROM remote_bot_links l WHERE l.bot_id = gs.bot_id AND l.root_bot_id = ?)"
            params.append(scope['root_bot_id'])
        common += " ORDER BY gs.bot_id"
        cur = await db.execute(common, tuple(params))
        return [int(row[0]) for row in await cur.fetchall()]


async def wait_for_round_robin_turn(group_id, bot_id, interval):
    """Wait for this bot's actual turn; do not advance state until its send attempt finishes.

    Old database timestamps have second resolution and no timezone. Both are handled
    here so an existing DB remains compatible instead of breaking the sender loop.
    """
    while True:
        if not await get_fosh_status(group_id, bot_id):
            return False

        participants = await _get_round_robin_participant_ids(group_id, bot_id)
        # Ignore stale DB statuses for a remote whose runtime task is not alive;
        # otherwise one offline bot could block every other bot's turn forever.
        try:
            participants = [
                participant_id for participant_id in participants
                if _is_fosh_task_running(participant_id, group_id)
            ]
        except NameError:
            pass
        if int(bot_id) not in participants:
            # Some older deployments may be missing a link/settings row; do not
            # permanently block that bot because of stale participant metadata.
            return True
        if len(participants) < 2:
            return True

        state = await get_round_robin_state(group_id, bot_id)
        now = datetime.now().astimezone()
        elapsed = float('inf')
        expected_bot_id = participants[0]
        if state:
            last_bot_id = int(state['last_bot_id'])
            raw_last_time = str(state.get('last_send_time') or '')
            try:
                last_time = datetime.fromisoformat(raw_last_time.replace('Z', '+00:00'))
                if last_time.tzinfo is None:
                    # SQLite CURRENT_TIMESTAMP is UTC but lacks a timezone suffix in old DBs.
                    last_time = last_time.replace(tzinfo=timezone.utc)
                elapsed = max(0.0, (now - last_time).total_seconds())
            except (TypeError, ValueError, OverflowError):
                # A malformed legacy timestamp should reset the gate, not kill BANKAI.
                elapsed = float('inf')
            if last_bot_id in participants:
                last_idx = participants.index(last_bot_id)
                expected_bot_id = participants[(last_idx + 1) % len(participants)]

        # The first bot may send immediately. Thereafter, a turn is eligible once
        # at least the configured interval has elapsed since the previous attempt.
        if int(bot_id) == expected_bot_id and elapsed >= float(interval):
            return True

        if int(bot_id) == expected_bot_id:
            wait_for = max(0.05, min(0.25, float(interval) - elapsed))
        else:
            wait_for = 0.15
        await asyncio.sleep(wait_for)

async def set_auto_reply_setting(bot_id, is_enabled, scope_type='group', scope_group_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO auto_reply_settings (bot_id, is_enabled, scope_type, scope_group_id) VALUES (?, ?, ?, ?)", (bot_id, is_enabled, scope_type, scope_group_id))
        await db.commit()

async def get_auto_reply_setting(bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM auto_reply_settings WHERE bot_id = ?", (bot_id,))
        row = await cur.fetchone()
        if row:
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        return None

async def add_auto_reply_user(user_id, bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO auto_reply_users (user_id, bot_id) VALUES (?, ?)", (user_id, bot_id))
        await db.commit()

async def get_auto_reply_users(bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM auto_reply_users WHERE bot_id = ?", (bot_id,))
        return [row[0] for row in await cur.fetchall()]

async def delete_auto_reply_user(user_id, bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM auto_reply_users WHERE user_id = ? AND bot_id = ?", (user_id, bot_id))
        await db.commit()

async def clear_auto_reply_users(bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM auto_reply_users WHERE bot_id = ?", (bot_id,))
        await db.commit()

async def add_auto_reply_text(content, bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO auto_reply_texts (content, bot_id) VALUES (?, ?)", (content, bot_id))
        await db.commit()
        return cur.lastrowid

async def get_auto_reply_texts(bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM auto_reply_texts WHERE bot_id = ? ORDER BY text_id", (bot_id,))
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in rows]

async def delete_auto_reply_text(text_id, bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM auto_reply_texts WHERE text_id = ? AND bot_id = ?", (text_id, bot_id))
        await db.commit()

async def clear_auto_reply_texts(bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM auto_reply_texts WHERE bot_id = ?", (bot_id,))
        await db.commit()

async def should_auto_reply(bot_id, user_id, chat_id):
    setting = await get_auto_reply_setting(bot_id)
    if not setting or not setting.get('is_enabled'):
        return False
    users = await get_auto_reply_users(bot_id)
    if user_id not in users:
        return False
    scope_type = setting.get('scope_type', 'group')
    scope_group_id = setting.get('scope_group_id')
    if scope_type == 'all':
        return True
    elif scope_type == 'group':
        return chat_id == scope_group_id
    return False

async def get_auto_reply_fosh_text(bot_id, group_id):
    """Pick one non-empty message previously added with !addfosh in this group."""
    messages = await get_queued_messages(group_id, bot_id)
    valid = [m['content'] for m in messages if m.get('content') and m['content'].strip()]
    if not valid:
        return ""
    return random.choice(valid)


def extract_flood_wait_time(error_message):
    """Best-effort parser for Telegram's textual retry interval."""
    match = re.search(r'Retry in\s+(\d+)\s+seconds', error_message, re.IGNORECASE)
    return int(match.group(1)) if match else None

def _retry_after_seconds(value, default=5.0):
    """Normalize RetryAfter values across python-telegram-bot versions."""
    try:
        if hasattr(value, "total_seconds"):
            value = value.total_seconds()
        seconds = float(value)
        if seconds < 0:
            return float(default)
        return seconds
    except (TypeError, ValueError, OverflowError):
        return float(default)


async def _sleep_before_retry(seconds):
    """Respect the server-provided wait without confusing timedelta with seconds."""
    seconds = _retry_after_seconds(seconds, default=3.0)
    if seconds:
        print(f"⏳ Telegram requested a retry wait of {seconds:.1f}s; sender remains active")
        await asyncio.sleep(seconds)

async def _send_message_with_retry(bot, *, chat_id, text, parse_mode=None, reply_to_message_id=None):
    """Send with rate-limit compliance and bounded transient-network retries.

    BadRequest is returned to the caller immediately. FloodWait/RetryAfter use
    Telegram's required wait; intermittent network timeouts are retried a few
    times so a failed request cannot hold the whole queue forever.
    """
    network_attempts = 0
    while True:
        try:
            kwargs = {"chat_id": chat_id, "text": text}
            if parse_mode is not None:
                kwargs["parse_mode"] = parse_mode
            if reply_to_message_id is not None:
                kwargs["reply_to_message_id"] = reply_to_message_id
            return await bot.send_message(**kwargs)
        except asyncio.CancelledError:
            raise
        except FloodWait as e:
            await _sleep_before_retry(getattr(e, "retry_after", 60))
        except RetryAfter as e:
            await _sleep_before_retry(getattr(e, "retry_after", 60))
        except (TimedOut, NetworkError):
            network_attempts += 1
            if network_attempts >= 4:
                raise
            await _sleep_before_retry(min(2 ** (network_attempts - 1), 4))
        except BadRequest:
            raise
        except Exception as e:
            error_str = str(e)
            lowered = error_str.lower()
            wait_time = extract_flood_wait_time(error_str)
            if wait_time is not None or "flood" in lowered or "too many requests" in lowered:
                await _sleep_before_retry(wait_time if wait_time is not None else 60)
                continue
            raise

async def _reply_target_missing(error):
    lowered = str(error).lower()
    return any(phrase in lowered for phrase in (
        "message to reply to not found",
        "message to reply not found",
        "message to be replied not found",
        "replied message not found",
        "message can't be replied",
        "message cannot be replied",
        "message identifier is not specified",
        "message_id_invalid",
        "message not found",
        "reply message not found",
    ))

async def _clear_reply_lock(group_id, bot_id):
    await create_or_update_group_settings(
        group_id, bot_id,
        reply_target_msg_id=None,
        reply_target_user_id=None,
        reply_target_chat_id=None,
    )

async def _send_with_reply_target_fallback(bot, *, group_id, bot_id, target_group, text, settings):
    """Send BANKAI text with a self-healing reply target.

    If the locked message was deleted or otherwise became unreplyable, the
    function immediately tries cached text messages from the same user. When
    none is usable, it clears the lock and sends normally so BANKAI continues.
    """
    reply_id = settings.get('reply_target_msg_id')
    reply_user_id = settings.get('reply_target_user_id')
    reply_chat_id = settings.get('reply_target_chat_id') or group_id

    def normal_send():
        return _send_message_with_retry(
            bot, chat_id=target_group, text=text, parse_mode=ParseMode.HTML
        )

    if not reply_id:
        return await normal_send()

    if int(reply_chat_id) != int(target_group):
        await _clear_reply_lock(group_id, bot_id)
        return await normal_send()

    # First try the currently locked target.
    try:
        return await _send_message_with_retry(
            bot,
            chat_id=target_group,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=int(reply_id),
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # Only a missing/unreplyable target should trigger failover. Other
        # BadRequest errors (for example an overlong text or missing permissions)
        # must not waste time trying up to 50 unrelated cached messages.
        if not await _reply_target_missing(e):
            raise

    if reply_user_id:
        candidates = await get_cached_reply_candidates(
            int(reply_chat_id), bot_id, int(reply_user_id), exclude_message_id=int(reply_id)
        )
        for candidate_id, _candidate_text in candidates:
            try:
                sent = await _send_message_with_retry(
                    bot,
                    chat_id=target_group,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=int(candidate_id),
                )
                await create_or_update_group_settings(
                    group_id, bot_id,
                    reply_target_msg_id=int(candidate_id),
                    reply_target_user_id=int(reply_user_id),
                    reply_target_chat_id=int(reply_chat_id),
                )
                print(
                    f"↩️ LOCK REPLY target moved: {reply_id} -> {candidate_id} "
                    f"(user {reply_user_id})"
                )
                return sent
            except asyncio.CancelledError:
                raise
            except Exception as candidate_error:
                if await _reply_target_missing(candidate_error):
                    await delete_cached_reply_message(
                        int(reply_chat_id), bot_id, int(candidate_id)
                    )
                    continue
                raise

    # No other text message from that user is available. Clear only the reply
    # target; BANKAI continues with a normal, non-reply send.
    await _clear_reply_lock(group_id, bot_id)
    print(
        f"🔓 LOCK REPLY cleared after target deletion; BANKAI continues without reply "
        f"(bot {bot_id}, group {group_id})"
    )
    return await normal_send()

async def send_fosh_messages(bot, group_id, bot_id, context):
    """Reliable per-bot queue sender with isolated failures and stable timing."""
    print(f"🔄 send_fosh_messages started for group {group_id} (bot_id: {bot_id})")
    while await get_fosh_status(group_id, bot_id):
        try:
            settings = await get_group_settings(group_id, bot_id)
            if not settings:
                await create_or_update_group_settings(
                    group_id, bot_id,
                    target_group_id=group_id,
                    send_interval=1.0,
                    tag_symbol='𒀽'
                )
                settings = await get_group_settings(group_id, bot_id)
                if not settings:
                    await asyncio.sleep(0.5)
                    continue

            messages = await get_queued_messages(group_id, bot_id)
            messages = [m for m in messages if m.get('content') and str(m['content']).strip()]
            if not messages:
                # Check the queue promptly so newly added messages don't wait 2+ seconds.
                await asyncio.sleep(0.5)
                continue

            # Random order is intentional, but isolate failures per item so one invalid
            # message cannot repeatedly block every other item for this bot.
            messages = random.sample(messages, k=len(messages))
            tag_user_ids = await get_tag_ids(bot_id, group_id)
            tag_symbol_raw = settings.get('tag_symbol', '𒀽')
            tag_symbol = html.escape(str(tag_symbol_raw))
            tag_text = ""
            if tag_user_ids:
                mentions = [
                    f'<a href="tg://user?id={int(user_id)}">{tag_symbol}</a>'
                    for user_id in tag_user_ids
                ]
                tag_text = "\n" + " ".join(mentions)

            interval = max(float(settings.get('send_interval') or 1.0), 0.5)
            round_robin_enabled = bool(settings.get('round_robin_enabled', 0))
            next_send_at = time.monotonic()

            for msg in messages:
                if not await get_fosh_status(group_id, bot_id):
                    return

                # Apply changed interval/target settings promptly while running.
                fresh_settings = await get_group_settings(group_id, bot_id)
                if fresh_settings:
                    settings = fresh_settings
                    interval = max(float(settings.get('send_interval') or 1.0), 0.5)
                    was_round_robin_enabled = round_robin_enabled
                    round_robin_enabled = bool(settings.get('round_robin_enabled', 0))
                    if was_round_robin_enabled and not round_robin_enabled:
                        # Restart the local monotonic schedule when leaving RR mode;
                        # otherwise the old deadline would be in the past and burst sends.
                        next_send_at = time.monotonic()
                    target_group = settings.get('target_group_id') or group_id
                else:
                    target_group = group_id

                if round_robin_enabled:
                    allowed = await wait_for_round_robin_turn(group_id, bot_id, interval)
                    if not allowed or not await get_fosh_status(group_id, bot_id):
                        return
                else:
                    delay = next_send_at - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    if not await get_fosh_status(group_id, bot_id):
                        return

                full_text = html.escape(str(msg['content'])) + tag_text
                sent = False
                try:
                    await _send_with_reply_target_fallback(
                        bot,
                        group_id=group_id,
                        bot_id=bot_id,
                        target_group=target_group,
                        text=full_text,
                        settings=settings,
                    )
                    sent = True
                    print(f"✅ ارسال شد به گروه {target_group} (bot {bot_id}, msg {msg.get('msg_id', '?')})")
                except asyncio.CancelledError:
                    raise
                except BadRequest as e:
                    # Permanent item/chat errors must not abort this bot's whole queue.
                    print(
                        f"⚠️ Telegram rejected message for bot {bot_id}, group {target_group}; "
                        f"item skipped for this cycle: {type(e).__name__}: {e}"
                    )
                except (TimedOut, NetworkError) as e:
                    print(f"⚠️ Temporary network send error (bot {bot_id}): {e}")
                except Exception as e:
                    # Prevent one bot-specific exception from interfering with all other
                    # messages in its batch. Other bot applications run independently.
                    print(f"❌ Send failed (bot {bot_id}, group {target_group}): {type(e).__name__}: {e}")

                if sent and tag_user_ids:
                    try:
                        await increment_mention_counts(tag_user_ids, bot_id, group_id)
                    except Exception as e:
                        print(f"⚠️ Mention counter update failed (bot {bot_id}): {e}")

                # Mark the attempt even when one item fails, so Round Robin cannot be
                # held forever by a rejected message from one remote bot.
                if round_robin_enabled:
                    try:
                        await update_round_robin_state(group_id, bot_id)
                    except Exception as e:
                        print(f"⚠️ Round Robin state update failed (bot {bot_id}): {e}")
                else:
                    # Use monotonic deadlines to avoid accumulating local loop/DB overhead.
                    now = time.monotonic()
                    next_send_at = max(next_send_at + interval, now)
                    delay = next_send_at - now
                    if delay > 0:
                        await asyncio.sleep(delay)

        except asyncio.CancelledError:
            print(f"⏹️ FOSH task cancelled (bot {bot_id}, group {group_id})")
            raise
        except Exception as e:
            print(f"❌ FOSH loop error (bot {bot_id}, group {group_id}): {type(e).__name__}: {e}")
            traceback.print_exc()
            await asyncio.sleep(1)

async def run_fosh_forever(bot, group_id, bot_id, context):
    print(f"🌟 run_fosh_forever شروع شد برای گروه {group_id} (bot_id: {bot_id})")
    while await get_fosh_status(group_id, bot_id):
        try:
            await send_fosh_messages(bot, group_id, bot_id, context)
        except asyncio.CancelledError:
            print("⏹️ run_fosh_forever cancelled")
            break
        except Exception as e:
            print(f"🔁 خطای غیرمنتظره در run_fosh_forever: {e}; ادامه خودکار...")
            await asyncio.sleep(2)
    print(f"⏹️ run_fosh_forever برای گروه {group_id} (bot {bot_id}) پایان یافت")

def get_bot_id(context):
    return context.application.bot_data.get('bot_id', 0)

async def get_prefix(context):
    bot_id = get_bot_id(context)
    if 'prefix' not in context.application.bot_data:
        prefix = await get_bot_prefix(bot_id)
        context.application.bot_data['prefix'] = prefix
    return context.application.bot_data['prefix']

async def check_admin(update, context):
    """Strict per-bot authorization.

    Main Bot: ONLY MAIN_ADMIN_ID.
    Remote Bot: MAIN_ADMIN_ID, its registered owner, or an admin
    explicitly registered for that same bot_id.
    """
    user = update.effective_user
    if not user:
        return False

    user_id = user.id
    bot = context.bot
    bot_id = get_bot_id(context)

    if getattr(user, "is_bot", False):
        return False

    if getattr(bot, "token", None) == MAIN_BOT_TOKEN:
        return user_id == MAIN_ADMIN_ID


    scope = await get_remote_scope(bot_id)
    if scope and user_id == scope["owner_id"]:
        return True

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM admins WHERE bot_id = ? AND admin_id = ? LIMIT 1",
            (bot_id, user_id)
        )
        return await cur.fetchone() is not None


def _project_root():
    return Path(__file__).resolve().parent



REMOTE_STATE_TABLES = (
    "bots",
    "admins",
    "group_settings",
    "tag_ids",
    "mention_counts",
    "queued_messages",
    "auto_reply_settings",
    "remote_owners",
    "remote_bot_links",
    "remote_creation_sessions",
    "auto_reply_users",
    "auto_reply_texts",
    "fosh_status",
    "round_robin_state",
)


async def _get_remote_bot_ids_from_db(db):
    cur = await db.execute(
        """
        SELECT DISTINCT b.bot_id
        FROM bots b
        LEFT JOIN remote_bot_links l ON l.bot_id = b.bot_id
        LEFT JOIN remote_owners o ON o.bot_id = b.bot_id
        WHERE b.token != ?
          AND (l.bot_id IS NOT NULL OR o.bot_id IS NOT NULL)
        ORDER BY b.bot_id
        """,
        (MAIN_BOT_TOKEN,),
    )
    return [int(row[0]) for row in await cur.fetchall()]


async def _export_remote_state_snapshot():
    """Export all persisted state belonging to non-main remote bots.

    No Python/source files, project files, or the SQLite file itself are
    included. The snapshot is JSON and is limited to DB state belonging to
    remote bots plus all their operational records.
    """
    global backup_current_path

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path(
        tempfile.mktemp(
            prefix=f"remote_state_{stamp}_",
            suffix=".json",
            dir=str(_project_root().parent),
        )
    )

    snapshot = {
        "backup_type": "telegram_bot_manager_remote_state_snapshot",
        "format_version": 2,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "scope": "remote_bots_only",
        "main_bot_excluded": True,
        "source_excluded": True,
        "tables": {},
    }

    async with aiosqlite.connect(DB_PATH) as db:
        remote_ids = await _get_remote_bot_ids_from_db(db)
        id_set = set(remote_ids)

        if remote_ids:
            ph = ",".join("?" for _ in remote_ids)

            queries = {
                "bots": (
                    f"""
                    SELECT bot_id, token, username, is_active, command_prefix
                    FROM bots
                    WHERE bot_id IN ({ph})
                    ORDER BY bot_id
                    """,
                    remote_ids,
                ),
                "admins": (
                    f"""
                    SELECT admin_id, bot_id
                    FROM admins
                    WHERE bot_id IN ({ph})
                    ORDER BY bot_id, admin_id
                    """,
                    remote_ids,
                ),
                "group_settings": (
                    f"""
                    SELECT group_id, bot_id, target_group_id, send_interval,
                           tag_symbol, reply_target_msg_id, reply_target_user_id,
                           reply_target_chat_id, round_robin_enabled
                    FROM group_settings
                    WHERE bot_id IN ({ph})
                    ORDER BY bot_id, group_id
                    """,
                    remote_ids,
                ),
                "tag_ids": (
                    f"""
                    SELECT id, user_id, bot_id, group_id
                    FROM tag_ids
                    WHERE bot_id IN ({ph})
                    ORDER BY bot_id, group_id, id
                    """,
                    remote_ids,
                ),
                "mention_counts": (
                    f"""
                    SELECT user_id, bot_id, group_id, count
                    FROM mention_counts
                    WHERE bot_id IN ({ph})
                    ORDER BY bot_id, group_id, user_id
                    """,
                    remote_ids,
                ),
                "queued_messages": (
                    f"""
                    SELECT msg_id, group_id, bot_id, content, order_index,
                           created_at, reply_to_msg_id
                    FROM queued_messages
                    WHERE bot_id IN ({ph})
                    ORDER BY bot_id, group_id, order_index, msg_id
                    """,
                    remote_ids,
                ),
                "auto_reply_settings": (
                    f"""
                    SELECT bot_id, is_enabled, scope_type, scope_group_id
                    FROM auto_reply_settings
                    WHERE bot_id IN ({ph})
                    ORDER BY bot_id
                    """,
                    remote_ids,
                ),
                "remote_owners": (
                    f"""
                    SELECT bot_id, owner_id, expires_at, max_bots, admin_limit, created_at
                    FROM remote_owners
                    WHERE bot_id IN ({ph})
                    ORDER BY bot_id
                    """,
                    remote_ids,
                ),
                "remote_bot_links": (
                    f"""
                    SELECT bot_id, root_bot_id, owner_id, created_at
                    FROM remote_bot_links
                    WHERE bot_id IN ({ph})
                    ORDER BY bot_id
                    """,
                    remote_ids,
                ),
                "auto_reply_users": (
                    f"""
                    SELECT user_id, bot_id
                    FROM auto_reply_users
                    WHERE bot_id IN ({ph})
                    ORDER BY bot_id, user_id
                    """,
                    remote_ids,
                ),
                "auto_reply_texts": (
                    f"""
                    SELECT text_id, content, bot_id
                    FROM auto_reply_texts
                    WHERE bot_id IN ({ph})
                    ORDER BY bot_id, text_id
                    """,
                    remote_ids,
                ),
                "fosh_status": (
                    f"""
                    SELECT group_id, bot_id, is_sending
                    FROM fosh_status
                    WHERE bot_id IN ({ph})
                    ORDER BY bot_id, group_id
                    """,
                    remote_ids,
                ),
                "round_robin_state": (
                    f"""
                    SELECT group_id, scope_root_id, last_bot_id, last_send_time
                    FROM round_robin_state
                    WHERE scope_root_id IN ({ph})
                    ORDER BY scope_root_id, group_id
                    """,
                    remote_ids,
                ),
            }

            for table, (query, params) in queries.items():
                cur = await db.execute(query, params)
                rows = await cur.fetchall()
                cols = [d[0] for d in cur.description]
                snapshot["tables"][table] = [
                    dict(zip(cols, row)) for row in rows
                ]

        owner_ids = {
            int(row["owner_id"])
            for row in snapshot["tables"].get("remote_owners", [])
            if row.get("owner_id") is not None
        }
        if owner_ids:
            ph = ",".join("?" for _ in owner_ids)
            cur = await db.execute(
                f"""
                SELECT admin_id, target_user_id, created_at
                FROM remote_creation_sessions
                WHERE admin_id IN ({ph})
                ORDER BY admin_id
                """,
                list(owner_ids),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
            snapshot["tables"]["remote_creation_sessions"] = [
                dict(zip(cols, row)) for row in rows
            ]
        else:
            snapshot["tables"]["remote_creation_sessions"] = []

    snapshot["counts"] = {
        table: len(rows)
        for table, rows in snapshot["tables"].items()
    }
    snapshot["remote_bot_ids"] = remote_ids

    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    backup_current_path = str(path)
    return path, snapshot


async def _send_full_backup(reason="scheduled"):
    """Send ONLY the remote-state snapshot to Main Admin."""
    global backup_current_path

    if backup_current_path:
        try:
            Path(backup_current_path).unlink(missing_ok=True)
        except Exception as e:
            print(f"⚠️ Previous remote snapshot cleanup failed: {e}")
        backup_current_path = None

    path, snapshot = await _export_remote_state_snapshot()

    try:
        with path.open("rb") as fh:
            await bot_manager.applications[MAIN_BOT_TOKEN].bot.send_document(
                chat_id=MAIN_ADMIN_ID,
                document=fh,
                filename=path.name,
                caption=(
                    "🗄️ Snapshot کامل وضعیت ریموت‌ها\n"
                    "📌 بدون سورس و فایل‌های پروژه\n"
                    f"🤖 ریموت‌ها: {len(snapshot['remote_bot_ids'])}\n"
                    f"💬 فحش‌ها/صف: {snapshot['counts'].get('queued_messages', 0)}\n"
                    f"👑 ادمین‌ها: {snapshot['counts'].get('admins', 0)}\n"
                    f"🎯 تنظیمات گروه: {snapshot['counts'].get('group_settings', 0)}\n"
                    f"🏷️ تگ‌ها: {snapshot['counts'].get('tag_ids', 0)}\n"
                    f"🤖 Auto Reply: {snapshot['counts'].get('auto_reply_users', 0)} کاربر / "
                    f"{snapshot['counts'].get('auto_reply_texts', 0)} متن\n"
                    f"▶️ FOSH State: {snapshot['counts'].get('fosh_status', 0)}\n"
                    f"🔄 Round Robin: {snapshot['counts'].get('round_robin_state', 0)}\n"
                    f"⏱️ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                ),
            )
        print(f"✅ Remote state snapshot sent to Main Admin: {path}")
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            backup_current_path = None
        raise
    return path


async def _stop_remote_runtime_bots(tokens):
    """Stop only restored remote bots. Main Bot remains online."""
    for token in list(tokens):
        if not token or token == MAIN_BOT_TOKEN:
            continue

        app = bot_manager.applications.get(token)
        if app is None:
            continue

        bot_id = app.bot_data.get("bot_id")
        if bot_id is not None:
            for key in [k for k in list(_fosh_tasks) if k[0] == int(bot_id)]:
                try:
                    await _stop_fosh_task(*key)
                except Exception:
                    pass

        try:
            await bot_manager.stop_application(app)
        except Exception as e:
            print(f"⚠️ Remote stop failed for {token}: {e}")

        bot_manager.applications.pop(token, None)


async def _restore_remote_state_snapshot(snapshot):
    """Replace current remote state with the snapshot and restart those bots."""
    if snapshot.get("backup_type") != "telegram_bot_manager_remote_state_snapshot":
        raise ValueError("این فایل Snapshot ریموت معتبر نیست.")

    if int(snapshot.get("format_version", 0)) != 2:
        raise ValueError("نسخه Snapshot پشتیبانی نمی‌شود.")

    if snapshot.get("main_bot_excluded") is not True:
        raise ValueError("Snapshot امن نیست: Main Bot باید خارج از Snapshot باشد.")

    tables = snapshot.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("ساختار Snapshot خراب است.")

    for table in REMOTE_STATE_TABLES:
        if table not in tables or not isinstance(tables[table], list):
            raise ValueError(f"بخش {table} در Snapshot وجود ندارد.")

    bots = tables["bots"]
    restored_ids = {int(row["bot_id"]) for row in bots}
    if not bots:
        restored_ids = set()

    for row in bots:
        if not row.get("token") or str(row["token"]) == MAIN_BOT_TOKEN:
            raise ValueError("Snapshot شامل Token نامعتبر یا Main Bot است.")

    current_tokens = []
    async with aiosqlite.connect(DB_PATH) as db:
        current_ids = await _get_remote_bot_ids_from_db(db)
        if current_ids:
            ph = ",".join("?" for _ in current_ids)
            cur = await db.execute(
                f"SELECT token FROM bots WHERE bot_id IN ({ph})",
                current_ids,
            )
            current_tokens = [row[0] for row in await cur.fetchall()]

    await _stop_remote_runtime_bots(current_tokens)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = OFF")

        current_ids = await _get_remote_bot_ids_from_db(db)
        if current_ids:
            ph = ",".join("?" for _ in current_ids)

            delete_map = {
                "admins": "bot_id",
                "group_settings": "bot_id",
                "tag_ids": "bot_id",
                "mention_counts": "bot_id",
                "queued_messages": "bot_id",
                "auto_reply_settings": "bot_id",
                "remote_bot_links": "bot_id",
                "auto_reply_users": "bot_id",
                "auto_reply_texts": "bot_id",
                "fosh_status": "bot_id",
                "bots": "bot_id",
            }

            for table, col in delete_map.items():
                await db.execute(
                    f"DELETE FROM {table} WHERE {col} IN ({ph})",
                    current_ids,
                )

            await db.execute(
                f"DELETE FROM round_robin_state WHERE scope_root_id IN ({ph})",
                current_ids,
            )

            cur = await db.execute(
                f"""
                SELECT DISTINCT owner_id
                FROM remote_owners
                WHERE bot_id IN ({ph})
                """,
                current_ids,
            )
            old_owner_ids = [int(row[0]) for row in await cur.fetchall()]
            if old_owner_ids:
                oph = ",".join("?" for _ in old_owner_ids)
                await db.execute(
                    f"DELETE FROM remote_creation_sessions "
                    f"WHERE admin_id IN ({oph})",
                    old_owner_ids,
                )

            await db.execute(
                f"DELETE FROM remote_owners WHERE bot_id IN ({ph})",
                current_ids,
            )

        for row in tables["bots"]:
            await db.execute(
                """
                INSERT OR REPLACE INTO bots
                (bot_id, token, username, is_active, command_prefix)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(row["bot_id"]),
                    str(row["token"]),
                    row.get("username"),
                    int(row.get("is_active", 1)),
                    row.get("command_prefix") or "!",
                ),
            )

        for row in tables["remote_owners"]:
            await db.execute(
                """
                INSERT OR REPLACE INTO remote_owners
                (bot_id, owner_id, expires_at, max_bots, admin_limit, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(row["bot_id"]),
                    int(row["owner_id"]),
                    row.get("expires_at"),
                    row.get("max_bots", 1),
                    row.get("admin_limit", 5),
                    row.get("created_at"),
                ),
            )

        for row in tables["remote_bot_links"]:
            await db.execute(
                """
                INSERT OR REPLACE INTO remote_bot_links
                (bot_id, root_bot_id, owner_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    int(row["bot_id"]),
                    int(row["root_bot_id"]),
                    int(row["owner_id"]),
                    row.get("created_at"),
                ),
            )

        for row in tables["admins"]:
            await db.execute(
                """
                INSERT OR IGNORE INTO admins (admin_id, bot_id)
                VALUES (?, ?)
                """,
                (int(row["admin_id"]), int(row["bot_id"])),
            )

        for row in tables["group_settings"]:
            await db.execute(
                """
                INSERT OR REPLACE INTO group_settings
                (group_id, bot_id, target_group_id, send_interval,
                 tag_symbol, reply_target_msg_id, reply_target_user_id,
                 reply_target_chat_id, round_robin_enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(row["group_id"]),
                    int(row["bot_id"]),
                    row.get("target_group_id"),
                    float(row.get("send_interval") or 1.0),
                    row.get("tag_symbol") or "𒀽",
                    row.get("reply_target_msg_id"),
                    row.get("reply_target_user_id"),
                    row.get("reply_target_chat_id"),
                    int(row.get("round_robin_enabled", 0) or 0),
                ),
            )

        for row in tables["tag_ids"]:
            await db.execute(
                """
                INSERT OR REPLACE INTO tag_ids
                (id, user_id, bot_id, group_id)
                VALUES (?, ?, ?, ?)
                """,
                (
                    int(row["id"]),
                    int(row["user_id"]),
                    int(row["bot_id"]),
                    int(row["group_id"]),
                ),
            )

        for row in tables["mention_counts"]:
            await db.execute(
                """
                INSERT OR REPLACE INTO mention_counts
                (user_id, bot_id, group_id, count)
                VALUES (?, ?, ?, ?)
                """,
                (
                    int(row["user_id"]),
                    int(row["bot_id"]),
                    int(row["group_id"]),
                    int(row["count"] or 0),
                ),
            )

        for row in tables["queued_messages"]:
            await db.execute(
                """
                INSERT OR REPLACE INTO queued_messages
                (msg_id, group_id, bot_id, content, order_index,
                 created_at, reply_to_msg_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(row["msg_id"]),
                    int(row["group_id"]),
                    int(row["bot_id"]),
                    row.get("content", ""),
                    int(row.get("order_index") or 0),
                    row.get("created_at"),
                    row.get("reply_to_msg_id"),
                ),
            )

        for row in tables["auto_reply_settings"]:
            await db.execute(
                """
                INSERT OR REPLACE INTO auto_reply_settings
                (bot_id, is_enabled, scope_type, scope_group_id)
                VALUES (?, ?, ?, ?)
                """,
                (
                    int(row["bot_id"]),
                    int(row.get("is_enabled", 0)),
                    row.get("scope_type", "group"),
                    row.get("scope_group_id"),
                ),
            )

        for row in tables["auto_reply_users"]:
            await db.execute(
                """
                INSERT OR IGNORE INTO auto_reply_users
                (user_id, bot_id)
                VALUES (?, ?)
                """,
                (int(row["user_id"]), int(row["bot_id"])),
            )

        for row in tables["auto_reply_texts"]:
            await db.execute(
                """
                INSERT OR REPLACE INTO auto_reply_texts
                (text_id, content, bot_id)
                VALUES (?, ?, ?)
                """,
                (
                    int(row["text_id"]),
                    row.get("content", ""),
                    int(row["bot_id"]),
                ),
            )

        for row in tables["fosh_status"]:
            await db.execute(
                """
                INSERT OR REPLACE INTO fosh_status
                (group_id, bot_id, is_sending)
                VALUES (?, ?, ?)
                """,
                (
                    int(row["group_id"]),
                    int(row["bot_id"]),
                    int(row.get("is_sending", 0)),
                ),
            )

        for row in tables["round_robin_state"]:
            await db.execute(
                """
                INSERT OR REPLACE INTO round_robin_state
                (group_id, scope_root_id, last_bot_id, last_send_time)
                VALUES (?, ?, ?, ?)
                """,
                (
                    int(row["group_id"]),
                    int(row["scope_root_id"]),
                    int(row["last_bot_id"]),
                    row.get("last_send_time"),
                ),
            )

        for row in tables["remote_creation_sessions"]:
            await db.execute(
                """
                INSERT OR REPLACE INTO remote_creation_sessions
                (admin_id, target_user_id, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    int(row["admin_id"]),
                    row.get("target_user_id"),
                    row.get("created_at"),
                ),
            )

        await db.commit()

    started = 0
    failed = []

    for row in bots:
        if not int(row.get("is_active", 1)):
            continue

        token = str(row["token"])
        if token == MAIN_BOT_TOKEN:
            continue

        try:
            ok = await bot_manager.add_bot(token, int(row["bot_id"]))
            if ok:
                started += 1
            else:
                failed.append(int(row["bot_id"]))
        except Exception as e:
            print(
                f"❌ Restored remote start failed "
                f"(bot_id={row['bot_id']}): {type(e).__name__}: {e}"
            )
            failed.append(int(row["bot_id"]))

    for row in tables["fosh_status"]:
        if not int(row.get("is_sending", 0)):
            continue

        bid = int(row["bot_id"])
        gid = int(row["group_id"])

        app = next(
            (
                a for a in bot_manager.applications.values()
                if int(a.bot_data.get("bot_id", -1)) == bid
            ),
            None,
        )

        if app is None:
            continue

        try:
            await _start_fosh_task(bid, gid, app)
        except Exception as e:
            print(
                f"⚠️ FOSH restore failed for bot={bid}, group={gid}: "
                f"{type(e).__name__}: {e}"
            )

    return {
        "remote_bots": len(bots),
        "admins": len(tables["admins"]),
        "queued_messages": len(tables["queued_messages"]),
        "group_settings": len(tables["group_settings"]),
        "tag_ids": len(tables["tag_ids"]),
        "fosh_states": len(tables["fosh_status"]),
        "started": started,
        "failed": failed,
    }


async def backup_scheduler():
    """Periodic Remote State Snapshot backup.

    The scheduler intentionally sends the same remote-state-only snapshot as
    manual !backup. No source/project files are included.
    """
    while True:
        try:
            interval = globals().get(
                "BACKUP_INTERVAL_SECONDS",
                globals().get("BACKUP_RETENTION_SECONDS", 3600),
            )
            interval = max(int(interval or 3600), 300)

            await asyncio.sleep(interval)

            try:
                await _send_full_backup("scheduled")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(
                    f"❌ Scheduled remote snapshot failed: "
                    f"{type(e).__name__}: {e}"
                )
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(
                f"❌ Backup scheduler error: "
                f"{type(e).__name__}: {e}"
            )
            await asyncio.sleep(300)


async def h_backup(update, context, args=""):
    """Main-admin-only remote state snapshot backup/restore."""
    if getattr(context.bot, "token", None) != MAIN_BOT_TOKEN or not is_main_private(update):
        return

    reply = update.effective_message.reply_to_message

    if reply and reply.document:
        await reply_styled(update.effective_message, 
            "⏳ در حال خواندن Snapshot ریموت‌ها..."
        )

        source = Path(
            tempfile.mktemp(
                prefix="remote_snapshot_restore_",
                suffix=".json",
                dir=str(_project_root().parent),
            )
        )

        try:
            tg_file = await reply.document.get_file()
            await tg_file.download_to_drive(custom_path=str(source))

            try:
                snapshot = json.loads(source.read_text(encoding="utf-8"))
            except Exception as e:
                raise ValueError(
                    f"فایل Snapshot معتبر نیست: {type(e).__name__}"
                )

            if snapshot.get("backup_type") != "telegram_bot_manager_remote_state_snapshot":
                raise ValueError(
                    "این فایل Snapshot جدید ریموت‌ها نیست."
                )

            tables = snapshot.get("tables") or {}
            await reply_styled(update.effective_message, 
                "✅ Snapshot معتبر است.\n\n"
                f"🤖 ریموت‌ها: {len(tables.get('bots', []))}\n"
                f"👑 ادمین‌ها: {len(tables.get('admins', []))}\n"
                f"💬 فحش‌ها/صف: {len(tables.get('queued_messages', []))}\n"
                f"📍 تنظیمات گروه و مقصد: {len(tables.get('group_settings', []))}\n"
                f"🏷️ تگ‌ها: {len(tables.get('tag_ids', []))}\n"
                f"▶️ وضعیت FOSH: {len(tables.get('fosh_status', []))}\n"
                f"🔄 Round Robin: {len(tables.get('round_robin_state', []))}\n\n"
                "🔄 همین وضعیت جایگزین می‌شود؛ سورس و Main Bot دست‌نخورده می‌مانند."
            )

            result = await _restore_remote_state_snapshot(snapshot)

            await reply_styled(update.effective_message, 
                "✅ Snapshot ریموت‌ها کامل Restore شد.\n\n"
                f"🤖 ریموت‌های ذخیره‌شده: {result['remote_bots']}\n"
                f"👑 ادمین‌ها: {result['admins']}\n"
                f"💬 فحش‌ها/صف: {result['queued_messages']}\n"
                f"📍 تنظیمات گروه: {result['group_settings']}\n"
                f"🏷️ تگ‌ها: {result['tag_ids']}\n"
                f"▶️ FOSH State: {result['fosh_states']}\n"
                f"🚀 ریموت‌های اجراشده: {result['started']}\n"
                f"⚠️ ریموت‌های اجرا نشده: {len(result['failed'])}\n\n"
                "✅ Main Bot و سورس تغییر نکردند."
            )

        except Exception as e:
            print(f"❌ Remote state restore failed: {type(e).__name__}: {e}")
            try:
                await reply_styled(update.effective_message, 
                    f"❌ بازیابی انجام نشد: {type(e).__name__}: {str(e)[:800]}"
                )
            except Exception:
                pass
        finally:
            try:
                source.unlink(missing_ok=True)
            except Exception:
                pass
        return

    try:
        await _send_full_backup("manual")
        await reply_styled(update.effective_message, 
            "✅ Snapshot کامل وضعیت ریموت‌ها به پیوی Owner اصلی ارسال شد.\n"
            "📌 بدون سورس و فایل‌های پروژه."
        )
    except Exception as e:
        await reply_styled(update.effective_message, 
            f"❌ ساخت Snapshot انجام نشد: {type(e).__name__}: {str(e)[:700]}"
        )



def _reply_target_text_error(lowered):
    """Return True for common Telegram errors meaning a reply target vanished."""
    return any(term in lowered for term in (
        "message to reply to not found",
        "replied message not found",
        "message to be replied not found",
        "message can't be replied",
        "message cannot be replied",
        "message identifier is not specified",
        "message_id_invalid",
        "message not found",
    ))

async def handle_message(update, context):
    """Commands use text; Auto Reply is triggered by any normal message type."""
    message = update.effective_message
    if not message or not update.effective_user:
        return
    if getattr(update.effective_user, "is_bot", False):
        return

    bot_id = get_bot_id(context)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id if update.effective_chat else None
    text = message.text or ""

    # !remot has a dedicated high-priority handler.
    if (
        text.strip().lower() == "!remot"
        and getattr(context.bot, "token", None) == MAIN_BOT_TOKEN
        and is_main_private(update)
    ):
        return

    prefix = (await get_prefix(context) or "!").strip() or "!"
    accepted_prefixes = {prefix, "!"}
    is_custom_command = bool(text) and any(text.startswith(p) for p in accepted_prefixes)
    is_telegram_command = bool(text) and text.startswith("/")

    # Cache only normal text messages; commands are excluded from fallback targets.
    if text and not is_custom_command and not is_telegram_command and chat_id is not None:
        try:
            await cache_reply_text_message(
                chat_id, bot_id, user_id, message.message_id, text
            )
        except Exception as e:
            print(f"⚠️ Reply text cache error (bot {bot_id}): {e}")

    # No text is required here: GIFs, stickers, photos, videos, etc. can trigger it.
    if is_telegram_command and text.startswith("/start") and (
        getattr(context.bot, "token", None) == MAIN_BOT_TOKEN
        and update.effective_chat
        and update.effective_chat.type == "private"
        and update.effective_user
        and update.effective_user.id == MAIN_ADMIN_ID
    ):
        return

    if text.startswith("/start") and (
        getattr(context.bot, "token", None) == MAIN_BOT_TOKEN
        or getattr(context.application, "bot_data", {}).get("is_main_bot")
    ):
        return

    if not is_custom_command and not is_telegram_command:
        # Ignore service-only updates such as join/leave events, while still
        # accepting text, GIFs, stickers, photos, videos, documents, etc.
        has_user_content = bool(
            text
            or message.caption
            or message.photo
            or message.video
            or message.animation
            or message.sticker
            or message.document
            or message.audio
            or message.voice
            or message.video_note
            or message.contact
            or message.location
            or message.venue
            or message.poll
            or message.dice
            or message.game
        )
        if not has_user_content:
            return
        if chat_id is not None and await should_auto_reply(bot_id, user_id, chat_id):
            reply_text = await get_auto_reply_fosh_text(bot_id, chat_id)
            if reply_text:
                try:
                    # Explicit reply_to_message_id keeps Auto Reply working for
                    # text, GIF, sticker, photo, video, document and other media.
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=reply_text,
                        reply_to_message_id=message.message_id,
                    )
                except BadRequest as e:
                    lowered = str(e).lower()
                    if _reply_target_text_error(lowered):
                        # The trigger message disappeared between update delivery
                        # and the response. Send the auto-reply normally instead
                        # of dropping the reply action entirely.
                        try:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=reply_text,
                            )
                        except Exception as fallback_error:
                            print(f"❌ Auto Reply fallback error (bot {bot_id}): {fallback_error}")
                    else:
                        print(f"❌ Auto Reply error (bot {bot_id}): {e}")
                except (FloodWait, RetryAfter, TimedOut, NetworkError) as e:
                    print(f"❌ Auto Reply transient error (bot {bot_id}): {e}")
                except Exception as e:
                    print(f"❌ Auto Reply error (bot {bot_id}): {e}")
        return

    if not await check_admin(update, context):
        return

    print(f"📩 Received: {text} from {user_id}")

    if is_telegram_command:
        command_text = text[1:].strip()
    else:
        used_prefix = "!" if text.startswith("!") else prefix
        command_text = text[len(used_prefix):].strip()

    parts = command_text.split(None, 1)
    command = parts[0].lower() if parts else ""
    command = command.split("@", 1)[0]
    args = parts[1] if len(parts) > 1 else ""

    handlers = {
        "ping": h_ping,
        "help": h_help,
        "panel": h_panel,
        "token": h_token,
        "deltoken": h_deltoken,
        "cleartoken": h_cleartoken,
        "admin": h_admin,
        "listadmin": h_listadmin,
        "deladmin": h_deladmin,
        "clearadmin": h_clearadmin,
        "addfosh": h_addfosh,
        "addfoshtext": h_addfoshtext,
        "delfosh": h_delfosh,
        "listfosh": h_listfosh,
        "clearfosh": h_clearfosh,
        "gpid": h_gpid,
        "setgp": h_setgp,
        "settime": h_settime,
        "setid": h_setid,
        "delid": h_delid,
        "clearid": h_clearid,
        "symbol": h_symbol,
        "bankai": h_bankai,
        "satk": h_satk,
        "status": h_status,
        "info": h_info,
        "lockreply": h_lockreply,
        "clearlockreply": h_clearlockreply,
        "setreply": h_setreply,
        "clearreply": h_clearreply,
        "setname": h_setname,
        "setbio": h_setbio,
        "setshort": h_setshort,
        "setphoto": h_setphoto,
        "clearphoto": h_clearphoto,
        "squtalon": h_squtalon,
        "squtalof": h_squtalof,
        "backup": h_backup,
    }

    handler = handlers.get(command)
    if handler:
        try:
            await handler(update, context, args)
        except Exception as e:
            print(f"Error in handler {command}: {e}")
            await reply_styled(message, f"❌ Error: {str(e)}")
    else:
        await reply_styled(message, 
            "╭─────────────────────────────╮\n"
            f"│{_box_title('✗ ᴜɴᴋɴᴏᴡɴ ᴄᴏᴍᴍᴀɴᴅ')}│\n"
            "╰─────────────────────────────╯\n"
            "╰┈➤ ᴜꜱᴇ !help ᴏʀ !panel"
        )


remote_sessions = {}
remote_creation_locks = {}

def is_main_private(update):
    return (
        update.effective_chat is not None
        and update.effective_chat.type == "private"
        and update.effective_user is not None
        and update.effective_user.id == MAIN_ADMIN_ID
    )

def main_remote_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ ساخت ریموت جدید", callback_data="main_create_remote")],
        [InlineKeyboardButton("📋 مدیریت ریموت‌ها", callback_data="main_list_remotes")],
    ])


async def _get_remote(bot_id):
    """Load a remote by either the internal DB id or the real Telegram bot id.

    Older UI/messages exposed Telegram's bot id while callback_data uses the
    internal database id. Accepting both prevents a remote from appearing
    "missing" just because the two identifiers differ.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT b.bot_id, b.username, b.token, b.is_active,
                      ro.owner_id, ro.expires_at, ro.max_bots, ro.admin_limit,
                      CAST(substr(b.token, 1, instr(b.token, ':') - 1) AS INTEGER) AS telegram_bot_id
               FROM bots b
               INNER JOIN remote_owners ro ON ro.bot_id = b.bot_id
               WHERE (b.bot_id = ?
                      OR CAST(substr(b.token, 1, instr(b.token, ':') - 1) AS INTEGER) = ?)
                 AND b.token != ?
               LIMIT 1""",
            (bot_id, bot_id, MAIN_BOT_TOKEN)
        )
        return await cur.fetchone()


async def _get_remote_children(root_bot_id):
    """Return child bots in a remote namespace for the Main Bot manager."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT b.bot_id, b.username, b.is_active,
                      CAST(substr(b.token, 1, instr(b.token, ':') - 1) AS INTEGER) AS telegram_bot_id
               FROM bots b
               INNER JOIN remote_bot_links l ON l.bot_id = b.bot_id
               WHERE l.root_bot_id = ? AND b.bot_id != ?
               ORDER BY b.bot_id""",
            (root_bot_id, root_bot_id)
        )
        return await cur.fetchall()


async def _remote_list_markup():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT b.bot_id, b.username, b.is_active, ro.owner_id,
                      ro.expires_at, ro.max_bots, ro.admin_limit,
                      CAST(substr(b.token, 1, instr(b.token, ':') - 1) AS INTEGER) AS telegram_bot_id
               FROM bots b
               INNER JOIN remote_owners ro ON ro.bot_id = b.bot_id
               WHERE b.token != ?
               ORDER BY b.bot_id DESC""",
            (MAIN_BOT_TOKEN,)
        )
        remotes = await cur.fetchall()

    buttons = []
    for r in remotes:
        buttons.append([
            InlineKeyboardButton(
                f"⚙️ @{r['username'] or 'remote'} | {r['owner_id'] or '-'}",
                callback_data=f"main_manage_{r['bot_id']}"
            )
        ])
    buttons.append([InlineKeyboardButton("➕ ساخت ریموت جدید", callback_data="main_create_remote")])
    buttons.append([InlineKeyboardButton("🔄 بروزرسانی", callback_data="main_list_remotes")])
    return remotes, InlineKeyboardMarkup(buttons)

async def _build_main_remote_panel():
    try:
        remotes, markup = await _remote_list_markup()
    except Exception as e:
        print(f"❌ !remot panel DB error: {type(e).__name__}: {e}")
        return (
            "❌ خطا در خواندن لیست ریموت‌ها.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 تلاش مجدد", callback_data="main_list_remotes")],
                [InlineKeyboardButton("➕ ساخت ریموت جدید", callback_data="main_create_remote")],
            ])
        )

    if not remotes:
        return "🛠️ پنل مدیریت Main Bot\n\n❌ هنوز هیچ ریموتی ساخته نشده است.", markup

    lines = ["🛠️ پنل مدیریت Main Bot", "", "📋 ریموت‌های موجود:", ""]
    for r in remotes:
        lines.extend([
            f"🤖 @{r['username'] or 'بدون username'}",
            f"👤 Owner: {r['owner_id'] or '-'}",
            f"🆔 DB Bot ID: {r['bot_id']}",
            f"🤖 Telegram Bot ID: {r['telegram_bot_id'] or '-'}",
            f"📅 اعتبار: {r['expires_at'] or 'نامحدود'}",
            f"🔢 سقف ربات‌های فرعی: {r['max_bots'] or 1}",
            f"👑 سقف ادمین‌های اضافه: {r['admin_limit'] if r['admin_limit'] is not None else 5}",
            f"📡 وضعیت: {'🟢 فعال' if r['is_active'] else '🔴 غیرفعال'}",
            "",
        ])
    return "\n".join(lines), markup

async def main_start(update, context):
    if not is_main_private(update):
        return
    msg = update.effective_message
    if not msg or not re.fullmatch(r"\s*!remot\s*", msg.text or ""):
        return

    text, markup = await _build_main_remote_panel()
    try:
        await reply_styled(msg, text, reply_markup=markup, parse_mode=None)
    except Exception as e:
        print(f"❌ !remot send error: {type(e).__name__}: {e}")
        try:
            await reply_styled(msg, "❌ پنل ریموت باز نشد؛ دوباره !remot را بفرستید.")
        except Exception:
            pass


async def main_remote_callback(update, context):
    query = update.callback_query

    if not is_main_private(update):
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return True

    data = query.data or ""
    await query.answer()

    if data == "main_create_remote":
        remote_sessions[MAIN_ADMIN_ID] = {"step": "user_id"}
        await reply_styled(query.message, 
            "👤 آیدی عددی صاحب ریموت را ارسال کنید:\n\n"
            "⚠️ Main Admin از قبل ریموت اصلیِ دائمی و نامحدود را دارد.\n"
            "برای لغو: /cancel"
        )
        return True

    if data == "main_list_remotes":
        text, markup = await _build_main_remote_panel()
        try:
            await edit_styled(query.message, text, reply_markup=markup, parse_mode=None)
        except Exception:
            try:
                await reply_styled(query.message, text, reply_markup=markup, parse_mode=None)
            except Exception:
                pass
        return True

    if data.startswith("main_manage_"):
        try:
            bot_id = int(data.rsplit("_", 1)[1])
        except ValueError:
            await reply_styled(query.message, "❌ Bot ID نامعتبر است.")
            return True

        r = await _get_remote(bot_id)
        if not r:
            await reply_styled(query.message, "❌ ریموت پیدا نشد.")
            return True

        children = await _get_remote_children(r['bot_id'])
        child_lines = []
        for child in children:
            state = "🟢" if child['is_active'] else "🔴"
            child_lines.append(
                f"{state} @{child['username'] or 'بدون username'} — TG ID: `{child['telegram_bot_id']}`"
            )
        children_text = "\n".join(child_lines) if child_lines else "❌ هنوز ربات فرعی ثبت نشده است."

        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 تنظیم اعتبار", callback_data=f"main_settime_{r['bot_id']}")],
            [InlineKeyboardButton("🔢 تنظیم تعداد ربات", callback_data=f"main_setmax_{r['bot_id']}")],
            [InlineKeyboardButton(
                "⛔ غیرفعال کردن" if r['is_active'] else "🟢 فعال کردن",
                callback_data=f"main_toggle_{r['bot_id']}"
            )],
            [InlineKeyboardButton("🔄 بروزرسانی ریموت", callback_data=f"main_manage_{r['bot_id']}")],
            [InlineKeyboardButton("🗑 حذف ریموت", callback_data=f"main_delete_{r['bot_id']}")],
            [InlineKeyboardButton("🔙 برگشت", callback_data="main_list_remotes")],
        ])

        text = (
            f"⚙️ مدیریت ریموت\n\n"
            f"🤖 @{r['username'] or 'بدون username'}\n"
            f"🆔 DB Bot ID: {r['bot_id']}\n"
            f"🤖 Telegram Bot ID: {r['telegram_bot_id']}\n"
            f"👤 Owner ID: {r['owner_id'] or '-'}\n"
            f"📅 اعتبار: {r['expires_at'] or 'نامحدود'}\n"
            f"🔢 حداکثر ربات‌های فرعی: {r['max_bots'] or 1}\n"
            f"👑 سقف ادمین‌های اضافه: {r['admin_limit'] if r['admin_limit'] is not None else 5}\n"
            f"📊 ربات‌های فرعی فعلی: {len(children)}\n"
            f"📡 وضعیت: {'🟢 فعال' if r['is_active'] else '🔴 غیرفعال'}\n\n"
            f"ربات‌های فرعی:\n{children_text}"
        )

        await edit_styled(query.message, 
            text,
            reply_markup=markup,
            parse_mode=None
        )
        return True

    if data.startswith("main_settime_"):
        bot_id = int(data.rsplit("_", 1)[1])
        remote = await _get_remote(bot_id)
        if not remote:
            await reply_styled(query.message, "❌ ریموت پیدا نشد.")
            return True
        bot_id = remote["bot_id"]
        remote_sessions[MAIN_ADMIN_ID] = {"step": "set_time", "bot_id": bot_id}
        await reply_styled(query.message, 
            "📅 چند روز اعتبار می‌خواهی؟\n"
            "مثال: `30`\n\n"
            "برای اعتبار نامحدود: `0`"
        )
        return True

    if data.startswith("main_setmax_"):
        bot_id = int(data.rsplit("_", 1)[1])
        remote = await _get_remote(bot_id)
        if not remote:
            await reply_styled(query.message, "❌ ریموت پیدا نشد.")
            return True
        bot_id = remote["bot_id"]
        remote_sessions[MAIN_ADMIN_ID] = {"step": "set_max", "bot_id": bot_id}
        await reply_styled(query.message, 
            "🔢 حداکثر چند ربات فرعی برای این Owner مجاز باشد؟\n"
            "مثال: `5`"
        )
        return True

    if data.startswith("main_toggle_"):
        bot_id = int(data.rsplit("_", 1)[1])
        r = await _get_remote(bot_id)
        if not r:
            await reply_styled(query.message, "❌ ریموت پیدا نشد.")
            return True
        bot_id = r["bot_id"]

        new_state = 0 if r["is_active"] else 1

        await set_remote_scope_active(bot_id, new_state)
        scope_ids = await get_remote_bot_ids(bot_id, include_root=True)
        for scoped_id in scope_ids:
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT token FROM bots WHERE bot_id = ? LIMIT 1",
                    (scoped_id,)
                )
                row = await cur.fetchone()
            if not row:
                continue
            if new_state:
                await bot_manager.add_bot(row[0], scoped_id)
            else:
                app_obj = bot_manager.applications.get(row[0])
                if app_obj is not None:
                    await bot_manager.stop_application(app_obj)
                    bot_manager.applications.pop(row[0], None)

        text, markup = await _build_main_remote_panel()
        try:
            await edit_styled(query.message, text, reply_markup=markup, parse_mode=None)
        except Exception:
            await reply_styled(query.message, text, reply_markup=markup, parse_mode=None)
        return True

    if data.startswith("main_delete_"):
        bot_id = int(data.rsplit("_", 1)[1])
        r = await _get_remote(bot_id)
        if not r:
            await reply_styled(query.message, "❌ ریموت پیدا نشد.")
            return True
        bot_id = r["bot_id"]

        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"main_confirm_delete_{bot_id}")],
            [InlineKeyboardButton("❌ لغو", callback_data=f"main_manage_{bot_id}")],
        ])
        await reply_styled(query.message, 
            f"⚠️ حذف @{r['username'] or 'remote'}؟\n"
            "این کار ریموت را از سیستم حذف می‌کند.",
            reply_markup=markup
        )
        return True

    if data.startswith("main_confirm_delete_"):
        bot_id = int(data.rsplit("_", 1)[1])
        r = await _get_remote(bot_id)
        if not r:
            await reply_styled(query.message, "❌ ریموت پیدا نشد.")
            return True
        bot_id = r["bot_id"]

        scope_ids = await get_remote_bot_ids(bot_id, include_root=True)
        for scoped_id in scope_ids:
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT token FROM bots WHERE bot_id = ? LIMIT 1",
                    (scoped_id,)
                )
                row = await cur.fetchone()
            if row:
                try:
                    await bot_manager.remove_bot(row[0])
                except Exception as e:
                    print(f"Remote remove warning ({scoped_id}): {e}")
                await delete_bot_by_token(row[0])

        remote_creation_locks.pop(bot_id, None)
        text, markup = await _build_main_remote_panel()
        try:
            await edit_styled(query.message, text, reply_markup=markup, parse_mode=None)
        except Exception:
            await reply_styled(query.message, text, reply_markup=markup, parse_mode=None)
        return True

    return False


async def handle_main_remote_input(update, context):
    if not is_main_private(update):
        return False

    msg = update.effective_message
    if not msg or not msg.text:
        return False

    value = msg.text.strip()

    if value.lower() == "/cancel":
        remote_sessions.pop(MAIN_ADMIN_ID, None)
        await reply_styled(msg, "❌ عملیات لغو شد.")
        return True

    session = remote_sessions.get(MAIN_ADMIN_ID)
    if not session:
        return False

    if session["step"] == "set_admin_limit":
        if not value.isdigit() or not 0 <= int(value) <= 10000:
            await reply_styled(msg, "❌ عدد باید بین 0 تا 10000 باشد.", parse_mode=None)
            return True

        admin_limit = int(value)
        remote_bot_id = int(session["bot_id"])
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT owner_id FROM remote_owners WHERE bot_id = ? LIMIT 1",
                (remote_bot_id,)
            )
            remote = await cur.fetchone()
            if not remote:
                remote_sessions.pop(MAIN_ADMIN_ID, None)
                await reply_styled(msg, "❌ ریموت پیدا نشد.", parse_mode=None)
                return True
            await db.execute(
                "UPDATE remote_owners SET admin_limit = ? WHERE bot_id = ?",
                (admin_limit, remote_bot_id)
            )
            await db.commit()

        remote_sessions.pop(MAIN_ADMIN_ID, None)
        await reply_styled(msg, 
            f"✅ سقف ادمین‌های اضافه برای ریموت با Owner ID {remote[0]} روی {admin_limit} تنظیم شد.\n"
            + ("افزودن ادمین جدید غیرفعال است." if admin_limit == 0 else f"حداکثر {admin_limit} ادمین اضافه مجاز است."),
            parse_mode=None
        )
        return True

    if session["step"] == "set_time":
        if not value.isdigit():
            await reply_styled(msg, "❌ فقط عدد وارد کن. مثال: `30`")
            return True

        days = int(value)
        bot_id = session["bot_id"]

        if days == 0:
            expires = None
        else:
            expires = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE remote_owners SET expires_at = ? WHERE bot_id = ?",
                (expires, bot_id)
            )
            await db.commit()

        remote_sessions.pop(MAIN_ADMIN_ID, None)
        await reply_styled(msg, 
            "✅ اعتبار نامحدود شد." if days == 0
            else f"✅ اعتبار روی {days} روز تنظیم شد.\n📅 تا: {expires}"
        )
        return True

    if session["step"] == "set_max":
        if not value.isdigit() or int(value) < 1:
            await reply_styled(msg, "❌ تعداد باید یک عدد حداقل 1 باشد.")
            return True

        max_bots = int(value)
        bot_id = session["bot_id"]

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE remote_owners SET max_bots = ? WHERE bot_id = ?",
                (max_bots, bot_id)
            )
            await db.commit()

        remote_sessions.pop(MAIN_ADMIN_ID, None)
        await reply_styled(msg, f"✅ سقف ربات روی {max_bots} تنظیم شد.")
        return True

    if session["step"] == "user_id":
        if not value.isdigit() or int(value) <= 0:
            await reply_styled(msg, "❌ آیدی باید یک عدد مثبت باشد.")
            return True

        target_id = int(value)
        if target_id == MAIN_ADMIN_ID:
            await reply_styled(msg, 
                "ℹ️ Main Admin از قبل ریموت اصلیِ دائمی و نامحدود را دارد.\n"
                "برای ساخت ریموت برای شخص دیگری، آیدی همان شخص را ارسال کنید."
            )
            return True

        session["target_user_id"] = target_id
        session["step"] = "token"
        await reply_styled(msg, "🔑 توکن ربات را ارسال کنید:")
        return True

    if session["step"] == "token":
        token = value

        if token == MAIN_BOT_TOKEN:
            await reply_styled(msg, "❌ توکن Main Bot قابل ثبت نیست.")
            return True

        test_bot = None
        try:
            from telegram import Bot
            test_bot = Bot(token=token)
            await test_bot.initialize()
            info = await test_bot.get_me()

            existing = await get_bot_by_token(token)
            if existing:
                await reply_styled(msg, "❌ این توکن قبلاً ثبت شده است.")
                remote_sessions.pop(MAIN_ADMIN_ID, None)
                return True

            bot_id = await add_bot(token, info.username or f"remote_{info.id}")
            if not bot_id:
                raise RuntimeError("ثبت ریموت در دیتابیس انجام نشد.")

            owner_id = session["target_user_id"]
            await add_admin(owner_id, bot_id)

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO remote_owners "
                    "(bot_id, owner_id, max_bots) VALUES (?, ?, 1)",
                    (bot_id, owner_id)
                )
                await db.commit()
            await link_remote_bot(bot_id, bot_id, owner_id)

            remote_app = await bot_manager._create_and_start(token, bot_id)
            if remote_app is None or not getattr(
                getattr(remote_app, "updater", None), "running", False
            ):
                raise RuntimeError("ریموت ساخته شد اما Polling آن شروع نشد.")

            remote_app.bot_data["remote_owner_id"] = owner_id
            remote_app.bot_data["remote_isolated"] = True

            remote_sessions.pop(MAIN_ADMIN_ID, None)
            await reply_styled(msg, 
                f"✅ ریموت ساخته شد.\n\n"
                f"👤 Owner: `{owner_id}`\n"
                f"🤖 @{info.username or 'بدون username'}\n"
                f"🆔 Bot ID: {info.id}",
                parse_mode=None
            )
            return True

        except Exception as e:
            remote_sessions.pop(MAIN_ADMIN_ID, None)
            try:
                existing = await get_bot_by_token(token)
                if existing:
                    await delete_bot_by_token(token)
            except Exception:
                pass
            await reply_styled(msg, f"❌ ساخت ریموت انجام نشد.\n\n{str(e)[:700]}")
            return True
        finally:
            if test_bot is not None:
                try:
                    await test_bot.shutdown()
                except Exception:
                    pass

    return False

async def handle_callback_query(update, context):
    query = update.callback_query
    user_id = update.effective_user.id
    if not await check_admin(update, context):
        await query.answer("⛔ You are not authorized.", show_alert=True)
        return

    data = query.data
    if data.startswith('main_'):
        return

    # The per-remote admin limit is configurable only by the Main Admin from
    # the main bot's !panel.
    if data == "panel_admin_limits":
        if getattr(context.bot, "token", None) != MAIN_BOT_TOKEN or user_id != MAIN_ADMIN_ID:
            await query.answer("⛔ فقط Owner اصلی از پنل ربات اصلی می‌تواند این تنظیم را تغییر دهد.", show_alert=True)
            return
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT b.bot_id, b.username, ro.owner_id, ro.admin_limit
                   FROM bots b JOIN remote_owners ro ON ro.bot_id = b.bot_id
                   WHERE b.token != ? ORDER BY b.bot_id DESC""",
                (MAIN_BOT_TOKEN,)
            )
            remotes = await cur.fetchall()
        if not remotes:
            await reply_styled(query.message, "هنوز هیچ ریموتی ساخته نشده است.", parse_mode=None)
            await query.answer()
            return
        buttons = []
        for remote in remotes:
            limit_value = remote["admin_limit"] if remote["admin_limit"] is not None else 5
            label = f"@{remote['username'] or 'remote'} | Owner {remote['owner_id']} | {limit_value} ادمین"
            buttons.append([InlineKeyboardButton(label[:64], callback_data=f"panel_admin_limit_select_{remote['bot_id']}")])
        buttons.append([InlineKeyboardButton("🔙 برگشت به پنل", callback_data="panel_admin_limit_back")])
        await reply_styled(query.message, 
            "👑 تنظیم سقف ادمین ریموت‌ها\n\nیک ریموت را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=None
        )
        await query.answer()
        return

    if data.startswith("panel_admin_limit_select_"):
        if getattr(context.bot, "token", None) != MAIN_BOT_TOKEN or user_id != MAIN_ADMIN_ID:
            await query.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        try:
            remote_bot_id = int(data.rsplit("_", 1)[1])
        except ValueError:
            await query.answer("شناسه ریموت نامعتبر است.", show_alert=True)
            return
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT owner_id, admin_limit FROM remote_owners WHERE bot_id = ? LIMIT 1",
                (remote_bot_id,)
            )
            remote = await cur.fetchone()
        if not remote:
            await query.answer("ریموت پیدا نشد.", show_alert=True)
            return
        remote_sessions[MAIN_ADMIN_ID] = {"step": "set_admin_limit", "bot_id": remote_bot_id}
        current_limit = remote[1] if remote[1] is not None else 5
        await reply_styled(query.message, 
            f"👑 سقف فعلی ادمین‌های اضافه: {current_limit}\n"
            "عدد جدید را بفرستید (از 0 تا 10000).\n"
            "0 یعنی افزودن ادمین جدید غیرفعال شود.\n"
            "برای لغو: /cancel",
            parse_mode=None
        )
        await query.answer()
        return

    if data == "panel_admin_limit_back":
        if getattr(context.bot, "token", None) != MAIN_BOT_TOKEN or user_id != MAIN_ADMIN_ID:
            await query.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        remote_sessions.pop(MAIN_ADMIN_ID, None)
        await reply_styled(query.message, 
            "╭─────────────────────────────╮\n"
            f"│{_box_title('ʙᴏᴛ ᴄᴏᴍᴍᴀɴᴅ ᴘᴀɴᴇʟ')}│\n"
            "╰─────────────────────────────╯\n\n"
            "╰┈➤ ᴛᴀᴘ ᴀ ᴄᴏᴍᴍᴀɴᴅ ꜰᴏʀ ɪᴛꜱ ᴅᴇꜱᴄʀɪᴘᴛɪᴏɴ\n"
            f"╰┈➤ ᴜᴘᴛɪᴍᴇ ➙ {get_uptime()}",
            reply_markup=get_glass_panel(include_remote_admin_limits=True),
            parse_mode=None
        )
        await query.answer()
        return

    if data == "noop":
        await query.answer()
        return

    if data.startswith("desc_"):
        cmd = data.replace("desc_", "")
        desc = COMMANDS_DESC.get(cmd)
        if desc:
            # Descriptions are sent as plain text: no Markdown parser means no
            # BadRequest caused by unmatched special characters in descriptions.
            description_text = desc["full"].replace("**", "").replace("`", "")
            await reply_styled(query.message, 
                f"COMMAND: !{cmd}\n\n"
                f"{description_text}\n\n"
                f"USAGE: !{cmd}",
                parse_mode=None
            )
            await query.answer()
        else:
            await query.answer("❌ دستور ناشناخته", show_alert=True)
        return

    if data == "about":
        await reply_styled(query.message, 
            "╭─────────────────────────────╮\n"
            f"│{_box_title('ʀᴇᴍᴏᴛᴇ ʙᴏᴛ ɪɴꜰᴏ')}│\n"
            "╰─────────────────────────────╯\n\n"
            "╰┈➤ نسخه: 3.0\n"
            "╰┈➤ مدیریت ربات‌ها و ادمین‌ها\n"
            "╰┈➤ صف پیام و پاسخ خودکار\n"
            "╰┈➤ پنل فرمان‌های تعاملی\n"
            f"╰┈➤ ᴜᴘᴛɪᴍᴇ ➙ {get_uptime()}",
            parse_mode=None
        )
        await query.answer()
        return

    if data == "refresh_panel":
        is_main_bot = (
            getattr(context.bot, "token", None) == MAIN_BOT_TOKEN
            and query.message.chat.type == "private"
            and user_id == MAIN_ADMIN_ID
        )
        await edit_styled(query.message, 
            "╭─────────────────────────────╮\n"
            f"│{_box_title('ʙᴏᴛ ᴄᴏᴍᴍᴀɴᴅ ᴘᴀɴᴇʟ')}│\n"
            "╰─────────────────────────────╯\n\n"
            "╰┈➤ ᴛᴀᴘ ᴀ ᴄᴏᴍᴍᴀɴᴅ ꜰᴏʀ ɪᴛꜱ ᴅᴇꜱᴄʀɪᴘᴛɪᴏɴ\n"
            f"╰┈➤ ᴜᴘᴛɪᴍᴇ ➙ {get_uptime()}",
            reply_markup=get_glass_panel(include_remote_admin_limits=is_main_bot),
            parse_mode=None
        )
        await query.answer("🔄 پنل بروزرسانی شد")
        return

    commands_map = {
        "cmd_ping": "!ping",
        "cmd_help": "!help",
        "cmd_token": "!token",
        "cmd_deltoken": "!deltoken",
        "cmd_cleartoken": "!cleartoken",
        "cmd_admin": "!admin",
        "cmd_listadmin": "!listadmin",
        "cmd_deladmin": "!deladmin",
        "cmd_clearadmin": "!clearadmin",
        "cmd_addfosh": "!addfosh",
        "cmd_addfoshtext": "!addfoshtext",
        "cmd_delfosh": "!delfosh",
        "cmd_listfosh": "!listfosh",
        "cmd_clearfosh": "!clearfosh",
        "cmd_gpid": "!gpid",
        "cmd_setgp": "!setgp",
        "cmd_settime": "!settime",
        "cmd_setid": "!setid",
        "cmd_delid": "!delid",
        "cmd_clearid": "!clearid",
        "cmd_symbol": "!symbol",
        "cmd_bankai": "!bankai",
        "cmd_satk": "!satk",
        "cmd_status": "!status",
        "cmd_info": "!info",
        "cmd_lockreply": "!lockreply",
        "cmd_clearlockreply": "!clearlockreply",
        "cmd_setreply": "!setreply",
        "cmd_clearreply": "!clearreply",
        "cmd_setname": "!setname",
        "cmd_setbio": "!setbio",
        "cmd_setshort": "!setshort",
        "cmd_setphoto": "!setphoto",
        "cmd_clearphoto": "!clearphoto",
        "cmd_squtalon": "!squtalon",
        "cmd_squtalof": "!squtalof",
    }
    if data in commands_map:
        cmd = commands_map[data]
        await query.answer(text=f"✅ {cmd}", show_alert=False)
    else:
        await query.answer()

async def h_help(update, context, args=""):
    """Stylish plain-text help with the existing tap-to-copy command buttons."""
    if not await check_admin(update, context):
        return

    text = (
        "╭─────────────────────────────╮\n"
        f"│{_box_title('ʙᴏᴛ ᴄᴏᴍᴍᴀɴᴅ ᴘᴀɴᴇʟ')}│\n"
        "╰─────────────────────────────╯\n\n"
        "ᴀᴅᴍɪɴ ᴄᴏᴍᴍᴀɴᴅꜱ\n"
        "╰┈➤ !ping - ᴛᴇꜱᴛ ʙᴏᴛ\n"
        "╰┈➤ !token <tok> - ᴀᴅᴅ ʙᴏᴛ\n"
        "╰┈➤ !deltoken <tok> - ʀᴇᴍᴏᴠᴇ ʙᴏᴛ\n"
        "╰┈➤ !cleartoken - ʀᴇᴍᴏᴠᴇ ᴀʟʟ ʙᴏᴛꜱ\n"
        "╰┈➤ !admin (reply/id) - ᴀᴅᴅ ᴀᴅᴍɪɴ\n"
        "╰┈➤ !listadmin - ʟɪꜱᴛ ᴀᴅᴍɪɴꜱ\n"
        "╰┈➤ !deladmin (reply/id) - ʀᴇᴍᴏᴠᴇ ᴀᴅᴍɪɴ\n"
        "╰┈➤ !clearadmin - ᴄʟᴇᴀʀ ᴀᴅᴍɪɴꜱ\n\n"
        "ᴍᴇꜱꜱᴀɢᴇ ᴀɴᴅ ɢʀᴏᴜᴘ ᴛᴏᴏʟꜱ\n"
        "╰┈➤ !addfosh (reply/text) - ᴀᴅᴅ ᴍᴇꜱꜱᴀɢᴇꜱ\n"
        "╰┈➤ !addfoshtext (reply/text) - ᴀᴅᴅ ᴍᴜʟᴛɪʟɪɴᴇ ᴛᴇxᴛ\n"
        "╰┈➤ !delfosh <idx> - ᴅᴇʟᴇᴛᴇ ǫᴜᴇᴜᴇ ɪᴛᴇᴍ\n"
        "╰┈➤ !listfosh - ʟɪꜱᴛ ǫᴜᴇᴜᴇ\n"
        "╰┈➤ !clearfosh - ᴄʟᴇᴀʀ ǫᴜᴇᴜᴇ\n"
        "╰┈➤ !gpid - ꜱʜᴏᴡ ɢʀᴏᴜᴘ ɪᴅ\n"
        "╰┈➤ !setgp (id/current) - ꜱᴇᴛ ᴛᴀʀɢᴇᴛ ɢʀᴏᴜᴘ\n"
        "╰┈➤ !settime <sec> - ꜱᴇᴛ ɪɴᴛᴇʀᴠᴀʟ\n"
        "╰┈➤ !setid (reply/id) - ꜱᴇᴛ ᴛᴀɢ ᴜꜱᴇʀ\n"
        "╰┈➤ !delid (reply/id) - ʀᴇᴍᴏᴠᴇ ᴛᴀɢ ᴜꜱᴇʀ\n"
        "╰┈➤ !clearid - ᴄʟᴇᴀʀ ᴛᴀɢɢᴇᴅ ᴜꜱᴇʀꜱ\n"
        "╰┈➤ !symbol <sym> - ᴄʜᴀɴɢᴇ ᴘʀᴇꜰɪx\n"
        "╰┈➤ !bankai - ꜱᴛᴀʀᴛ ᴍᴇꜱꜱᴀɢᴇ ǫᴜᴇᴜᴇ\n"
        "╰┈➤ !satk - ꜱᴛᴏᴘ ᴍᴇꜱꜱᴀɢᴇ ǫᴜᴇᴜᴇ\n"
        "╰┈➤ !squtalon / !squtalof - ʀᴏᴜɴᴅ ʀᴏʙɪɴ ᴏɴ/ᴏꜰꜰ\n"
        "╰┈➤ !status - ꜱʜᴏᴡ ꜱᴛᴀᴛᴜꜱ\n"
        "╰┈➤ !info - ᴠɪᴇᴡ ᴍᴇɴᴛɪᴏɴ ɪɴꜰᴏ\n\n"
        "ᴀᴜᴛᴏ ʀᴇᴘʟʏ ᴄᴏᴍᴍᴀɴᴅꜱ\n"
        "╰┈➤ !lockreply - ʟᴏᴄᴋ ʀᴇᴘʟʏ ᴛᴀʀɢᴇᴛ\n"
        "╰┈➤ !clearlockreply - ᴄʟᴇᴀʀ ʟᴏᴄᴋᴇᴅ ʀᴇᴘʟʏ\n"
        "╰┈➤ !setreply - ᴇɴᴀʙʟᴇ ᴀᴜᴛᴏ ʀᴇᴘʟʏ\n"
        "╰┈➤ !clearreply - ᴅɪꜱᴀʙʟᴇ/ᴄʟᴇᴀʀ ᴀᴜᴛᴏ ʀᴇᴘʟʏ\n\n"
        "ʙᴏᴛ ᴘʀᴏꜰɪʟᴇ ᴀɴᴅ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ\n"
        "╰┈➤ !setname - ᴄʜᴀɴɢᴇ ʙᴏᴛ ɴᴀᴍᴇ\n"
        "╰┈➤ !setbio / !setshort - ᴄʜᴀɴɢᴇ ʙᴏᴛ ᴅᴇꜱᴄʀɪᴘᴛɪᴏɴꜱ\n"
        "╰┈➤ !setphoto / !clearphoto - ᴍᴀɴᴀɢᴇ ʙᴏᴛ ᴘʜᴏᴛᴏ\n"
        "╰┈➤ !backup - ʙᴀᴄᴋᴜᴘ ʀᴇᴍᴏᴛᴇ ꜱᴇᴛᴛɪɴɢꜱ\n\n"
        "╰┈➤ ᴛᴀᴘ ᴀ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ ᴛᴏ ᴄᴏᴘʏ ᴀ ᴄᴏᴍᴍᴀɴᴅ.\n"
        "╰┈➤ ᴜꜱᴇ !panel ꜰᴏʀ ᴅᴇᴛᴀɪʟᴇᴅ ᴅᴇꜱᴄʀɪᴘᴛɪᴏɴꜱ."
    )
    try:
        await reply_styled(update.message, 
            text,
            reply_markup=get_simple_panel(),
            parse_mode=None
        )
    except Exception as e:
        print(f"Help panel error: {e}")
        await reply_styled(update.message, "✗ ᴄᴏᴜʟᴅ ɴᴏᴛ ᴅɪꜱᴘʟᴀʏ ʜᴇʟᴘ. ᴜꜱᴇ !panel")

async def h_panel(update, context, args=""):
    """پنل شیشه‌ای جدید با توضیحات کامل"""
    if not await check_admin(update, context):
        return
    text = (
        "╭─────────────────────────────╮\n"
        f"│{_box_title('ʙᴏᴛ ᴄᴏᴍᴍᴀɴᴅ ᴘᴀɴᴇʟ')}│\n"
        "╰─────────────────────────────╯\n\n"
        "╰┈➤ ᴛᴀᴘ ᴀ ᴄᴏᴍᴍᴀɴᴅ ꜰᴏʀ ɪᴛꜱ ᴅᴇꜱᴄʀɪᴘᴛɪᴏɴ\n"
        "╰┈➤ ᴀʟʟ ᴄᴏᴍᴍᴀɴᴅꜱ ᴜꜱᴇ ᴛʜᴇ ! ᴘʀᴇꜰɪx\n\n"
        f"╰┈➤ ᴜᴘᴛɪᴍᴇ ➙ {get_uptime()}"
    )
    include_remote_admin_limits = (
        getattr(context.bot, "token", None) == MAIN_BOT_TOKEN
        and is_main_private(update)
    )
    try:
        await reply_styled(update.message, 
            text,
            reply_markup=get_glass_panel(include_remote_admin_limits=include_remote_admin_limits),
            parse_mode=None
        )
    except Exception as e:
        await reply_styled(update.message, f"✗ {small_caps('error')}: {e}", parse_mode=None)

async def h_ping(update, context, args):
    """Simple, styled health check with a single Telegram API request."""
    start = time.perf_counter()
    bot_info = await context.bot.get_me()
    latency = time.perf_counter() - start
    username = f"@{bot_info.username}" if bot_info.username else "unknown"
    text = (
        "╭─────────────────────────────╮\n"
        f"│{_box_title('ʙᴏᴛ ɪꜱ ᴏɴʟɪɴᴇ')}│\n"
        "╰─────────────────────────────╯\n"
        f"╰┈➤ ʙᴏᴛ ➙ {username}\n"
        f"╰┈➤ ʟᴀᴛᴇɴᴄʏ ➙ {latency:.2f} ₛ\n"
        f"╰┈➤ ᴜᴘᴛɪᴍᴇ ➙ {get_uptime()}"
    )
    await reply_styled(update.message, text, parse_mode=None)

async def h_token(update, context, args):
    token = args.strip()
    if not token:
        await reply_styled(update.message, "USAGE: !token [token]")
        return

    current_bot_id = get_bot_id(context)
    is_main = getattr(context.bot, "token", None) == MAIN_BOT_TOKEN

    if token == MAIN_BOT_TOKEN:
        await reply_styled(update.message, "❌ توکن Main Bot قابل ثبت نیست.")
        return

    if is_main:
        existing = await get_bot_by_token(token)
        if existing:
            await reply_styled(update.message, "❌ این توکن قبلاً ثبت شده است.")
            return
        bid = await add_bot(token)
        if not bid:
            await reply_styled(update.message, "❌ FAILED TO REGISTER BOT")
            return

        root_scope = await get_remote_scope(current_bot_id)
        if not root_scope or root_scope["root_bot_id"] != current_bot_id:
            await link_remote_bot(current_bot_id, current_bot_id, MAIN_ADMIN_ID)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO remote_owners "
                    "(bot_id, owner_id, expires_at, max_bots) VALUES (?, ?, NULL, NULL)",
                    (current_bot_id, MAIN_ADMIN_ID)
                )
                await db.commit()
        await link_remote_bot(bid, current_bot_id, MAIN_ADMIN_ID)
        await add_admin(MAIN_ADMIN_ID, bid)

        started = await bot_manager.add_bot(token, bid)
        if not started:
            await delete_bot_by_token(token)
            await reply_styled(update.message, 
                "❌ ربات ثبت شد ولی اجرا نشد. توکن را از BotFather بررسی کن و دوباره !token بزن."
            )
            return

        await reply_styled(update.message, 
            f"✅ ربات با موفقیت اجرا شد.\n"
            f"🆔 Bot ID: `{bid}`\n"
            f"🔒 Scope: `{current_bot_id}`\n"
            f"♾️ سهمیه: نامحدود"
        )
        return

    current_scope = await get_remote_scope(current_bot_id)
    if not current_scope:
        await reply_styled(update.message, "❌ این ربات ریموت معتبر نیست.")
        return

    if update.effective_user.id != current_scope["owner_id"]:
        await reply_styled(update.message, "⛔ فقط Owner این ریموت می‌تواند ربات جدید بسازد.")
        return

    root_id = current_scope["root_bot_id"]
    lock = remote_creation_locks.setdefault(root_id, asyncio.Lock())

    async with lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT max_bots FROM remote_owners WHERE bot_id = ? LIMIT 1",
                (root_id,)
            )
            row = await cur.fetchone()

        if not row:
            await reply_styled(update.message, "❌ پلن ریموت پیدا نشد.")
            return

        max_bots = int(row[0] or 1)
        owned_count = await count_remote_bots(root_id)
        if owned_count >= max_bots:
            await reply_styled(update.message, 
                f"❌ سقف ربات‌های این ریموت پر شده است: {max_bots}\n"
                f"📊 استفاده: {owned_count}/{max_bots}"
            )
            return

        existing = await get_bot_by_token(token)
        if existing:
            existing_scope = await get_remote_scope(existing["bot_id"])
            if not existing_scope or existing_scope["root_bot_id"] != root_id:
                await reply_styled(update.message, 
                    "❌ این توکن متعلق به ریموت دیگری است یا در سیستم رزرو شده."
                )
                return
            await reply_styled(update.message, "❌ این توکن قبلاً ثبت شده است.")
            return

        bid = await add_bot(token)
        if not bid:
            await reply_styled(update.message, "FAILED TO ADD BOT")
            return

        await link_remote_bot(bid, root_id, current_scope["owner_id"])
        await add_admin(current_scope["owner_id"], bid)

        started = await bot_manager.add_bot(token, bid)
        if not started:
            await delete_bot_by_token(token)
            await reply_styled(update.message, "FAILED TO START BOT")
            return

        await reply_styled(update.message, 
            f"╭─────────────────────────────╮\n"
            f"│{_box_title('ʀᴇᴍᴏᴛᴇ ʙᴏᴛ ᴀᴅᴅᴇᴅ')}│\n"
            f"╰─────────────────────────────╯\n"
            f"╰┈➤ Bot ID ➙ {bid}\n"
            f"╰┈➤ ᴜꜱᴀɢᴇ ➙ {owned_count + 1}/{max_bots}\n"
            f"╰┈➤ Scope ➙ {root_id}",
            parse_mode=None
        )

async def h_deltoken(update, context, args):
    token = args.strip()
    if not token:
        await reply_styled(update.message, "USAGE: !deltoken [token]")
        return
    if token == MAIN_BOT_TOKEN:
        await reply_styled(update.message, "CANNOT DELETE MAIN BOT")
        return

    current_bot_id = get_bot_id(context)
    is_main = getattr(context.bot, "token", None) == MAIN_BOT_TOKEN
    target = await get_bot_by_token(token)
    if not target:
        await reply_styled(update.message, "BOT NOT FOUND")
        return

    if not is_main:
        current_scope = await get_remote_scope(current_bot_id)
        target_scope = await get_remote_scope(target["bot_id"])
        if (not current_scope or not target_scope or
                current_scope["root_bot_id"] != target_scope["root_bot_id"]):
            await reply_styled(update.message, "❌ این توکن خارج از ریموت شماست.")
            return
        if target["bot_id"] == current_scope["root_bot_id"]:
            await reply_styled(update.message, "❌ ریموت اصلی را از داخل خودش نمی‌توان حذف کرد.")
            return

    await bot_manager.remove_bot(token)
    await delete_bot_by_token(token)
    await reply_styled(update.message, "BOT REMOVED")


async def h_cleartoken(update, context, args):
    current_bot_id = get_bot_id(context)
    is_main = getattr(context.bot, "token", None) == MAIN_BOT_TOKEN

    if is_main:
        await bot_manager.clear_all_except_main()
        await reply_styled(update.message, "ALL BOTS REMOVED EXCEPT MAIN")
        return

    scope = await get_remote_scope(current_bot_id)
    if not scope:
        await reply_styled(update.message, "❌ این ربات ریموت معتبر نیست.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """SELECT b.bot_id, b.token
               FROM bots b
               JOIN remote_bot_links l ON l.bot_id = b.bot_id
               WHERE l.root_bot_id = ? AND b.bot_id != ?""",
            (scope["root_bot_id"], scope["root_bot_id"])
        )
        children = await cur.fetchall()

    removed = 0
    for child_id, child_token in children:
        await bot_manager.remove_bot(child_token)
        await delete_bot_by_token(child_token)
        removed += 1

    await reply_styled(update.message, f"REMOTE CHILD BOTS CLEARED: {removed}")

async def h_admin(update, context, args):
    bid = get_bot_id(context)
    admin_id = None
    if update.message.reply_to_message:
        admin_id = update.message.reply_to_message.from_user.id
    elif args.strip():
        try: admin_id = int(args.strip())
        except ValueError:
            await reply_styled(update.message, "INVALID ID")
            return
    if not admin_id:
        await reply_styled(update.message, "USAGE: !admin [reply/id]")
        return
    bid = get_bot_id(context)
    is_main_bot = getattr(context.bot, "token", None) == MAIN_BOT_TOKEN
    if not is_main_bot:
        scope = await get_remote_scope(bid)
        if scope:
            root_id = int(scope["root_bot_id"])
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT admin_limit FROM remote_owners WHERE bot_id = ? LIMIT 1",
                    (root_id,)
                )
                limit_row = await cur.fetchone()
                admin_limit = int(limit_row[0]) if limit_row and limit_row[0] is not None else 5
                cur = await db.execute(
                    """SELECT 1 FROM admins a
                       JOIN remote_bot_links l ON l.bot_id = a.bot_id
                       WHERE l.root_bot_id = ? AND a.admin_id = ? LIMIT 1""",
                    (root_id, admin_id)
                )
                already_in_scope = await cur.fetchone() is not None
                if not already_in_scope and admin_id != int(scope["owner_id"]):
                    cur = await db.execute(
                        """SELECT COUNT(DISTINCT a.admin_id)
                           FROM admins a
                           JOIN remote_bot_links l ON l.bot_id = a.bot_id
                           WHERE l.root_bot_id = ? AND a.admin_id != ?""",
                        (root_id, int(scope["owner_id"]))
                    )
                    count_row = await cur.fetchone()
                    extra_admin_count = int(count_row[0] or 0)
                else:
                    extra_admin_count = 0
            if not already_in_scope and admin_id != int(scope["owner_id"]) and extra_admin_count >= admin_limit:
                await reply_styled(update.message, 
                    f"❌ سقف ادمین‌های اضافه این ریموت پر شده است.\n"
                    f"👑 سقف مجاز: {admin_limit}\n"
                    f"📊 تعداد فعلی در کل ریموت: {extra_admin_count}",
                    parse_mode=None
                )
                return
    await add_admin(admin_id, bid)
    await reply_styled(update.message, f"ADMIN ADDED: {admin_id}", parse_mode=None)

async def h_listadmin(update, context, args):
    bid = get_bot_id(context)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT admin_id FROM admins WHERE bot_id = ? ORDER BY admin_id",
            (bid,)
        )
        admins = [row[0] for row in await cur.fetchall()]

    if not admins:
        await reply_styled(update.message, "NO ADMINS")
        return
    text = "ADMIN LIST:\n"
    for i, aid in enumerate(admins, 1):
        text += f"{i}. {aid}\n"
    await reply_styled(update.message, text)

async def h_deladmin(update, context, args):
    admin_id = None
    if update.message.reply_to_message:
        admin_id = update.message.reply_to_message.from_user.id
    elif args.strip():
        try: admin_id = int(args.strip())
        except ValueError:
            await reply_styled(update.message, "INVALID ID")
            return
    if not admin_id:
        await reply_styled(update.message, "USAGE: !deladmin [reply/id]")
        return
    bid = get_bot_id(context)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM admins WHERE admin_id = ? AND bot_id = ?",
            (admin_id, bid)
        )
        await db.commit()
    await reply_styled(update.message, f"ADMIN REMOVED: {admin_id}")

async def h_clearadmin(update, context, args):
    bid = get_bot_id(context)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE bot_id = ?", (bid,))
        await db.commit()
    await reply_styled(update.message, "ALL ADMINS CLEARED")

async def h_addfosh(update, context, args):
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    if update.message.reply_to_message:
        content = update.message.reply_to_message.text or ""
        if not content:
            await reply_styled(update.message, "NO TEXT IN REPLIED MESSAGE")
            return
        reply_to_msg_id = update.message.reply_to_message.message_id
        await add_queued_message(content, gid, bid, reply_to_msg_id=reply_to_msg_id)
        await reply_styled(update.message, "✅ MESSAGE ADDED TO QUEUE WITH REPLY")
        return
    if args.strip():
        lines = args.strip().split('\n')
        count = 0
        for line in lines:
            if line.strip():
                await add_queued_message(line.strip(), gid, bid)
                count += 1
        await reply_styled(update.message, f"✅ {count} MESSAGE(S) ADDED")
        return
    await reply_styled(update.message, "USAGE: !addfosh [reply/text]")

async def h_addfoshtext(update, context, args):
    """Add a whole multi-line text as ONE queued FOSH message."""
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    message = update.effective_message

    if message.reply_to_message:
        content = message.reply_to_message.text or message.reply_to_message.caption or ""
        if not content.strip():
            await reply_styled(message, "NO TEXT IN REPLIED MESSAGE")
            return
        reply_to_msg_id = message.reply_to_message.message_id
        await add_queued_message(content, gid, bid, reply_to_msg_id=reply_to_msg_id)
        await reply_styled(message, "✅ MULTI-LINE MESSAGE ADDED AS ONE QUEUED MESSAGE WITH REPLY")
        return

    content = args
    if not content.strip():
        await reply_styled(message, "USAGE: !addfoshtext [multi-line text]\nیا روی یک پیام متنی ریپلای کنید.")
        return

    await add_queued_message(content, gid, bid)
    line_count = len(content.splitlines())
    await reply_styled(message, 
        f"✅ MULTI-LINE MESSAGE ADDED AS ONE QUEUED MESSAGE\n"
        f"📄 LINES: {line_count}"
    )


async def h_delfosh(update, context, args):
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    if not args.strip():
        messages = await get_queued_messages(gid, bid)
        if not messages:
            await reply_styled(update.message, "QUEUE IS EMPTY")
            return
        text = "QUEUE LIST:\n"
        for msg in messages:
            text += f"{msg['order_index']}. {msg['content']}\n"
        await reply_styled(update.message, text)
        return
    try:
        index = int(args.strip())
    except ValueError:
        await reply_styled(update.message, "INVALID INDEX")
        return
    await delete_queued_message_by_index(index, gid, bid)
    await reply_styled(update.message, f"MESSAGE {index} DELETED")

async def h_listfosh(update, context, args):
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    messages = await get_queued_messages(gid, bid)
    if not messages:
        await reply_styled(update.message, "QUEUE IS EMPTY")
        return
    text = "QUEUE LIST:\n"
    for msg in messages:
        text += f"{msg['order_index']}. {msg['content']}\n"
    await reply_styled(update.message, text)

async def h_clearfosh(update, context, args):
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    await clear_queued_messages(gid, bid)
    await reply_styled(update.message, "QUEUE CLEARED")

async def h_gpid(update, context, args):
    gid = update.effective_chat.id
    await reply_styled(update.message, f"GROUP ID: {gid}")

async def h_setgp(update, context, args):
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    if args.strip():
        try: target_group = int(args.strip())
        except ValueError:
            await reply_styled(update.message, "INVALID GROUP ID")
            return
    else:
        target_group = gid
    await create_or_update_group_settings(gid, bid, target_group_id=target_group)
    await reply_styled(update.message, f"TARGET GROUP: {target_group}")

async def h_settime(update, context, args):
    """Set the FOSH interval for every bot in the same remote namespace."""
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    if not args.strip():
        await reply_styled(update.message, "USAGE: !settime [seconds]")
        return
    try:
        interval = float(args.strip())
    except ValueError:
        await reply_styled(update.message, "INVALID NUMBER")
        return
    if interval <= 0:
        await reply_styled(update.message, "INTERVAL MUST BE > 0")
        return

    root_id = await get_remote_root_id(bid)
    scope_ids = await get_remote_bot_ids(root_id, include_root=True)
    if bid not in scope_ids:
        scope_ids.append(bid)

    scope_ids = sorted(set(scope_ids))
    for scoped_bid in scope_ids:
        await create_or_update_group_settings(gid, scoped_bid, send_interval=interval)

    await reply_styled(update.message, 
        f"INTERVAL: {interval}s\nAPPLIED TO {len(scope_ids)} REMOTE BOT(S)"
    )

async def h_setid(update, context, args):
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    user_id = None
    if update.message.reply_to_message:
        user_id = update.message.reply_to_message.from_user.id
    elif args.strip():
        try: user_id = int(args.strip())
        except ValueError:
            await reply_styled(update.message, "INVALID ID")
            return
    if not user_id:
        await reply_styled(update.message, "USAGE: !setid [reply/id]")
        return
    await add_tag_id(user_id, bid, gid)
    await reply_styled(update.message, f"USER TAGGED: {user_id}")

async def h_delid(update, context, args):
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    user_id = None
    if update.message.reply_to_message:
        user_id = update.message.reply_to_message.from_user.id
    elif args.strip():
        try: user_id = int(args.strip())
        except ValueError:
            await reply_styled(update.message, "INVALID ID")
            return
    if not user_id:
        await reply_styled(update.message, "USAGE: !delid [reply/id]")
        return
    await delete_tag_id(user_id, bid, gid)
    await reply_styled(update.message, f"USER UNTAGGED: {user_id}")

async def h_clearid(update, context, args):
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    await clear_tag_ids(bid, gid)
    await reply_styled(update.message, "TAGS CLEARED")

async def h_symbol(update, context, args):
    """Set the visual symbol used for saved user-ID tags.

    IMPORTANT: !symbol is NOT the command prefix.  Older versions incorrectly
    wrote the symbol into bots.command_prefix, which made the bot appear
    offline because it stopped recognizing commands beginning with !.
    The symbol belongs to the current bot + current group in group_settings.
    """
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    symbol = args.strip()

    if not symbol:
        await reply_styled(update.message, "USAGE: !symbol [symbol]")
        return

    settings = await get_group_settings(gid, bid)
    if not settings:
        await create_or_update_group_settings(
            gid, bid,
            target_group_id=gid,
            send_interval=1.0,
            tag_symbol=symbol
        )
    else:
        await create_or_update_group_settings(gid, bid, tag_symbol=symbol)

    await update_bot_prefix(bid, "!")
    context.application.bot_data['prefix'] = "!"

    await reply_styled(update.message, f"TAG SYMBOL: {symbol}")
    print(f"🏷️ Tag symbol changed for bot {bid}, group {gid}: {symbol}")

_fosh_tasks = {}


def _fosh_task_key(bot_id, group_id):
    return (int(bot_id), int(group_id))


def _cleanup_fosh_task(task):
    for key, current in list(_fosh_tasks.items()):
        if current is task:
            _fosh_tasks.pop(key, None)


async def _stop_fosh_task(bot_id, group_id):
    key = _fosh_task_key(bot_id, group_id)
    task = _fosh_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception as e:
            print(f"⚠️ FOSH task stop error ({bot_id}, {group_id}): {e}")


def _start_fosh_task(bot, group_id, bot_id, context):
    key = _fosh_task_key(bot_id, group_id)
    old = _fosh_tasks.get(key)
    if old and not old.done():
        return old
    task = asyncio.create_task(
        run_fosh_forever(bot, group_id, bot_id, context),
        name=f"fosh-{bot_id}-{group_id}"
    )
    _fosh_tasks[key] = task
    task.add_done_callback(_cleanup_fosh_task)
    return task


def _is_fosh_task_running(bot_id, group_id):
    task = _fosh_tasks.get(_fosh_task_key(bot_id, group_id))
    return bool(task and not task.done())


async def resume_fosh_tasks_for_bot(bot, bot_id):
    """Resume jobs whose persisted status was active before a bot/runtime restart."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT group_id FROM fosh_status WHERE bot_id = ? AND is_sending = 1",
            (int(bot_id),)
        )
        group_ids = [int(row[0]) for row in await cur.fetchall()]
    for group_id in group_ids:
        try:
            if not _is_fosh_task_running(bot_id, group_id):
                _start_fosh_task(bot, group_id, bot_id, None)
                print(f"🔁 Resumed BANKAI for bot {bot_id}, group {group_id}")
        except Exception as e:
            print(f"⚠️ Could not resume BANKAI (bot {bot_id}, group {group_id}): {e}")


async def h_bankai(update, context, args):
    bid = get_bot_id(context)
    gid = update.effective_chat.id

    is_sending = await get_fosh_status(gid, bid)
    task_running = _is_fosh_task_running(bid, gid)
    if is_sending and task_running:
        await reply_styled(update.message, "⏳ FOSH در حال اجراست")
        return
    if is_sending and not task_running:
        print(f"⚠️ Stale FOSH status repaired for group {gid}, bot {bid}")
        await set_fosh_status(gid, bid, 0)

    messages = await get_queued_messages(gid, bid)
    if not messages:
        await reply_styled(update.message, "📭 صف خالی است")
        return
    settings = await get_group_settings(gid, bid)
    if not settings:
        await create_or_update_group_settings(
            gid, bid,
            target_group_id=gid,
            send_interval=1.0,
            tag_symbol='𒀽'
        )
        print(f"✅ تنظیمات پیش‌فرض برای گروه {gid} ایجاد شد")
        settings = await get_group_settings(gid, bid)
    await _stop_fosh_task(bid, gid)
    await set_fosh_status(gid, bid, 1)
    _start_fosh_task(context.bot, gid, bid, context)
    await reply_styled(update.message, "✅ BANKAI شروع شد (ارسال پیام‌ها)")
    print(f"🚀 FOSH started for group {gid} with {len(messages)} messages")

async def h_satk(update, context, args):
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    is_sending = await get_fosh_status(gid, bid)
    if not is_sending:
        await reply_styled(update.message, "BANKAI NOT RUNNING")
        return
    await set_fosh_status(gid, bid, 0)
    await _stop_fosh_task(bid, gid)
    await reply_styled(update.message, "BANKAI STOPPED")

async def h_status(update, context, args):
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    is_sending = await get_fosh_status(gid, bid)
    messages = await get_queued_messages(gid, bid)
    settings = await get_group_settings(gid, bid)
    interval = settings.get('send_interval', 1.0) if settings else 1.0
    target = settings.get('reply_target_msg_id') if settings else None
    rr_enabled = settings.get('round_robin_enabled', 0) if settings else 0
    await reply_styled(update.message, 
        f"STATUS: {'RUNNING' if is_sending else 'STOPPED'}\n"
        f"QUEUE: {len(messages)} MESSAGES\n"
        f"INTERVAL: {interval}s\n"
        f"REPLY TARGET: {target if target else 'NONE'}\n"
        f"ROUND ROBIN: {'ON' if rr_enabled else 'OFF'}\n"
        f"UPTIME: {get_uptime()}\n"
        f"GROUP ID: {gid}"
    )

async def h_info(update, context, args):
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    counts = await get_mention_counts(bid, gid)
    tag_user_ids = await get_tag_ids(bid, gid)

    if not tag_user_ids:
        await reply_styled(update.message, "ℹ️ برای این گروه هیچ کاربری با !setid ثبت نشده است.")
        return

    count_map = {user_id: count for user_id, count in counts}
    lines = ["📊 INFO — آمار منشن‌ها", ""]
    for user_id in tag_user_ids:
        count = count_map.get(user_id, 0)
        try:
            chat = await context.bot.get_chat(user_id)
            display = f"@{chat.username}" if getattr(chat, "username", None) else (getattr(chat, "full_name", None) or str(user_id))
        except Exception:
            display = str(user_id)
        lines.append(f"👤 {display} — {count} بار")

    await reply_styled(update.message, "\n".join(lines))

async def h_lockreply(update, context, args):
    message = update.effective_message
    target = message.reply_to_message if message else None
    if not target:
        await reply_styled(message, 
            "USAGE: Reply to a message and send !lockreply to set it as reply target for BANKAI."
        )
        return

    bid = get_bot_id(context)
    gid = update.effective_chat.id
    target_user_id = target.from_user.id if target.from_user else None
    target_chat_id = target.chat.id if target.chat else gid
    target_message_id = target.message_id

    if target.text and target_user_id:
        await cache_reply_text_message(
            target_chat_id, bid, target_user_id, target_message_id, target.text
        )

    await create_or_update_group_settings(
        gid,
        bid,
        reply_target_msg_id=target_message_id,
        reply_target_user_id=target_user_id,
        reply_target_chat_id=target_chat_id,
    )
    await reply_styled(message, 
        "🔒 LOCK REPLY TARGET SET\n"
        f"Message ID: {target_message_id}\n"
        f"User ID: {target_user_id or '-'}\n"
        "اگر این پیام حذف شود، ابتدا پیام متنی دیگری از همان فرد پیدا می‌شود؛ "
        "اگر چیزی پیدا نشود، BANKAI بدون ریپلای ادامه می‌دهد."
    )

async def h_clearlockreply(update, context, args):
    """Clear the !lockreply target for this group/bot only."""
    message = update.effective_message
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    settings = await get_group_settings(gid, bid)
    if not settings or not settings.get("reply_target_msg_id"):
        await reply_styled(message, "ℹ️ REPLY LOCK IS ALREADY CLEAR")
        return
    await _clear_reply_lock(gid, bid)
    await reply_styled(message, 
        "🔓 REPLY LOCK CLEARED\n"
        "Queued messages will no longer reply to a locked message.\n"
        "Use !lockreply (by replying to a message) to set a new target."
    )


async def h_setreply(update, context, args):
    """Enable Auto Reply for the sender of any replied message type."""
    message = update.effective_message
    bid = get_bot_id(context)
    gid = update.effective_chat.id

    user_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        user_id = message.reply_to_message.from_user.id
    elif args.strip():
        try:
            user_id = int(args.strip())
        except ValueError:
            await reply_styled(message, "INVALID USER ID")
            return

    if not user_id:
        await reply_styled(message, 
            "USAGE: Reply to any user's message (text/GIF/sticker/photo/etc.) and send !setreply"
        )
        return
    if user_id == bid:
        await reply_styled(message, "❌ CANNOT SET AUTO REPLY FOR THE BOT ITSELF")
        return

    await add_auto_reply_user(user_id, bid)
    await set_auto_reply_setting(bid, 1, scope_type='group', scope_group_id=gid)

    queue = await get_queued_messages(gid, bid)
    valid_count = sum(1 for m in queue if m.get('content') and m['content'].strip())
    await reply_styled(message, 
        f"✅ AUTO REPLY ENABLED\n"
        f"USER: {user_id}\n"
        f"GROUP: {gid}\n"
        f"REPLIES AVAILABLE: {valid_count}\n\n"
        "Auto Reply works for text, GIF, sticker, photo, video and other normal messages."
    )


async def h_clearreply(update, context, args):
    """Clear every Auto Reply registration for the current bot."""
    message = update.effective_message
    bid = get_bot_id(context)
    gid = update.effective_chat.id

    users = await get_auto_reply_users(bid)
    await clear_auto_reply_users(bid)
    await set_auto_reply_setting(bid, 0, scope_type='group', scope_group_id=gid)

    await reply_styled(message, 
        f"✅ ALL AUTO REPLIES CLEARED\n"
        f"👤 REMOVED USERS: {len(users)}\n"
        f"📍 GROUP: {gid}"
    )


async def h_squtalon(update, context, args):
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    settings = await get_group_settings(gid, bid)
    if not settings:
        await create_or_update_group_settings(
            gid, bid,
            target_group_id=gid,
            send_interval=1.0,
            tag_symbol='𒀽'
        )
    await set_round_robin_enabled(gid, bid, True)
    await reply_styled(update.message, 
        f"✅ **ROUND ROBIN ACTIVATED**\n\n"
        f"Bots in this group will send messages in turn.\n"
        f"Each bot waits for its turn based on the interval set.\n"
        f"Use !squtalof to disable."
    )
    print(f"✅ Round Robin activated for group {gid}, bot {bid}")

async def h_squtalof(update, context, args):
    bid = get_bot_id(context)
    gid = update.effective_chat.id
    await set_round_robin_enabled(gid, bid, False)
    await reply_styled(update.message, 
        f"❌ **ROUND ROBIN DEACTIVATED**\n\n"
        f"All bots will send messages independently.\n"
        f"Use !squtalon to activate again."
    )
    print(f"✅ Round Robin deactivated for group {gid}, bot {bid}")

async def h_setname(update, context, args):
    if not args.strip():
        await reply_styled(update.message, "USAGE: !setname [name]")
        return
    new_name = args.strip()
    bot = context.bot
    try:
        await bot.set_my_name(new_name)
        await reply_styled(update.message, f"NAME CHANGED TO: {new_name}")
    except Exception as e:
        await reply_styled(update.message, f"FAILED: {str(e)}")

async def h_setbio(update, context, args):
    """Set the bio/description for the exact bot receiving this command.

    Telegram exposes two bot profile text fields: description and short description.
    Keep both in sync so !setbio behaves consistently for Main Bot and every remote.
    """
    message = update.effective_message
    new_bio = (args or "").strip()
    if not new_bio:
        await reply_styled(message, "USAGE: !setbio [bio]")
        return

    bot = context.bot
    bot_id = get_bot_id(context)
    try:
        me = await bot.get_me()

        await bot.set_my_description(description=new_bio)

        short_bio = new_bio[:120]
        await bot.set_my_short_description(short_description=short_bio)

        await reply_styled(message, 
            f"✅ BIO CHANGED\n"
            f"🤖 @{me.username or 'unknown'}\n"
            f"🆔 Bot ID: {me.id}\n"
            f"🔧 Internal ID: {bot_id}\n\n"
            f"Description:\n{new_bio}\n\n"
            f"Short Description:\n{short_bio}"
        )
        print(f"✅ Bio updated for @{me.username} (bot_id={bot_id}, telegram_id={me.id})")
    except Exception as e:
        print(f"❌ setbio failed for bot_id={bot_id}: {type(e).__name__}: {e}")
        await reply_styled(message, 
            f"❌ SETBIO FAILED\n"
            f"🤖 Bot ID: {bot_id}\n"
            f"⚠️ {type(e).__name__}: {e}"
        )

async def h_setshort(update, context, args):
    """Set only the short description for the exact bot receiving this command."""
    message = update.effective_message
    new_short = (args or "").strip()
    if not new_short:
        await reply_styled(message, "USAGE: !setshort [short description]")
        return

    if len(new_short) > 120:
        await reply_styled(message, "❌ Short Description حداکثر ۱۲۰ کاراکتر است.")
        return

    bot = context.bot
    bot_id = get_bot_id(context)
    try:
        me = await bot.get_me()
        await bot.set_my_short_description(short_description=new_short)
        await reply_styled(message, 
            f"✅ SHORT DESCRIPTION CHANGED\n"
            f"🤖 @{me.username or 'unknown'}\n"
            f"🆔 Bot ID: {me.id}\n"
            f"🔧 Internal ID: {bot_id}\n\n"
            f"{new_short}"
        )
        print(
            f"✅ Short description updated for @{me.username} "
            f"(bot_id={bot_id}, telegram_id={me.id})"
        )
    except Exception as e:
        print(
            f"❌ setshort failed for bot_id={bot_id}: "
            f"{type(e).__name__}: {e}"
        )
        await reply_styled(message, 
            f"❌ SETSHORT FAILED\n"
            f"🤖 Bot ID: {bot_id}\n"
            f"⚠️ {type(e).__name__}: {e}"
        )


async def h_setphoto(update, context, args):
    """Set the profile photo of the exact bot receiving this command.

    Usage: reply to a photo with !setphoto.
    """
    message = update.effective_message
    reply = message.reply_to_message if message else None

    if not reply or not reply.photo:
        await reply_styled(message, 
            "USAGE: روی یک عکس ریپلای کنید و `!setphoto` را ارسال کنید."
        )
        return

    bot = context.bot
    bot_id = get_bot_id(context)

    try:
        from telegram import InputProfilePhotoStatic

        if not hasattr(bot, "set_my_profile_photo"):
            await reply_styled(message, 
                "❌ نسخه python-telegram-bot نصب‌شده از تغییر عکس پروفایل بات پشتیبانی نمی‌کند.\n"
                "این قابلیت به نسخه 22.7 یا بالاتر نیاز دارد."
            )
            return

        photo = reply.photo[-1]

        # Telegram's setMyProfilePhoto requires a NEW multipart-uploaded file;
        # the PhotoSize file_id from the replied message cannot be reused here.
        # Download the selected PhotoSize and upload the bytes as a fresh file.
        tg_file = await photo.get_file()
        with tempfile.NamedTemporaryFile(prefix="setphoto_", suffix=".jpg", delete=False) as tmp:
            temp_photo_path = tmp.name

        try:
            await tg_file.download_to_drive(custom_path=temp_photo_path)
            with open(temp_photo_path, "rb") as photo_file:
                profile_photo = InputProfilePhotoStatic(photo=photo_file)

                me = await bot.get_me()
                await bot.set_my_profile_photo(photo=profile_photo)
        finally:
            try:
                os.remove(temp_photo_path)
            except OSError:
                pass

        await reply_styled(message, 
            f"✅ PROFILE PHOTO CHANGED\n"
            f"🤖 @{me.username or 'unknown'}\n"
            f"🆔 Bot ID: {me.id}\n"
            f"🔧 Internal ID: {bot_id}\n\n"
            f"این عکس فقط روی همین ریموت اعمال شد."
        )
        print(
            f"✅ Profile photo updated for @{me.username} "
            f"(bot_id={bot_id}, telegram_id={me.id})"
        )
    except Exception as e:
        print(
            f"❌ setphoto failed for bot_id={bot_id}: "
            f"{type(e).__name__}: {e}"
        )
        await reply_styled(message, 
            f"❌ SETPHOTO FAILED\n"
            f"🤖 Bot ID: {bot_id}\n"
            f"⚠️ {type(e).__name__}: {e}"
        )


async def h_clearphoto(update, context, args):
    """Remove profile photos from all managed non-main bots."""
    message = update.effective_message

    try:
        from telegram import Bot

        # Read all bots directly so inactive managed remotes are included too.
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT bot_id, token, username FROM bots WHERE token != ? ORDER BY bot_id",
                (MAIN_BOT_TOKEN,)
            )
            rows = await cur.fetchall()

        if not rows:
            await reply_styled(message, "ℹ️ هیچ بات ریموتی برای پاک‌کردن عکس وجود ندارد.")
            return

        success = []
        failed = []

        for bot_id, token, username in rows:
            try:
                async with Bot(token=token) as remote_bot:
                    if not hasattr(remote_bot, "remove_my_profile_photo"):
                        failed.append(f"{bot_id} (متد remove profile photo در نسخه نصب‌شده وجود ندارد)")
                        continue
                    await remote_bot.remove_my_profile_photo()
                    success.append(f"@{username}" if username else str(bot_id))
            except Exception as e:
                failed.append(f"{username or bot_id}: {type(e).__name__}: {str(e)[:180]}")

        result = [
            "🗑️ ALL REMOTE PROFILE PHOTOS CLEARED",
            f"✅ موفق: {len(success)}",
            f"❌ خطا: {len(failed)}",
        ]
        if success:
            result.append("\n✅ " + ", ".join(success))
        if failed:
            result.append("\n❌ " + "\n❌ ".join(failed))
        await reply_styled(message, "\n".join(result))
    except Exception as e:
        await reply_styled(message, 
            f"❌ CLEARPHOTO FAILED\n⚠️ {type(e).__name__}: {e}"
        )



class BotManager:
    def __init__(self):
        self.applications = {}
        self.main_token = MAIN_BOT_TOKEN
        self.monitor_task = None
        self.restart_locks = {}
        self.token_locks = {}
        self.stopping = False

    @staticmethod
    def configure_application(app, bot_id):
        app.bot_data['bot_id'] = bot_id
        app.bot_data['prefix'] = DEFAULT_COMMAND_PREFIX

        if getattr(app.bot, "token", None) == MAIN_BOT_TOKEN:
            app.bot_data["is_main_bot"] = True

        if app.bot_data.get("is_main_bot") or getattr(app.bot, "token", None) == MAIN_BOT_TOKEN:
            app.add_handler(
                MessageHandler(
                    filters.Regex(r"(?i)^\s*!remot\s*$") & filters.ChatType.PRIVATE,
                    main_start,
                ),
                group=-1000,
            )
            app.add_handler(
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                    handle_main_remote_input,
                ),
                group=-90,
            )
        if getattr(app.bot, "token", None) == MAIN_BOT_TOKEN:
            app.add_handler(
                CallbackQueryHandler(main_remote_callback, pattern=r"^main_")
            )
        # One universal message handler: commands use text; Auto Reply sees all message types.
        app.add_handler(
            MessageHandler(filters.ALL, handle_message),
            group=-50,
        )
        app.add_handler(CallbackQueryHandler(handle_callback_query))
        app.add_error_handler(handle_bot_error)
        return app

    @staticmethod
    def build_application(token):
        return (
            Application.builder()
            .token(token)
            .connect_timeout(30)
            .read_timeout(60)
            .write_timeout(60)
            .pool_timeout(30)
            .get_updates_connect_timeout(30)
            .get_updates_read_timeout(60)
            .get_updates_write_timeout(60)
            .get_updates_pool_timeout(30)
            .build()
        )

    async def start_application(self, app):
        await app.initialize()
        try:
            await app.bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            print(f"⚠️ delete_webhook failed: {e}")
        bot_info = await app.bot.get_me()
        await app.start()
        if not getattr(app, 'updater', None):
            raise RuntimeError('Updater is unavailable')
        await app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            bootstrap_retries=-1,
        )
        if not app.updater.running:
            raise RuntimeError('Polling failed to start')
        print(f"✅ Bot started: @{bot_info.username} (ID: {bot_info.id})")

    @staticmethod
    async def stop_application(app):
        try:
            if hasattr(app, 'updater') and app.updater and app.updater.running:
                await app.updater.stop()
        except Exception as e:
            print(f"Stop updater error: {e}")
        try:
            if app.running:
                await app.stop()
        except Exception as e:
            print(f"Stop application error: {e}")
        try:
            await app.shutdown()
        except Exception as e:
            print(f"Shutdown error: {e}")

    async def _create_and_start(self, token, bot_id):
        lock = self.token_locks.setdefault(token, asyncio.Lock())
        async with lock:
            existing = self.applications.get(token)
            if existing is not None and getattr(getattr(existing, 'updater', None), 'running', False):
                return existing
            if existing is not None:
                try:
                    await self.stop_application(existing)
                except Exception:
                    pass
                self.applications.pop(token, None)
            app = self.build_application(token)
            self.configure_application(app, bot_id)
            app.bot_data['prefix'] = await get_bot_prefix(bot_id)
            try:
                await self.start_application(app)
                self.applications[token] = app
                await resume_fosh_tasks_for_bot(app.bot, bot_id)
                return app
            except Exception:
                try:
                    await self.stop_application(app)
                except Exception:
                    pass
                raise

    async def restart_bot(self, token, reason='health check'):
        if self.stopping:
            return False
        lock = self.restart_locks.setdefault(token, asyncio.Lock())
        if lock.locked():
            return False
        async with lock:
            if self.stopping:
                return False
            row = await get_bot_by_token(token)
            if not row:
                print(f"⚠️ Cannot restart bot: token is no longer registered")
                return False
            bot_id = row['bot_id']
            old_app = self.applications.get(token)
            print(f"🔄 Restarting bot {bot_id} ({reason})")
            for key in [k for k in list(_fosh_tasks) if k[0] == int(bot_id)]:
                await _stop_fosh_task(*key)
            if old_app is not None:
                try:
                    await self.stop_application(old_app)
                except Exception as e:
                    print(f"⚠️ Old app stop failed: {e}")
                self.applications.pop(token, None)
            delay = 2
            for attempt in range(1, 6):
                if self.stopping:
                    return False
                try:
                    await self._create_and_start(token, bot_id)
                    print(f"✅ Bot recovered: {bot_id}")
                    return True
                except Exception as e:
                    msg = str(e)
                    if 'Conflict' in msg or 'terminated by other getUpdates request' in msg:
                        print(f"🚨 BOT CONFLICT for {bot_id}: another process/instance is polling this token. Stop the other instance.")
                    print(f"❌ Restart attempt {attempt}/5 failed for {bot_id}: {type(e).__name__}: {e}")

                    delay = min(delay * 2, 60)
            print(f"🚨 Bot {bot_id} could not be recovered now; watchdog will retry.")
            return False

    async def watchdog(self):
        print("🛡️ Bot watchdog started")
        failures = {}
        while not self.stopping:
            try:
                for token, app in list(self.applications.items()):
                    if self.stopping:
                        break
                    bot_id = app.bot_data.get('bot_id', 'unknown')
                    healthy = False
                    try:
                        if not app.running:
                            raise RuntimeError('Application is not running')
                        updater = getattr(app, 'updater', None)
                        if not updater or not updater.running:
                            raise RuntimeError('Polling updater is not running')
                        updater_task = getattr(updater, '_polling_task', None)
                        if updater_task is not None and updater_task.done():
                            exc = updater_task.exception() if not updater_task.cancelled() else None
                            raise RuntimeError(f'Polling task stopped: {exc!r}')
                        await asyncio.wait_for(app.bot.get_me(), timeout=20)
                        healthy = True
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        failures[token] = failures.get(token, 0) + 1
                        print(f"⚠️ Health check failed for bot {bot_id} ({failures[token]}/3): {type(e).__name__}: {e}")
                        if failures[token] >= 3:
                            await self.restart_bot(token, reason='3 consecutive health-check failures')
                            failures.pop(token, None)
                    if healthy:
                        failures.pop(token, None)
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"❌ Watchdog error: {type(e).__name__}: {e}")
                await asyncio.sleep(10)
        print("🛡️ Bot watchdog stopped")

    def start_watchdog(self):
        if self.monitor_task is None or self.monitor_task.done():
            self.monitor_task = asyncio.create_task(self.watchdog(), name='bot-watchdog')

    async def stop_watchdog(self):
        task = self.monitor_task
        self.monitor_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def add_bot(self, token, bot_id=None):
        if not bot_id:
            existing = await get_bot_by_token(token)
            if existing:
                bot_id = existing['bot_id']
            else:
                bot_id = await add_bot(token)
                if not bot_id:
                    existing = await get_bot_by_token(token)
                    bot_id = existing['bot_id'] if existing else None
        if not bot_id:
            print("❌ Error getting bot_id for a stored bot")
            return False
        if token in self.applications:
            return False
        try:
            # Never let one broken remote token block the Main Bot.
            await asyncio.wait_for(self._create_and_start(token, bot_id), timeout=45)
            print(f"✅ Bot started: {bot_id}")
            return True
        except asyncio.TimeoutError:
            print(f"⚠️ Bot {bot_id} start timed out; skipped so other bots can continue.")
            return False
        except Exception as e:
            error_text = str(e)
            error_name = type(e).__name__
            print(f"❌ Bot {bot_id} start error: {error_name}: {error_text}")
            lowered = error_text.lower()
            if (
                error_name.lower() in {"invalidtoken", "unauthorized"}
                or "invalid token" in lowered
                or "unauthorized" in lowered
                or "token is invalid" in lowered
            ):
                print(f"⛔ Stored bot {bot_id} has an invalid/unauthorized token; it was skipped.")
            return False

    async def remove_bot(self, token):
        if token == self.main_token:
            return False
        if token in self.applications:
            app = self.applications[token]
            bot_id = app.bot_data.get('bot_id')
            if bot_id is not None:
                for key in [k for k in list(_fosh_tasks) if k[0] == int(bot_id)]:
                    await _stop_fosh_task(*key)
            try:
                await self.stop_application(app)
            except Exception:
                pass
            del self.applications[token]
        await delete_bot_by_token(token)
        return True

    async def clear_all_except_main(self):
        tokens = [t for t in self.applications.keys() if t != self.main_token]
        for token in tokens:
            await self.remove_bot(token)
        await clear_all_tokens_except_main(self.main_token)
        return True

    async def load_existing_bots(self):
        bots = await get_all_bots()
        loaded_roots = set()
        for bot in bots:
            token = bot['token']
            bid = bot['bot_id']
            if token == self.main_token or token in self.applications:
                continue

            scope = await get_remote_scope(bid)
            if scope:
                root_id = scope["root_bot_id"]
                if root_id in loaded_roots:
                    root_active = True
                else:
                    root_active = await get_remote_scope_state(root_id)
                    if root_active:
                        loaded_roots.add(root_id)
                if not root_active:
                    print(
                        f"⏭️ Skipping bot {bid}: remote root "
                        f"{root_id} is inactive"
                    )
                    continue

            try:
                await self.add_bot(token, bid)
            except Exception as e:
                print(
                    f"⚠️ Skipping stored bot {bid}: "
                    f"{type(e).__name__}: {e}"
                )
                continue

    async def shutdown_all(self):
        self.stopping = True
        await self.stop_watchdog()
        for token in list(self.applications):
            app = self.applications.pop(token)
            bot_id = app.bot_data.get('bot_id')
            if bot_id is not None:
                for key in [k for k in list(_fosh_tasks) if k[0] == int(bot_id)]:
                    await _stop_fosh_task(*key)
            try:
                await self.stop_application(app)
            except Exception as e:
                print(f"Shutdown error: {e}")

async def handle_bot_error(update, context):
    bot = getattr(context, "bot", None)
    bot_username = getattr(bot, "username", None) or "unknown"
    print(f"❌ Error @{bot_username}: {type(context.error).__name__}: {context.error}")

async def main():
    global bot_manager
    bot_manager = BotManager()

    validate_configuration()
    print("=" * 50)
    print("🤖 BOT MANAGER STARTING...")
    print("=" * 50)

    await init_db()
    print("✅ DB READY")

    existing = await get_bot_by_token(MAIN_BOT_TOKEN)
    if not existing:
        bid = await add_bot(MAIN_BOT_TOKEN, "main_bot")
        if not bid:
            existing = await get_bot_by_token(MAIN_BOT_TOKEN)
            bid = existing['bot_id'] if existing else None
        print(f"✅ Main bot registered: {bid}")
    else:
        bid = existing['bot_id']
        print(f"✅ Main bot exists: {bid}")

    await add_admin(MAIN_ADMIN_ID, bid)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO remote_owners "
            "(bot_id, owner_id, expires_at, max_bots) VALUES (?, ?, NULL, NULL)",
            (bid, MAIN_ADMIN_ID)
        )
        await db.execute(
            "INSERT OR REPLACE INTO remote_bot_links "
            "(bot_id, root_bot_id, owner_id) VALUES (?, ?, ?)",
            (bid, bid, MAIN_ADMIN_ID)
        )
        await db.commit()
    print(f"✅ Admin registered: {MAIN_ADMIN_ID}")
    print("♾️ Main Admin remote: unlimited bots / unlimited time")

    # Start the Main Bot BEFORE loading any stored remote tokens.
    # A broken remote token must never prevent the Main Bot from coming online.
    bot_manager.stopping = False
    app = bot_manager.build_application(MAIN_BOT_TOKEN)
    bot_manager.configure_application(app, bid)
    app.bot_data['prefix'] = await get_bot_prefix(bid)
    bot_manager.applications[MAIN_BOT_TOKEN] = app

    print("🚀 Starting Main Bot...")
    await bot_manager.start_application(app)
    await resume_fosh_tasks_for_bot(app.bot, bid)
    bot_manager.start_watchdog()
    print("✅ Main Bot polling is running")

    global backup_task
    if backup_task is None or backup_task.done():
        backup_task = asyncio.create_task(backup_scheduler(), name="hourly-remote-state-backup")

    # Restore remote bots only after Main Bot is already online.
    try:
        await bot_manager.load_existing_bots()
    except Exception as e:
        print(f"⚠️ Stored remote restore finished with errors: {type(e).__name__}: {e}")

    loaded_extra = max(0, len(bot_manager.applications) - 1)
    print(f"✅ Loaded {loaded_extra} extra bots")
    if loaded_extra:
        print("✅ Restored remote bots are being loaded from the restored DB.")
    print("=" * 50)
    print("🚀 BOT IS READY")
    print("=" * 50)

    try:
        await asyncio.Event().wait()
    finally:
        await bot_manager.shutdown_all()

if __name__ == "__main__":
    restart_delay = 10
    while True:
        try:
            asyncio.run(main())
            print("🛑 Main loop ended; restarting in 10 seconds...")
        except KeyboardInterrupt:
            print("\n👋 Bots stopped by user")
            break
        except Exception as e:
            print(f"🔥 Fatal main-loop error: {type(e).__name__}: {e}")
            traceback.print_exc()
            print(f"🔄 Process supervisor will restart in {restart_delay} seconds...")
        time.sleep(restart_delay)
