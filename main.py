from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import Response
from pydantic import BaseModel

import edge_tts
import os
import tempfile
import sqlite3
import secrets
import string
import asyncio
import time
import hashlib

from datetime import datetime, timezone, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)


# =========================================================
# CONFIG
# =========================================================

DATABASE = "users.db"

WEEKLY_LIMIT = 200_000

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# ---------------------------------------------------------
# ADMIN CONFIG
# ---------------------------------------------------------
#
# Set these in Faable Environment Variables / Secrets:
#
# ADMIN_USERNAME=admin
# ADMIN_PASSWORD=YOUR_STRONG_PASSWORD
#
# NEVER put the real password directly in this file.
#

ADMIN_USERNAME = os.getenv(
    "ADMIN_USERNAME",
    "admin"
)

ADMIN_PASSWORD = os.getenv(
    "ADMIN_PASSWORD"
)

ADMIN_SESSION_HOURS = int(
    os.getenv(
        "ADMIN_SESSION_HOURS",
        "24"
    )
)

if not TELEGRAM_BOT_TOKEN:

    print(
        "WARNING: TELEGRAM_BOT_TOKEN is not set"
    )

if not ADMIN_PASSWORD:

    print(
        "WARNING: ADMIN_PASSWORD is not set. "
        "Admin login will be disabled."
    )


app = FastAPI(
    title="Edge TTS API"
)


# =========================================================
# DATABASE
# =========================================================

def get_db():

    conn = sqlite3.connect(
        DATABASE,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    conn = get_db()

    # -----------------------------------------------------
    # USERS
    # -----------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            telegram_user_id TEXT UNIQUE NOT NULL,

            activation_code TEXT,

            api_key TEXT UNIQUE,

            characters_used INTEGER DEFAULT 0,

            activated INTEGER DEFAULT 0,

            week_id TEXT,

            code_week_id TEXT
        )
    """)

    # -----------------------------------------------------
    # DEVICES
    # -----------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS devices (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            installation_id TEXT UNIQUE NOT NULL,

            telegram_user_id TEXT NOT NULL,

            manufacturer TEXT,

            model TEXT,

            android_version TEXT,

            app_version TEXT,

            platform TEXT DEFAULT 'Android',

            first_seen TEXT,

            last_seen TEXT
        )
    """)

    # -----------------------------------------------------
    # TTS REQUESTS
    # -----------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tts_requests (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            telegram_user_id TEXT NOT NULL,

            installation_id TEXT,

            character_count INTEGER DEFAULT 0,

            voice TEXT,

            rate TEXT,

            pitch TEXT,

            generation_time_ms INTEGER DEFAULT 0,

            file_size_bytes INTEGER DEFAULT 0,

            success INTEGER DEFAULT 0,

            created_at TEXT
        )
    """)

    # -----------------------------------------------------
    # ADMIN SESSIONS
    # -----------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_sessions (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            token_hash TEXT UNIQUE NOT NULL,

            created_at TEXT NOT NULL,

            expires_at TEXT NOT NULL
        )
    """)

    # -----------------------------------------------------
    # INDEXES
    # -----------------------------------------------------

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_devices_user
        ON devices(telegram_user_id)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_requests_user
        ON tts_requests(telegram_user_id)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_requests_installation
        ON tts_requests(installation_id)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_requests_created
        ON tts_requests(created_at)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sessions_expires
        ON admin_sessions(expires_at)
    """)

    conn.commit()

    conn.close()


init_db()


# =========================================================
# WEEK SYSTEM
# =========================================================

def get_current_week():

    now = datetime.now(timezone.utc)

    year, week, _ = now.isocalendar()

    return f"{year}-{week}"


def reset_week_if_needed(user):

    current_week = get_current_week()

    if user["week_id"] != current_week:

        conn = get_db()

        conn.execute("""
            UPDATE users

            SET
                characters_used = 0,
                week_id = ?

            WHERE telegram_user_id = ?
        """, (
            current_week,
            user["telegram_user_id"]
        ))

        conn.commit()

        conn.close()

        return True

    return False


# =========================================================
# GENERATORS
# =========================================================

def generate_code():

    chars = string.ascii_uppercase + string.digits

    return "-".join(
        "".join(
            secrets.choice(chars)
            for _ in range(4)
        )
        for _ in range(3)
    )


def generate_api_key():

    return secrets.token_urlsafe(32)


# =========================================================
# ADMIN SECURITY HELPERS
# =========================================================

def hash_admin_token(token):

    return hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()


def create_admin_session():

    token = secrets.token_urlsafe(48)

    token_hash = hash_admin_token(token)

    created_at = datetime.now(
        timezone.utc
    )

    expires_at = (
        created_at
        + timedelta(
            hours=ADMIN_SESSION_HOURS
        )
    )

    conn = get_db()

    # Remove expired sessions
    conn.execute("""
        DELETE FROM admin_sessions
        WHERE expires_at < ?
    """, (
        created_at.isoformat(),
    ))

    conn.execute("""
        INSERT INTO admin_sessions (
            token_hash,
            created_at,
            expires_at
        )

        VALUES (?, ?, ?)
    """, (
        token_hash,
        created_at.isoformat(),
        expires_at.isoformat()
    ))

    conn.commit()

    conn.close()

    return token, expires_at


def require_admin(
    authorization: str = Header(None)
):

    if not authorization:

        raise HTTPException(
            status_code=401,
            detail="Admin authorization required"
        )

    if not authorization.startswith(
        "Bearer "
    ):

        raise HTTPException(
            status_code=401,
            detail="Invalid admin authorization"
        )

    token = authorization[
        len("Bearer "):
    ].strip()

    if not token:

        raise HTTPException(
            status_code=401,
            detail="Invalid admin session"
        )

    token_hash = hash_admin_token(
        token
    )

    now = datetime.now(
        timezone.utc
    ).isoformat()

    conn = get_db()

    session = conn.execute("""
        SELECT *
        FROM admin_sessions
        WHERE token_hash = ?
          AND expires_at > ?
    """, (
        token_hash,
        now
    )).fetchone()

    conn.close()

    if not session:

        raise HTTPException(
            status_code=401,
            detail="Admin session expired or invalid"
        )

    return session


# =========================================================
# USER HELPERS
# =========================================================

def get_user_by_telegram_id(
    telegram_id
):

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (
        str(telegram_id),
    )).fetchone()

    conn.close()

    return row


def create_or_get_user(
    telegram_id
):

    telegram_id = str(
        telegram_id
    )

    current_week = get_current_week()

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (
        telegram_id,
    )).fetchone()

    if row:

        conn.close()

        return row

    code = generate_code()

    api_key = generate_api_key()

    conn.execute("""
        INSERT INTO users (

            telegram_user_id,
            activation_code,
            api_key,
            characters_used,
            activated,
            week_id,
            code_week_id

        )

        VALUES (?, ?, ?, 0, 0, ?, ?)
    """, (
        telegram_id,
        code,
        api_key,
        current_week,
        current_week
    ))

    conn.commit()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (
        telegram_id,
    )).fetchone()

    conn.close()

    return row


# =========================================================
# DEVICE REGISTRATION
# =========================================================

def register_device(
    telegram_user_id,
    installation_id,
    manufacturer,
    model,
    android_version,
    app_version,
    platform
):

    if not installation_id:

        return

    now = datetime.now(
        timezone.utc
    ).isoformat()

    conn = get_db()

    existing = conn.execute("""
        SELECT id
        FROM devices
        WHERE installation_id = ?
    """, (
        installation_id,
    )).fetchone()

    if existing:

        conn.execute("""
            UPDATE devices

            SET
                telegram_user_id = ?,
                manufacturer = ?,
                model = ?,
                android_version = ?,
                app_version = ?,
                platform = ?,
                last_seen = ?

            WHERE installation_id = ?
        """, (
            telegram_user_id,
            manufacturer,
            model,
            android_version,
            app_version,
            platform or "Android",
            now,
            installation_id
        ))

    else:

        conn.execute("""
            INSERT INTO devices (

                installation_id,
                telegram_user_id,
                manufacturer,
                model,
                android_version,
                app_version,
                platform,
                first_seen,
                last_seen

            )

            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            installation_id,
            telegram_user_id,
            manufacturer,
            model,
            android_version,
            app_version,
            platform or "Android",
            now,
            now
        ))

    conn.commit()

    conn.close()


# =========================================================
# REQUEST LOGGING
# =========================================================

def log_tts_request(
    telegram_user_id,
    installation_id,
    character_count,
    voice,
    rate,
    pitch,
    generation_time_ms,
    file_size_bytes,
    success
):

    now = datetime.now(
        timezone.utc
    ).isoformat()

    conn = get_db()

    conn.execute("""
        INSERT INTO tts_requests (

            telegram_user_id,
            installation_id,
            character_count,
            voice,
            rate,
            pitch,
            generation_time_ms,
            file_size_bytes,
            success,
            created_at

        )

        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        telegram_user_id,
        installation_id,
        character_count,
        voice,
        rate,
        pitch,
        generation_time_ms,
        file_size_bytes,
        1 if success else 0,
        now
    ))

    conn.commit()

    conn.close()


# =========================================================
# TELEGRAM MAIN MENU
# =========================================================

def main_menu():

    keyboard = [

        [
            InlineKeyboardButton(
                "🚀 بدء الاستخدام",
                callback_data="start_use"
            )
        ],

        [
            InlineKeyboardButton(
                "📊 حسابي",
                callback_data="account"
            ),

            InlineKeyboardButton(
                "📱 ربط التطبيق",
                callback_data="link_app"
            )
        ],

        [
            InlineKeyboardButton(
                "ℹ️ المساعدة",
                callback_data="help"
            )
        ]

    ]

    return InlineKeyboardMarkup(
        keyboard
    )


# =========================================================
# TELEGRAM /START
# =========================================================

async def telegram_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    await update.message.reply_text(

        f"""
أهلاً {user.first_name} 👋

مرحبًا بك في Edge TTS.

🎙️ تحويل النص إلى صوت
⚡ سريع
🎁 200,000 حرف أسبوعيًا

اختر من القائمة:
""",

        reply_markup=main_menu()
    )


# =========================================================
# START USE BUTTON
# =========================================================

async def button_start_use(
    query
):

    user = query.from_user

    telegram_id = str(
        user.id
    )

    row = create_or_get_user(
        telegram_id
    )

    reset_week_if_needed(row)

    row = get_user_by_telegram_id(
        telegram_id
    )

    if not row["activated"]:

        keyboard = [

            [
                InlineKeyboardButton(
                    "🎁 الحصول على التفعيل",
                    callback_data="activate_account"
                )
            ],

            [
                InlineKeyboardButton(
                    "📊 حسابي",
                    callback_data="account"
                )
            ],

            [
                InlineKeyboardButton(
                    "⬅️ القائمة الرئيسية",
                    callback_data="home"
                )
            ]

        ]

        await query.edit_message_text(

            """
🎉 حسابك جاهز!

لديك رصيد مجاني:

🎁 200,000 حرف أسبوعيًا

اضغط الزر التالي لتفعيل الحساب.
""",

            reply_markup=InlineKeyboardMarkup(
                keyboard
            )
        )

        return

    await query.edit_message_text(

        """
✅ حسابك مفعل بالفعل.

يمكنك الآن استخدام تطبيق Edge TTS.
""",

        reply_markup=main_menu()
    )


# =========================================================
# ACTIVATE ACCOUNT BUTTON
# =========================================================

async def button_activate_account(
    query
):

    user = query.from_user

    telegram_id = str(
        user.id
    )

    current_week = get_current_week()

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (
        telegram_id,
    )).fetchone()

    if not row:

        conn.close()

        row = create_or_get_user(
            telegram_id
        )

        conn = get_db()

        row = conn.execute("""
            SELECT *
            FROM users
            WHERE telegram_user_id = ?
        """, (
            telegram_id,
        )).fetchone()

    if row["code_week_id"] != current_week:

        code = generate_code()

        conn.execute("""
            UPDATE users

            SET
                activation_code = ?,
                code_week_id = ?,
                activated = 0

            WHERE telegram_user_id = ?
        """, (
            code,
            current_week,
            telegram_id
        ))

        conn.commit()

        row = conn.execute("""
            SELECT *
            FROM users
            WHERE telegram_user_id = ?
        """, (
            telegram_id,
        )).fetchone()

    code = row["activation_code"]

    conn.close()

    keyboard = [

        [
            InlineKeyboardButton(
                "🔑 تفعيل الحساب الآن",
                callback_data="confirm_activation"
            )
        ],

        [
            InlineKeyboardButton(
                "📱 ربط التطبيق",
                callback_data="link_app"
            )
        ],

        [
            InlineKeyboardButton(
                "⬅️ القائمة",
                callback_data="home"
            )
        ]

    ]

    await query.edit_message_text(

        f"""
🎁 تفعيلك المجاني جاهز.

رصيدك:

200,000 حرف أسبوعيًا

🔑 كود التفعيل الخاص بك:

`{code}`

لكن لا تحتاج إلى نسخه يدويًا.

اضغط الزر التالي لإتمام التفعيل.
""",

        parse_mode="Markdown",

        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


# =========================================================
# CONFIRM ACTIVATION
# =========================================================

async def button_confirm_activation(
    query
):

    user = query.from_user

    telegram_id = str(
        user.id
    )

    current_week = get_current_week()

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (
        telegram_id,
    )).fetchone()

    if not row:

        conn.close()

        await query.edit_message_text(
            "❌ لم يتم العثور على حسابك."
        )

        return

    if row["code_week_id"] != current_week:

        conn.close()

        await query.edit_message_text(
            "❌ انتهت صلاحية كود التفعيل."
        )

        return

    api_key = row["api_key"]

    if not api_key:

        api_key = generate_api_key()

    conn.execute("""
        UPDATE users

        SET
            api_key = ?,
            activated = 1,
            characters_used = 0,
            week_id = ?

        WHERE telegram_user_id = ?
    """, (
        api_key,
        current_week,
        telegram_id
    ))

    conn.commit()

    conn.close()

    keyboard = [

        [
            InlineKeyboardButton(
                "📱 ربط التطبيق",
                callback_data="link_app"
            )
        ],

        [
            InlineKeyboardButton(
                "📊 حسابي",
                callback_data="account"
            )
        ],

        [
            InlineKeyboardButton(
                "⬅️ القائمة",
                callback_data="home"
            )
        ]

    ]

    await query.edit_message_text(

        """
✅ تم تفعيل حسابك بنجاح!

🎁 الرصيد الأسبوعي:

200,000 حرف

الخطوة التالية:

اضغط «📱 ربط التطبيق» لربط تطبيق Android بحسابك.
""",

        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


# =========================================================
# ACCOUNT BUTTON
# =========================================================

async def button_account(
    query
):

    telegram_id = str(
        query.from_user.id
    )

    row = get_user_by_telegram_id(
        telegram_id
    )

    if not row:

        await query.edit_message_text(

            """
ليس لديك حساب بعد.

اضغط «🚀 بدء الاستخدام» لإنشاء حسابك.
""",

            reply_markup=main_menu()
        )

        return

    reset_week_if_needed(row)

    row = get_user_by_telegram_id(
        telegram_id
    )

    used = row["characters_used"]

    remaining = max(
        0,
        WEEKLY_LIMIT - used
    )

    status = (
        "✅ مفعل"
        if row["activated"]
        else "⚠️ غير مفعل"
    )

    keyboard = [

        [
            InlineKeyboardButton(
                "📱 ربط التطبيق",
                callback_data="link_app"
            )
        ],

        [
            InlineKeyboardButton(
                "⬅️ القائمة الرئيسية",
                callback_data="home"
            )
        ]

    ]

    await query.edit_message_text(

        f"""
📊 حسابك

الحالة:
{status}

🎁 الحد الأسبوعي:
{WEEKLY_LIMIT:,} حرف

📝 المستخدم:
{used:,} حرف

💰 المتبقي:
{remaining:,} حرف

📅 التجديد:
أسبوعيًا
""",

        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


# =========================================================
# LINK APP BUTTON
# =========================================================

async def button_link_app(
    query
):

    await query.edit_message_text(

        """
📱 ربط تطبيق Android

هذه الصفحة جاهزة للربط التلقائي.

في النسخة التالية سنضيف رابط Telegram Deep Link بحيث تضغط زرًا واحدًا وينتقل الربط مباشرة إلى التطبيق.

لا تحتاج إلى نسخ API Key.
""",

        reply_markup=InlineKeyboardMarkup([

            [
                InlineKeyboardButton(
                    "⬅️ القائمة الرئيسية",
                    callback_data="home"
                )
            ]

        ])
    )


# =========================================================
# HELP BUTTON
# =========================================================

async def button_help(
    query
):

    await query.edit_message_text(

        """
ℹ️ طريقة الاستخدام

1️⃣ افتح التطبيق.

2️⃣ اربط حساب Telegram.

3️⃣ اكتب النص.

4️⃣ اختر الصوت والسرعة والنغمة.

5️⃣ اضغط إنشاء الصوت.

🎙️ سيتم تحويل النص إلى MP3.

🎁 الحد المجاني:
200,000 حرف أسبوعيًا.

🔒 لا يتم حفظ نصوصك على السيرفر.
""",

        reply_markup=InlineKeyboardMarkup([

            [
                InlineKeyboardButton(
                    "⬅️ القائمة الرئيسية",
                    callback_data="home"
                )
            ]

        ])
    )


# =========================================================
# HOME BUTTON
# =========================================================

async def button_home(
    query
):

    user = query.from_user

    await query.edit_message_text(

        f"""
أهلاً {user.first_name} 👋

مرحبًا بك في Edge TTS.

🎙️ تحويل النص إلى صوت
⚡ سريع
🎁 200,000 حرف أسبوعيًا

اختر من القائمة:
""",

        reply_markup=main_menu()
    )


# =========================================================
# CALLBACK ROUTER
# =========================================================

async def telegram_button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    action = query.data

    if action == "start_use":

        await button_start_use(
            query
        )

    elif action == "activate_account":

        await button_activate_account(
            query
        )

    elif action == "confirm_activation":

        await button_confirm_activation(
            query
        )

    elif action == "account":

        await button_account(
            query
        )

    elif action == "link_app":

        await button_link_app(
            query
        )

    elif action == "help":

        await button_help(
            query
        )

    elif action == "home":

        await button_home(
            query
        )


# =========================================================
# LEGACY /TRIAL
# =========================================================

async def telegram_trial(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    telegram_id = str(
        user.id
    )

    current_week = get_current_week()

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (
        telegram_id,
    )).fetchone()

    if row:

        if row["code_week_id"] != current_week:

            code = generate_code()

            conn.execute("""
                UPDATE users

                SET
                    activation_code = ?,
                    code_week_id = ?,
                    activated = 0

                WHERE telegram_user_id = ?
            """, (
                code,
                current_week,
                telegram_id
            ))

            conn.commit()

            conn.close()

            await update.message.reply_text(

                f"""
🎁 الكود المجاني الأسبوعي الجديد:

`{code}`

💰 الرصيد:
200,000 حرف

📅 هذا الكود متاح لهذا الأسبوع فقط.
""",

                parse_mode="Markdown"
            )

            return

        conn.close()

        await update.message.reply_text(

            """
⚠️ لقد حصلت بالفعل على كودك المجاني هذا الأسبوع.

🎁 كل حساب يحصل على 200,000 حرف أسبوعيًا.
"""

        )

        return

    code = generate_code()

    api_key = generate_api_key()

    conn.execute("""
        INSERT INTO users (

            telegram_user_id,
            activation_code,
            api_key,
            characters_used,
            activated,
            week_id,
            code_week_id

        )

        VALUES (?, ?, ?, 0, 0, ?, ?)
    """, (
        telegram_id,
        code,
        api_key,
        current_week,
        current_week
    ))

    conn.commit()

    conn.close()

    await update.message.reply_text(

        f"""
🎉 تم إنشاء حسابك المجاني!

🎁 رصيدك الأسبوعي:

200,000 حرف

🔑 كود التفعيل:

`{code}`

يمكنك الآن فتح التطبيق واستخدام الكود.

أو استخدم القائمة الرئيسية للتفعيل بسهولة.
""",

        parse_mode="Markdown",

        reply_markup=main_menu()
    )


# =========================================================
# LEGACY TELEGRAM /ACTIVATE
# =========================================================

async def telegram_activate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:

        await update.message.reply_text(

            """
⚠️ اكتب كود التفعيل بعد الأمر.

مثال:

/activate ABCD-EF12-3456
"""

        )

        return

    code = context.args[0].strip().upper()

    current_week = get_current_week()

    conn = get_db()

    user = conn.execute("""
        SELECT *
        FROM users
        WHERE activation_code = ?
    """, (
        code,
    )).fetchone()

    if not user:

        conn.close()

        await update.message.reply_text(
            "❌ كود التفعيل غير صحيح."
        )

        return

    if user["code_week_id"] != current_week:

        conn.close()

        await update.message.reply_text(
            "❌ انتهت صلاحية كود التفعيل."
        )

        return

    api_key = user["api_key"]

    if not api_key:

        api_key = generate_api_key()

    conn.execute("""
        UPDATE users

        SET
            api_key = ?,
            activated = 1,
            characters_used = 0,
            week_id = ?

        WHERE telegram_user_id = ?
    """, (
        api_key,
        current_week,
        user["telegram_user_id"]
    ))

    conn.commit()

    conn.close()

    await update.message.reply_text(

        """
✅ تم تفعيل حسابك بنجاح!

🎁 الرصيد:
200,000 حرف أسبوعيًا

📱 استخدم تطبيق Edge TTS الآن.
""",

        reply_markup=main_menu()
    )


# =========================================================
# LEGACY TELEGRAM /ACCOUNT
# =========================================================

async def telegram_account(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    telegram_id = str(
        update.effective_user.id
    )

    row = get_user_by_telegram_id(
        telegram_id
    )

    if not row:

        await update.message.reply_text(

            "ليس لديك حساب بعد.\n\nاستخدم /start",

            reply_markup=main_menu()
        )

        return

    reset_week_if_needed(row)

    row = get_user_by_telegram_id(
        telegram_id
    )

    used = row["characters_used"]

    remaining = max(
        0,
        WEEKLY_LIMIT - used
    )

    status = (
        "✅ مفعل"
        if row["activated"]
        else "⚠️ غير مفعل"
    )

    await update.message.reply_text(

        f"""
📊 حسابك

الحالة:
{status}

🎁 الحد الأسبوعي:
{WEEKLY_LIMIT:,} حرف

📝 المستخدم:
{used:,} حرف

💰 المتبقي:
{remaining:,} حرف
""",

        reply_markup=main_menu()
    )


# =========================================================
# TELEGRAM BOT STARTUP
# =========================================================

telegram_app = None

telegram_starting = False

telegram_running = False


async def start_telegram_bot():

    global telegram_app
    global telegram_starting
    global telegram_running

    # -----------------------------------------------------
    # PREVENT DUPLICATE START IN SAME PROCESS
    # -----------------------------------------------------

    if telegram_starting or telegram_running:

        print(
            "Telegram bot is already starting/running"
        )

        return

    if not TELEGRAM_BOT_TOKEN:

        print(
            "Telegram bot disabled: "
            "TELEGRAM_BOT_TOKEN missing"
        )

        return

    telegram_starting = True

    try:

        telegram_app = (
            Application.builder()
            .token(
                TELEGRAM_BOT_TOKEN
            )
            .build()
        )

        # -------------------------------------------------
        # COMMANDS
        # -------------------------------------------------

        telegram_app.add_handler(
            CommandHandler(
                "start",
                telegram_start
            )
        )

        telegram_app.add_handler(
            CommandHandler(
                "trial",
                telegram_trial
            )
        )

        telegram_app.add_handler(
            CommandHandler(
                "activate",
                telegram_activate
            )
        )

        telegram_app.add_handler(
            CommandHandler(
                "account",
                telegram_account
            )
        )

        # -------------------------------------------------
        # BUTTONS
        # -------------------------------------------------

        telegram_app.add_handler(
            CallbackQueryHandler(
                telegram_button_handler
            )
        )

        await telegram_app.initialize()

        await telegram_app.start()

        try:

            await telegram_app.updater.start_polling()

            telegram_running = True

            print(
                "Telegram bot started"
            )

        except Exception as e:

            error_text = str(e)

            if (
                "Conflict"
                in error_text
                or "getUpdates"
                in error_text
            ):

                print(
                    "Telegram polling conflict detected."
                )

                print(
                    "Another bot instance is using "
                    "the same TELEGRAM_BOT_TOKEN."
                )

                print(
                    "FastAPI will continue running."
                )

                try:

                    await telegram_app.updater.stop()

                except Exception:

                    pass

                try:

                    await telegram_app.stop()

                except Exception:

                    pass

                try:

                    await telegram_app.shutdown()

                except Exception:

                    pass

                telegram_app = None

                telegram_running = False

            else:

                print(
                    "Telegram startup error:",
                    repr(e)
                )

                try:

                    await telegram_app.stop()

                except Exception:

                    pass

                try:

                    await telegram_app.shutdown()

                except Exception:

                    pass

                telegram_app = None

                telegram_running = False

    except Exception as e:

        print(
            "Telegram bot initialization error:",
            repr(e)
        )

        telegram_app = None

        telegram_running = False

    finally:

        telegram_starting = False


# =========================================================
# FASTAPI STARTUP
# =========================================================

@app.on_event("startup")
async def startup_event():

    asyncio.create_task(
        start_telegram_bot()
    )


# =========================================================
# MODELS
# =========================================================

class TTSRequest(BaseModel):

    text: str

    voice: str = "ar-EG-SalmaNeural"

    rate: str = "+0%"

    pitch: str = "+0Hz"

    installation_id: str | None = None

    manufacturer: str | None = None

    model: str | None = None

    android_version: str | None = None

    app_version: str | None = None

    platform: str = "Android"


class ActivateRequest(BaseModel):

    code: str


class AdminLoginRequest(BaseModel):

    username: str

    password: str


# =========================================================
# HOME API
# =========================================================

@app.get("/")
async def home():

    return {

        "status": "online",

        "service": "Edge TTS API",

        "weekly_limit": WEEKLY_LIMIT
    }


# =========================================================
# ACTIVATE API
# =========================================================

@app.post("/activate")
async def activate(
    data: ActivateRequest
):

    code = data.code.strip().upper()

    if not code:

        raise HTTPException(
            status_code=400,
            detail="Activation code is required"
        )

    conn = get_db()

    user = conn.execute("""
        SELECT *
        FROM users
        WHERE activation_code = ?
    """, (
        code,
    )).fetchone()

    if not user:

        conn.close()

        raise HTTPException(
            status_code=404,
            detail="Invalid activation code"
        )

    current_week = get_current_week()

    if user["code_week_id"] != current_week:

        conn.close()

        raise HTTPException(
            status_code=400,
            detail="Activation code expired"
        )

    api_key = user["api_key"]

    if not api_key:

        api_key = generate_api_key()

    conn.execute("""
        UPDATE users

        SET
            api_key = ?,
            activated = 1,
            characters_used = 0,
            week_id = ?

        WHERE telegram_user_id = ?
    """, (
        api_key,
        current_week,
        user["telegram_user_id"]
    ))

    conn.commit()

    conn.close()

    return {

        "success": True,

        "message": "Account activated",

        "api_key": api_key,

        "weekly_limit": WEEKLY_LIMIT,

        "characters_used": 0,

        "characters_remaining": WEEKLY_LIMIT
    }


# =========================================================
# ACCOUNT API
# =========================================================

@app.get("/account")
async def account(
    authorization: str = Header(None)
):

    if not authorization:

        raise HTTPException(
            status_code=401,
            detail="Authorization required"
        )

    if not authorization.startswith(
        "Bearer "
    ):

        raise HTTPException(
            status_code=401,
            detail="Invalid authorization"
        )

    api_key = authorization.replace(
        "Bearer ",
        "",
        1
    ).strip()

    conn = get_db()

    user = conn.execute("""
        SELECT *
        FROM users
        WHERE api_key = ?
    """, (
        api_key,
    )).fetchone()

    conn.close()

    if not user:

        raise HTTPException(
            status_code=401,
            detail="Invalid API key"
        )

    reset_week_if_needed(user)

    user = get_user_by_telegram_id(
        user["telegram_user_id"]
    )

    used = user["characters_used"]

    remaining = max(
        0,
        WEEKLY_LIMIT - used
    )

    return {

        "success": True,

        "weekly_limit": WEEKLY_LIMIT,

        "characters_used": used,

        "characters_remaining": remaining,

        "week": get_current_week()
    }


# =========================================================
# TTS API
# =========================================================

@app.post("/tts")
async def tts(
    data: TTSRequest,
    authorization: str = Header(None)
):

    # -----------------------------------------------------
    # AUTH
    # -----------------------------------------------------

    if not authorization:

        raise HTTPException(
            status_code=401,
            detail="Authorization required"
        )

    if not authorization.startswith(
        "Bearer "
    ):

        raise HTTPException(
            status_code=401,
            detail="Invalid authorization"
        )

    api_key = authorization.replace(
        "Bearer ",
        "",
        1
    ).strip()

    conn = get_db()

    user = conn.execute("""
        SELECT *
        FROM users
        WHERE api_key = ?
    """, (
        api_key,
    )).fetchone()

    conn.close()

    if not user:

        raise HTTPException(
            status_code=401,
            detail="Invalid API key"
        )

    if not user["activated"]:

        raise HTTPException(
            status_code=403,
            detail="Account is not activated"
        )

    # -----------------------------------------------------
    # WEEK RESET
    # -----------------------------------------------------

    reset_week_if_needed(user)

    user = get_user_by_telegram_id(
        user["telegram_user_id"]
    )

    # -----------------------------------------------------
    # TEXT VALIDATION
    # -----------------------------------------------------

    if not data.text or not data.text.strip():

        raise HTTPException(
            status_code=400,
            detail="Text is empty"
        )

    character_count = len(data.text)

    # -----------------------------------------------------
    # QUOTA CHECK
    # -----------------------------------------------------

    remaining = (
        WEEKLY_LIMIT -
        user["characters_used"]
    )

    if character_count > remaining:

        raise HTTPException(

            status_code=429,

            detail={

                "message":
                    "Weekly character limit exceeded",

                "weekly_limit":
                    WEEKLY_LIMIT,

                "characters_used":
                    user["characters_used"],

                "characters_remaining":
                    remaining,

                "requested":
                    character_count
            }
        )

    # -----------------------------------------------------
    # DEVICE
    # -----------------------------------------------------

    register_device(

        telegram_user_id=
            user["telegram_user_id"],

        installation_id=
            data.installation_id,

        manufacturer=
            data.manufacturer,

        model=
            data.model,

        android_version=
            data.android_version,

        app_version=
            data.app_version,

        platform=
            data.platform
    )

    # -----------------------------------------------------
    # TEMP FILE
    # -----------------------------------------------------

    temp_file = tempfile.NamedTemporaryFile(
        suffix=".mp3",
        delete=False
    )

    temp_file.close()

    start_time = time.perf_counter()

    try:

        communicate = edge_tts.Communicate(

            text=data.text,

            voice=data.voice,

            rate=data.rate,

            pitch=data.pitch
        )

        await communicate.save(
            temp_file.name
        )

        with open(
            temp_file.name,
            "rb"
        ) as f:

            audio = f.read()

        generation_time_ms = int(

            (
                time.perf_counter()
                - start_time
            ) * 1000
        )

        file_size_bytes = len(
            audio
        )

        # -------------------------------------------------
        # DEDUCT
        # -------------------------------------------------

        conn = get_db()

        conn.execute("""
            UPDATE users

            SET characters_used =
                characters_used + ?

            WHERE api_key = ?
        """, (
            character_count,
            api_key
        ))

        conn.commit()

        conn.close()

        # -------------------------------------------------
        # LOG
        # -------------------------------------------------

        log_tts_request(

            telegram_user_id=
                user["telegram_user_id"],

            installation_id=
                data.installation_id,

            character_count=
                character_count,

            voice=
                data.voice,

            rate=
                data.rate,

            pitch=
                data.pitch,

            generation_time_ms=
                generation_time_ms,

            file_size_bytes=
                file_size_bytes,

            success=True
        )

        return Response(

            content=audio,

            media_type="audio/mpeg",

            headers={

                "Content-Disposition":
                    'attachment; filename="speech.mp3"'
            }
        )

    except Exception as e:

        generation_time_ms = int(

            (
                time.perf_counter()
                - start_time
            ) * 1000
        )

        log_tts_request(

            telegram_user_id=
                user["telegram_user_id"],

            installation_id=
                data.installation_id,

            character_count=
                character_count,

            voice=
                data.voice,

            rate=
                data.rate,

            pitch=
                data.pitch,

            generation_time_ms=
                generation_time_ms,

            file_size_bytes=0,

            success=False
        )

        raise HTTPException(

            status_code=500,

            detail=str(e)
        )

    finally:

        if os.path.exists(
            temp_file.name
        ):

            os.remove(
                temp_file.name
            )


# =========================================================
# =========================================================
# ADMIN API
# =========================================================
# =========================================================


# =========================================================
# ADMIN LOGIN
# =========================================================

@app.post("/admin/login")
async def admin_login(
    data: AdminLoginRequest
):

    if not ADMIN_PASSWORD:

        raise HTTPException(
            status_code=503,
            detail="Admin login is not configured"
        )

    if not secrets.compare_digest(
        data.username,
        ADMIN_USERNAME
    ):

        raise HTTPException(
            status_code=401,
            detail="Invalid admin credentials"
        )

    if not secrets.compare_digest(
        data.password,
        ADMIN_PASSWORD
    ):

        raise HTTPException(
            status_code=401,
            detail="Invalid admin credentials"
        )

    token, expires_at = (
        create_admin_session()
    )

    return {

        "success": True,

        "token": token,

        "expires_at":
            expires_at.isoformat(),

        "session_hours":
            ADMIN_SESSION_HOURS
    }


# =========================================================
# ADMIN LOGOUT
# =========================================================

@app.post("/admin/logout")
async def admin_logout(
    authorization: str = Header(None)
):

    if not authorization:

        return {
            "success": True
        }

    if not authorization.startswith(
        "Bearer "
    ):

        return {
            "success": True
        }

    token = authorization[
        len("Bearer "):
    ].strip()

    if token:

        token_hash = hash_admin_token(
            token
        )

        conn = get_db()

        conn.execute("""
            DELETE FROM admin_sessions
            WHERE token_hash = ?
        """, (
            token_hash,
        ))

        conn.commit()

        conn.close()

    return {

        "success": True,

        "message": "Admin logged out"
    }


# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.get("/admin/dashboard")
async def admin_dashboard(
    authorization: str = Header(None)
):

    require_admin(
        authorization
    )

    conn = get_db()

    # -----------------------------------------------------
    # USERS
    # -----------------------------------------------------

    total_users = conn.execute("""
        SELECT COUNT(*)
        FROM users
    """).fetchone()[0]

    activated_users = conn.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE activated = 1
    """).fetchone()[0]

    disabled_users = conn.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE activated = 0
    """).fetchone()[0]

    # -----------------------------------------------------
    # DEVICES
    # -----------------------------------------------------

    total_devices = conn.execute("""
        SELECT COUNT(*)
        FROM devices
    """).fetchone()[0]

    # -----------------------------------------------------
    # REQUESTS
    # -----------------------------------------------------

    total_requests = conn.execute("""
        SELECT COUNT(*)
        FROM tts_requests
    """).fetchone()[0]

    successful_requests = conn.execute("""
        SELECT COUNT(*)
        FROM tts_requests
        WHERE success = 1
    """).fetchone()[0]

    failed_requests = conn.execute("""
        SELECT COUNT(*)
        FROM tts_requests
        WHERE success = 0
    """).fetchone()[0]

    total_characters = conn.execute("""
        SELECT COALESCE(
            SUM(character_count),
            0
        )
        FROM tts_requests
        WHERE success = 1
    """).fetchone()[0]

    # -----------------------------------------------------
    # LAST ACTIVITY
    # -----------------------------------------------------

    last_tts = conn.execute("""
        SELECT created_at
        FROM tts_requests
        ORDER BY id DESC
        LIMIT 1
    """).fetchone()

    last_device = conn.execute("""
        SELECT last_seen
        FROM devices
        WHERE last_seen IS NOT NULL
        ORDER BY last_seen DESC
        LIMIT 1
    """).fetchone()

    activity_values = []

    if last_tts:

        activity_values.append(
            last_tts["created_at"]
        )

    if last_device:

        activity_values.append(
            last_device["last_seen"]
        )

    last_activity = None

    if activity_values:

        last_activity = max(
            activity_values
        )

    # -----------------------------------------------------
    # WEEK
    # -----------------------------------------------------

    current_week = get_current_week()

    conn.close()

    return {

        "success": True,

        "server": {

            "status": "online",

            "current_week":
                current_week,

            "weekly_limit":
                WEEKLY_LIMIT
        },

        "users": {

            "total":
                total_users,

            "activated":
                activated_users,

            "disabled":
                disabled_users
        },

        "devices": {

            "total":
                total_devices
        },

        "tts": {

            "total_requests":
                total_requests,

            "successful":
                successful_requests,

            "failed":
                failed_requests,

            "characters":
                total_characters
        },

        "last_activity":
            last_activity
    }


# =========================================================
# MASK API KEY
# =========================================================

def mask_api_key(
    api_key
):

    if not api_key:

        return None

    if len(api_key) <= 10:

        return "********"

    return (
        api_key[:6]
        + "..."
        + api_key[-4:]
    )


# =========================================================
# ADMIN USER SERIALIZER
# =========================================================

def serialize_user(
    row,
    conn
):

    telegram_id = row[
        "telegram_user_id"
    ]

    device_count = conn.execute("""
        SELECT COUNT(*)
        FROM devices
        WHERE telegram_user_id = ?
    """, (
        telegram_id,
    )).fetchone()[0]

    last_device = conn.execute("""
        SELECT last_seen
        FROM devices
        WHERE telegram_user_id = ?
        ORDER BY last_seen DESC
        LIMIT 1
    """, (
        telegram_id,
    )).fetchone()

    last_tts = conn.execute("""
        SELECT created_at
        FROM tts_requests
        WHERE telegram_user_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (
        telegram_id,
    )).fetchone()

    activity_values = []

    if last_device and last_device["last_seen"]:

        activity_values.append(
            last_device["last_seen"]
        )

    if last_tts and last_tts["created_at"]:

        activity_values.append(
            last_tts["created_at"]
        )

    last_activity = None

    if activity_values:

        last_activity = max(
            activity_values
        )

    used = row["characters_used"]

    remaining = max(
        0,
        WEEKLY_LIMIT - used
    )

    return {

        "id":
            row["id"],

        "telegram_user_id":
            telegram_id,

        "activated":
            bool(row["activated"]),

        "status":
            "enabled"
            if row["activated"]
            else "disabled",

        "activation_code":
            row["activation_code"],

        "api_key":
            mask_api_key(
                row["api_key"]
            ),

        "characters_used":
            used,

        "characters_remaining":
            remaining,

        "weekly_limit":
            WEEKLY_LIMIT,

        "week_id":
            row["week_id"],

        "code_week_id":
            row["code_week_id"],

        "devices_count":
            device_count,

        "last_activity":
            last_activity
    }


# =========================================================
# ADMIN USERS LIST
# =========================================================

@app.get("/admin/users")
async def admin_users(
    authorization: str = Header(None),
    search: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200)
):

    require_admin(
        authorization
    )

    conn = get_db()

    conditions = []

    params = []

    # -----------------------------------------------------
    # SEARCH
    # -----------------------------------------------------

    if search:

        conditions.append("""
            telegram_user_id LIKE ?
        """)

        params.append(
            "%" + search.strip() + "%"
        )

    # -----------------------------------------------------
    # STATUS
    # -----------------------------------------------------

    if status == "enabled":

        conditions.append(
            "activated = 1"
        )

    elif status == "disabled":

        conditions.append(
            "activated = 0"
        )

    where_clause = ""

    if conditions:

        where_clause = (
            "WHERE "
            + " AND ".join(
                conditions
            )
        )

    total = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM users
        {where_clause}
        """,
        params
    ).fetchone()[0]

    offset = (
        page - 1
    ) * limit

    rows = conn.execute(
        f"""
        SELECT *
        FROM users
        {where_clause}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        params + [
            limit,
            offset
        ]
    ).fetchall()

    users = []

    for row in rows:

        reset_week_if_needed(row)

    # Re-read after potential weekly reset
    rows = conn.execute(
        f"""
        SELECT *
        FROM users
        {where_clause}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        params + [
            limit,
            offset
        ]
    ).fetchall()

    for row in rows:

        users.append(
            serialize_user(
                row,
                conn
            )
        )

    conn.close()

    pages = (
        (
            total
            + limit
            - 1
        )
        // limit
        if total
        else 0
    )

    return {

        "success": True,

        "page":
            page,

        "limit":
            limit,

        "total":
            total,

        "pages":
            pages,

        "users":
            users
    }


# =========================================================
# ADMIN USER DETAILS
# =========================================================

@app.get(
    "/admin/users/{telegram_user_id}"
)
async def admin_user_details(
    telegram_user_id: str,
    authorization: str = Header(None)
):

    require_admin(
        authorization
    )

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (
        telegram_user_id,
    )).fetchone()

    if not row:

        conn.close()

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    reset_week_if_needed(row)

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (
        telegram_user_id,
    )).fetchone()

    user = serialize_user(
        row,
        conn
    )

    # -----------------------------------------------------
    # REQUEST SUMMARY
    # -----------------------------------------------------

    request_summary = conn.execute("""
        SELECT

            COUNT(*) AS total,

            COALESCE(
                SUM(
                    CASE
                    WHEN success = 1
                    THEN 1
                    ELSE 0
                    END
                ),
                0
            ) AS successful,

            COALESCE(
                SUM(
                    CASE
                    WHEN success = 0
                    THEN 1
                    ELSE 0
                    END
                ),
                0
            ) AS failed,

            COALESCE(
                SUM(character_count),
                0
            ) AS characters

        FROM tts_requests

        WHERE telegram_user_id = ?
    """, (
        telegram_user_id,
    )).fetchone()

    # -----------------------------------------------------
    # LAST ACTIVITY
    # -----------------------------------------------------

    last_activity = conn.execute("""
        SELECT created_at
        FROM tts_requests
        WHERE telegram_user_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (
        telegram_user_id,
    )).fetchone()

    # -----------------------------------------------------
    # DEVICES
    # -----------------------------------------------------

    devices = conn.execute("""
        SELECT *

        FROM devices

        WHERE telegram_user_id = ?

        ORDER BY last_seen DESC
    """, (
        telegram_user_id,
    )).fetchall()

    device_list = []

    for device in devices:

        device_list.append({

            "id":
                device["id"],

            "installation_id":
                device["installation_id"],

            "manufacturer":
                device["manufacturer"],

            "model":
                device["model"],

            "android_version":
                device["android_version"],

            "app_version":
                device["app_version"],

            "platform":
                device["platform"],

            "first_seen":
                device["first_seen"],

            "last_seen":
                device["last_seen"]
        })

    conn.close()

    return {

        "success": True,

        "user":
            user,

        "requests": {

            "total":
                request_summary["total"],

            "successful":
                request_summary["successful"],

            "failed":
                request_summary["failed"],

            "characters":
                request_summary["characters"]
        },

        "last_tts_activity":
            last_activity["created_at"]
            if last_activity
            else None,

        "devices":
            device_list
    }


# =========================================================
# ADMIN ENABLE USER
# =========================================================

@app.post(
    "/admin/users/{telegram_user_id}/enable"
)
async def admin_enable_user(
    telegram_user_id: str,
    authorization: str = Header(None)
):

    require_admin(
        authorization
    )

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (
        telegram_user_id,
    )).fetchone()

    if not row:

        conn.close()

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    conn.execute("""
        UPDATE users

        SET activated = 1

        WHERE telegram_user_id = ?
    """, (
        telegram_user_id,
    ))

    conn.commit()

    conn.close()

    return {

        "success": True,

        "message":
            "User enabled",

        "telegram_user_id":
            telegram_user_id,

        "activated":
            True
    }


# =========================================================
# ADMIN DISABLE USER
# =========================================================

@app.post(
    "/admin/users/{telegram_user_id}/disable"
)
async def admin_disable_user(
    telegram_user_id: str,
    authorization: str = Header(None)
):

    require_admin(
        authorization
    )

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (
        telegram_user_id,
    )).fetchone()

    if not row:

        conn.close()

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    conn.execute("""
        UPDATE users

        SET activated = 0

        WHERE telegram_user_id = ?
    """, (
        telegram_user_id,
    ))

    conn.commit()

    conn.close()

    return {

        "success": True,

        "message":
            "User disabled",

        "telegram_user_id":
            telegram_user_id,

        "activated":
            False
    }


# =========================================================
# ADMIN RESET USAGE
# =========================================================

@app.post(
    "/admin/users/{telegram_user_id}/reset-usage"
)
async def admin_reset_usage(
    telegram_user_id: str,
    authorization: str = Header(None)
):

    require_admin(
        authorization
    )

    current_week = get_current_week()

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (
        telegram_user_id,
    )).fetchone()

    if not row:

        conn.close()

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    conn.execute("""
        UPDATE users

        SET
            characters_used = 0,
            week_id = ?

        WHERE telegram_user_id = ?
    """, (
        current_week,
        telegram_user_id
    ))

    conn.commit()

    conn.close()

    return {

        "success": True,

        "message":
            "User usage reset",

        "telegram_user_id":
            telegram_user_id,

        "characters_used":
            0,

        "characters_remaining":
            WEEKLY_LIMIT,

        "week":
            current_week
    }


# =========================================================
# ADMIN REGENERATE API KEY
# =========================================================

@app.post(
    "/admin/users/{telegram_user_id}/regenerate-key"
)
async def admin_regenerate_key(
    telegram_user_id: str,
    authorization: str = Header(None)
):

    require_admin(
        authorization
    )

    new_api_key = generate_api_key()

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (
        telegram_user_id,
    )).fetchone()

    if not row:

        conn.close()

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    conn.execute("""
        UPDATE users

        SET api_key = ?

        WHERE telegram_user_id = ?
    """, (
        new_api_key,
        telegram_user_id
    ))

    conn.commit()

    conn.close()

    return {

        "success": True,

        "message":
            "API key regenerated",

        "telegram_user_id":
            telegram_user_id,

        "api_key":
            new_api_key,

        "warning":
            "Save this API key now. "
            "It is only returned in full by this operation."
    }


# =========================================================
# ADMIN RENEW ACTIVATION CODE
# =========================================================

@app.post(
    "/admin/users/{telegram_user_id}/renew-code"
)
async def admin_renew_code(
    telegram_user_id: str,
    authorization: str = Header(None)
):

    require_admin(
        authorization
    )

    current_week = get_current_week()

    new_code = generate_code()

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (
        telegram_user_id,
    )).fetchone()

    if not row:

        conn.close()

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    conn.execute("""
        UPDATE users

        SET
            activation_code = ?,
            code_week_id = ?

        WHERE telegram_user_id = ?
    """, (
        new_code,
        current_week,
        telegram_user_id
    ))

    conn.commit()

    conn.close()

    return {

        "success": True,

        "message":
            "Activation code renewed",

        "telegram_user_id":
            telegram_user_id,

        "activation_code":
            new_code,

        "week":
            current_week
    }


# =========================================================
# ADMIN DEVICES
# =========================================================

@app.get(
    "/admin/users/{telegram_user_id}/devices"
)
async def admin_user_devices(
    telegram_user_id: str,
    authorization: str = Header(None)
):

    require_admin(
        authorization
    )

    conn = get_db()

    user = conn.execute("""
        SELECT id
        FROM users
        WHERE telegram_user_id = ?
    """, (
        telegram_user_id,
    )).fetchone()

    if not user:

        conn.close()

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    rows = conn.execute("""
        SELECT *

        FROM devices

        WHERE telegram_user_id = ?

        ORDER BY last_seen DESC
    """, (
        telegram_user_id,
    )).fetchall()

    devices = []

    for row in rows:

        devices.append({

            "id":
                row["id"],

            "installation_id":
                row["installation_id"],

            "manufacturer":
                row["manufacturer"],

            "model":
                row["model"],

            "android_version":
                row["android_version"],

            "app_version":
                row["app_version"],

            "platform":
                row["platform"],

            "first_seen":
                row["first_seen"],

            "last_seen":
                row["last_seen"]
        })

    conn.close()

    return {

        "success": True,

        "telegram_user_id":
            telegram_user_id,

        "count":
            len(devices),

        "devices":
            devices
    }


# =========================================================
# ADMIN USER TTS REQUESTS
# =========================================================

@app.get(
    "/admin/users/{telegram_user_id}/requests"
)
async def admin_user_requests(
    telegram_user_id: str,
    authorization: str = Header(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200)
):

    require_admin(
        authorization
    )

    conn = get_db()

    user = conn.execute("""
        SELECT id
        FROM users
        WHERE telegram_user_id = ?
    """, (
        telegram_user_id,
    )).fetchone()

    if not user:

        conn.close()

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    total = conn.execute("""
        SELECT COUNT(*)
        FROM tts_requests
        WHERE telegram_user_id = ?
    """, (
        telegram_user_id,
    )).fetchone()[0]

    offset = (
        page - 1
    ) * limit

    rows = conn.execute("""
        SELECT *

        FROM tts_requests

        WHERE telegram_user_id = ?

        ORDER BY id DESC

        LIMIT ? OFFSET ?
    """, (
        telegram_user_id,
        limit,
        offset
    )).fetchall()

    requests = []

    for row in rows:

        requests.append({

            "id":
                row["id"],

            "installation_id":
                row["installation_id"],

            "character_count":
                row["character_count"],

            "voice":
                row["voice"],

            "rate":
                row["rate"],

            "pitch":
                row["pitch"],

            "generation_time_ms":
                row["generation_time_ms"],

            "file_size_bytes":
                row["file_size_bytes"],

            "success":
                bool(row["success"]),

            "created_at":
                row["created_at"]
        })

    conn.close()

    pages = (
        (
            total
            + limit
            - 1
        )
        // limit
        if total
        else 0
    )

    return {

        "success": True,

        "telegram_user_id":
            telegram_user_id,

        "page":
            page,

        "limit":
            limit,

        "total":
            total,

        "pages":
            pages,

        "requests":
            requests
    }


# =========================================================
# ADMIN ALL TTS REQUESTS
# =========================================================

@app.get(
    "/admin/requests"
)
async def admin_requests(
    authorization: str = Header(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    success: int | None = Query(None)
):

    require_admin(
        authorization
    )

    conn = get_db()

    conditions = []

    params = []

    if success is not None:

        if success not in (0, 1):

            conn.close()

            raise HTTPException(
                status_code=400,
                detail="success must be 0 or 1"
            )

        conditions.append(
            "success = ?"
        )

        params.append(
            success
        )

    where_clause = ""

    if conditions:

        where_clause = (
            "WHERE "
            + " AND ".join(
                conditions
            )
        )

    total = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM tts_requests
        {where_clause}
        """,
        params
    ).fetchone()[0]

    offset = (
        page - 1
    ) * limit

    rows = conn.execute(
        f"""
        SELECT *

        FROM tts_requests

        {where_clause}

        ORDER BY id DESC

        LIMIT ? OFFSET ?
        """,
        params + [
            limit,
            offset
        ]
    ).fetchall()

    requests = []

    for row in rows:

        requests.append({

            "id":
                row["id"],

            "telegram_user_id":
                row["telegram_user_id"],

            "installation_id":
                row["installation_id"],

            "character_count":
                row["character_count"],

            "voice":
                row["voice"],

            "rate":
                row["rate"],

            "pitch":
                row["pitch"],

            "generation_time_ms":
                row["generation_time_ms"],

            "file_size_bytes":
                row["file_size_bytes"],

            "success":
                bool(row["success"]),

            "created_at":
                row["created_at"]
        })

    conn.close()

    pages = (
        (
            total
            + limit
            - 1
        )
        // limit
        if total
        else 0
    )

    return {

        "success": True,

        "page":
            page,

        "limit":
            limit,

        "total":
            total,

        "pages":
            pages,

        "requests":
            requests
    }


# =========================================================
# ADMIN CLEAN EXPIRED SESSIONS
# =========================================================

@app.post(
    "/admin/cleanup-sessions"
)
async def admin_cleanup_sessions(
    authorization: str = Header(None)
):

    require_admin(
        authorization
    )

    now = datetime.now(
        timezone.utc
    ).isoformat()

    conn = get_db()

    cursor = conn.execute("""
        DELETE FROM admin_sessions
        WHERE expires_at < ?
    """, (
        now,
    ))

    deleted = cursor.rowcount

    conn.commit()

    conn.close()

    return {

        "success": True,

        "deleted_sessions":
            deleted
    }


# =========================================================
# ADMIN HEALTH
# =========================================================

@app.get(
    "/admin/health"
)
async def admin_health(
    authorization: str = Header(None)
):

    require_admin(
        authorization
    )

    return {

        "success": True,

        "api": "online",

        "telegram": {

            "configured":
                bool(
                    TELEGRAM_BOT_TOKEN
                ),

            "running":
                telegram_running
        },

        "database":
            os.path.exists(
                DATABASE
            ),

        "current_week":
            get_current_week(),

        "weekly_limit":
            WEEKLY_LIMIT
    }
