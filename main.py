from fastapi import (
    FastAPI,
    HTTPException,
    Header,
    Request,
    Depends,
)
from fastapi.responses import Response
from fastapi.security import (
    HTTPBearer,
    HTTPAuthorizationCredentials,
)
from pydantic import BaseModel, Field

import edge_tts
import os
import tempfile
import sqlite3
import secrets
import string
import asyncio
import time
import hashlib

from datetime import datetime, timezone

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

# أقصى حجم للنص في طلب TTS واحد
MAX_TTS_CHARACTERS = 50_000

# مدة جلسة لوحة التحكم
ADMIN_SESSION_HOURS = 12


# =========================================================
# ENVIRONMENT VARIABLES
# =========================================================

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    ""
)

PUBLIC_URL = os.getenv(
    "PUBLIC_URL",
    ""
).rstrip("/")

TELEGRAM_WEBHOOK_SECRET = os.getenv(
    "TELEGRAM_WEBHOOK_SECRET",
    ""
)


# =========================================================
# ADMIN CONFIG
# =========================================================

ADMIN_USERNAME = os.getenv(
    "ADMIN_USERNAME",
    ""
)

ADMIN_PASSWORD = os.getenv(
    "ADMIN_PASSWORD",
    ""
)

ADMIN_API_ENABLED = bool(
    ADMIN_USERNAME and ADMIN_PASSWORD
)


# =========================================================
# FASTAPI
# =========================================================

app = FastAPI(
    title="Edge TTS API",
    version="2.0.0",
    description=(
        "Edge TTS API with Telegram activation "
        "and secure Admin API"
    )
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

    # تحسين التعامل مع الطلبات المتزامنة
    conn.execute(
        "PRAGMA journal_mode=WAL"
    )

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
            expires_at INTEGER NOT NULL
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
        CREATE INDEX IF NOT EXISTS idx_admin_sessions_hash
        ON admin_sessions(token_hash)
    """)

    conn.commit()

    conn.close()


# إنشاء الجداول عند تشغيل الملف
init_db()


# =========================================================
# WEEK SYSTEM
# =========================================================

def get_current_week():

    now = datetime.now(
        timezone.utc
    )

    year, week, _ = now.isocalendar()

    return f"{year}-{week}"


def reset_week_if_needed(user):

    current_week = get_current_week()

    if user["week_id"] == current_week:
        return False

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


# =========================================================
# GENERATORS
# =========================================================

def generate_code():

    chars = (
        string.ascii_uppercase
        +
        string.digits
    )

    return "-".join(
        "".join(
            secrets.choice(chars)
            for _ in range(4)
        )
        for _ in range(3)
    )


def generate_api_key():

    return secrets.token_urlsafe(
        32
    )


def hash_admin_token(token):

    return hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()


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
        VALUES (
            ?, ?, ?, 0, 0, ?, ?
        )
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
        SELECT
            id,
            telegram_user_id
        FROM devices
        WHERE installation_id = ?
    """, (
        installation_id,
    )).fetchone()

    if existing:

        # لا نسمح بنقل Installation ID
        # تلقائيًا من حساب إلى حساب آخر.
        if str(
            existing["telegram_user_id"]
        ) == str(
            telegram_user_id
        ):

            conn.execute("""
                UPDATE devices
                SET
                    manufacturer = ?,
                    model = ?,
                    android_version = ?,
                    app_version = ?,
                    platform = ?,
                    last_seen = ?
                WHERE installation_id = ?
            """, (
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
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
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
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
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

    if not update.message:
        return

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
# START USE
# =========================================================

async def button_start_use(query):

    telegram_id = str(
        query.from_user.id
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
# ACTIVATE ACCOUNT
# =========================================================

async def button_activate_account(query):

    telegram_id = str(
        query.from_user.id
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

        reply_markup=InlineKeyboardMarkup([

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

        ])
    )


# =========================================================
# CONFIRM ACTIVATION
# =========================================================

async def button_confirm_activation(query):

    telegram_id = str(
        query.from_user.id
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

    await query.edit_message_text(

        """
✅ تم تفعيل حسابك بنجاح!

🎁 الرصيد الأسبوعي:

200,000 حرف

الخطوة التالية:

اضغط «📱 ربط التطبيق» لربط تطبيق Android بحسابك.
""",

        reply_markup=InlineKeyboardMarkup([

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

        ])
    )


# =========================================================
# ACCOUNT BUTTON
# =========================================================

async def button_account(query):

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

        reply_markup=InlineKeyboardMarkup([

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

        ])
    )


# =========================================================
# LINK APP
# =========================================================

async def button_link_app(query):

    await query.edit_message_text(

        """
📱 ربط تطبيق Android

افتح التطبيق واختر «ربط حساب Telegram».

سيتم استخدام API Key الخاص بحسابك داخليًا.

🔒 لا تشارك API Key مع أي شخص.
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
# HELP
# =========================================================

async def button_help(query):

    await query.edit_message_text(

        f"""
ℹ️ طريقة الاستخدام

1️⃣ افتح التطبيق.

2️⃣ اربط حساب Telegram.

3️⃣ اكتب النص.

4️⃣ اختر الصوت والسرعة والنغمة.

5️⃣ اضغط إنشاء الصوت.

🎙️ سيتم تحويل النص إلى MP3.

🎁 الحد المجاني:
{WEEKLY_LIMIT:,} حرف أسبوعيًا.

🔒 نصوص TTS لا يتم حفظها في قاعدة البيانات.
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
# HOME
# =========================================================

async def button_home(query):

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

    if not query:
        return

    await query.answer()

    actions = {

        "start_use":
            button_start_use,

        "activate_account":
            button_activate_account,

        "confirm_activation":
            button_confirm_activation,

        "account":
            button_account,

        "link_app":
            button_link_app,

        "help":
            button_help,

        "home":
            button_home
    }

    handler = actions.get(
        query.data
    )

    if handler:

        await handler(query)


# =========================================================
# LEGACY /TRIAL
# =========================================================

async def telegram_trial(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    telegram_id = str(
        update.effective_user.id
    )

    row = create_or_get_user(
        telegram_id
    )

    reset_week_if_needed(row)

    row = get_user_by_telegram_id(
        telegram_id
    )

    status = (
        "مفعل"
        if row["activated"]
        else "غير مفعل"
    )

    await update.message.reply_text(

        f"""
🎉 حسابك

الحالة:
{status}

🎁 الرصيد الأسبوعي:
{WEEKLY_LIMIT:,} حرف

🔑 كود التفعيل:
`{row["activation_code"]}`
""",

        parse_mode="Markdown",

        reply_markup=main_menu()
    )


# =========================================================
# LEGACY /ACTIVATE
# =========================================================

async def telegram_activate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not context.args:

        await update.message.reply_text(

            """
⚠️ اكتب كود التفعيل بعد الأمر.

مثال:

/activate ABCD-EF12-3456
"""
        )

        return

    code = (
        context.args[0]
        .strip()
        .upper()
    )

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

        f"""
✅ تم تفعيل حسابك بنجاح!

🎁 الرصيد:

{WEEKLY_LIMIT:,} حرف أسبوعيًا

📱 استخدم تطبيق Edge TTS الآن.
""",

        reply_markup=main_menu()
    )


# =========================================================
# LEGACY /ACCOUNT
# =========================================================

async def telegram_account(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

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
# TELEGRAM APPLICATION
# =========================================================

telegram_app = None


async def start_telegram_bot():

    global telegram_app

    if telegram_app is not None:
        return

    if not TELEGRAM_BOT_TOKEN:

        print(
            "Telegram disabled: "
            "TELEGRAM_BOT_TOKEN missing"
        )

        return

    if not PUBLIC_URL:

        print(
            "Telegram webhook disabled: "
            "PUBLIC_URL missing"
        )

        return

    if not TELEGRAM_WEBHOOK_SECRET:

        print(
            "Telegram webhook disabled: "
            "TELEGRAM_WEBHOOK_SECRET missing"
        )

        return

    telegram_app = (
        Application.builder()
        .token(
            TELEGRAM_BOT_TOKEN
        )
        .build()
    )

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

    telegram_app.add_handler(
        CallbackQueryHandler(
            telegram_button_handler
        )
    )

    await telegram_app.initialize()

    await telegram_app.start()

    webhook_url = (
        f"{PUBLIC_URL}"
        "/telegram/webhook"
    )

    await telegram_app.bot.delete_webhook(
        drop_pending_updates=False
    )

    await telegram_app.bot.set_webhook(
        url=webhook_url,
        secret_token=TELEGRAM_WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES
    )

    print(
        "Telegram webhook started"
    )

    print(
        f"Webhook URL: {webhook_url}"
    )


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

@app.post(
    "/telegram/webhook",
    response_model=dict
)
async def telegram_webhook(
    request: Request
):

    if telegram_app is None:

        raise HTTPException(
            status_code=503,
            detail=
                "Telegram bot is not ready"
        )

    secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if (
        not TELEGRAM_WEBHOOK_SECRET
        or
        not secrets.compare_digest(
            secret or "",
            TELEGRAM_WEBHOOK_SECRET
        )
    ):

        raise HTTPException(
            status_code=403,
            detail=
                "Invalid Telegram webhook secret"
        )

    data = await request.json()

    update = Update.de_json(
        data,
        telegram_app.bot
    )

    await telegram_app.process_update(
        update
    )

    return {
        "success": True
    }


# =========================================================
# ADMIN AUTHENTICATION
# =========================================================

admin_bearer = HTTPBearer(
    auto_error=False
)


def cleanup_expired_admin_sessions():

    conn = get_db()

    conn.execute("""
        DELETE FROM admin_sessions
        WHERE expires_at < ?
    """, (
        int(time.time()),
    ))

    conn.commit()

    conn.close()


def create_admin_session():

    token = secrets.token_urlsafe(
        48
    )

    token_hash = hash_admin_token(
        token
    )

    created_at = datetime.now(
        timezone.utc
    ).isoformat()

    expires_at = (
        int(time.time())
        +
        ADMIN_SESSION_HOURS * 3600
    )

    conn = get_db()

    conn.execute("""
        INSERT INTO admin_sessions (
            token_hash,
            created_at,
            expires_at
        )
        VALUES (
            ?, ?, ?
        )
    """, (
        token_hash,
        created_at,
        expires_at
    ))

    conn.commit()

    conn.close()

    return token


def verify_admin_token(token):

    if not ADMIN_API_ENABLED:

        raise HTTPException(
            status_code=503,
            detail=
                "Admin API is not configured"
        )

    if not token:

        raise HTTPException(
            status_code=401,
            detail=
                "Admin authorization required"
        )

    token = token.removeprefix(
        "Bearer "
    ).strip()

    token_hash = hash_admin_token(
        token
    )

    now = int(time.time())

    conn = get_db()

    session = conn.execute("""
        SELECT id
        FROM admin_sessions
        WHERE token_hash = ?
        AND expires_at >= ?
    """, (
        token_hash,
        now
    )).fetchone()

    conn.close()

    if not session:

        cleanup_expired_admin_sessions()

        raise HTTPException(
            status_code=401,
            detail=
                "Invalid or expired admin token"
        )

    return True


def require_admin(
    credentials:
        HTTPAuthorizationCredentials = Depends(
            admin_bearer
        )
):

    if credentials is None:

        raise HTTPException(
            status_code=401,
            detail=
                "Admin authorization required"
        )

    if credentials.scheme.lower() != "bearer":

        raise HTTPException(
            status_code=401,
            detail=
                "Invalid authorization scheme"
        )

    verify_admin_token(
        credentials.credentials
    )

    return True


# =========================================================
# ADMIN MODELS
# =========================================================

class AdminLoginRequest(
    BaseModel
):

    username: str = Field(
        min_length=1,
        max_length=200
    )

    password: str = Field(
        min_length=1,
        max_length=500
    )


# =========================================================
# ADMIN LOGIN
# =========================================================

@app.post(
    "/admin/login",
    response_model=dict
)
async def admin_login(
    data: AdminLoginRequest
):

    if not ADMIN_API_ENABLED:

        raise HTTPException(
            status_code=503,
            detail=
                "Admin API is not configured"
        )

    username_ok = secrets.compare_digest(
        data.username,
        ADMIN_USERNAME
    )

    password_ok = secrets.compare_digest(
        data.password,
        ADMIN_PASSWORD
    )

    if (
        not username_ok
        or
        not password_ok
    ):

        raise HTTPException(
            status_code=401,
            detail=
                "Invalid admin credentials"
        )

    cleanup_expired_admin_sessions()

    token = create_admin_session()

    return {

        "success":
            True,

        "token":
            token,

        "token_type":
            "Bearer",

        "expires_in":
            ADMIN_SESSION_HOURS * 3600
    }


# =========================================================
# ADMIN LOGOUT
# =========================================================

@app.post(
    "/admin/logout",
    response_model=dict
)
async def admin_logout(

    credentials:
        HTTPAuthorizationCredentials = Depends(
            admin_bearer
        )

):

    if credentials is None:

        raise HTTPException(
            status_code=401,
            detail=
                "Admin authorization required"
        )

    if credentials.scheme.lower() != "bearer":

        raise HTTPException(
            status_code=401,
            detail=
                "Invalid authorization scheme"
        )

    token_hash = hash_admin_token(
        credentials.credentials
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

        "success":
            True,

        "message":
            "Admin session terminated"
    }


# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.get(
    "/admin/dashboard",
    response_model=dict
)
async def admin_dashboard(
    _: bool = Depends(require_admin)
):

    conn = get_db()

    total_users = conn.execute("""
        SELECT COUNT(*)
        FROM users
    """).fetchone()[0]

    active_users = conn.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE activated = 1
    """).fetchone()[0]

    inactive_users = conn.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE activated = 0
    """).fetchone()[0]

    total_devices = conn.execute("""
        SELECT COUNT(*)
        FROM devices
    """).fetchone()[0]

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

    total_file_bytes = conn.execute("""
        SELECT COALESCE(
            SUM(file_size_bytes),
            0
        )
        FROM tts_requests
        WHERE success = 1
    """).fetchone()[0]

    today = (
        datetime.now(
            timezone.utc
        )
        .date()
        .isoformat()
    )

    today_requests = conn.execute("""
        SELECT COUNT(*)
        FROM tts_requests
        WHERE created_at LIKE ?
    """, (
        today + "%",
    )).fetchone()[0]

    today_characters = conn.execute("""
        SELECT COALESCE(
            SUM(character_count),
            0
        )
        FROM tts_requests
        WHERE success = 1
        AND created_at LIKE ?
    """, (
        today + "%",
    )).fetchone()[0]

    conn.close()

    success_rate = (

        round(
            (
                successful_requests
                /
                total_requests
            )
            * 100,
            2
        )

        if total_requests

        else 0
    )

    return {

        "success":
            True,

        "users": {

            "total":
                total_users,

            "active":
                active_users,

            "inactive":
                inactive_users
        },

        "devices": {

            "total":
                total_devices
        },

        "tts": {

            "total_requests":
                total_requests,

            "successful_requests":
                successful_requests,

            "failed_requests":
                failed_requests,

            "success_rate_percent":
                success_rate,

            "total_characters":
                total_characters,

            "total_file_bytes":
                total_file_bytes,

            "today_requests":
                today_requests,

            "today_characters":
                today_characters
        },

        "weekly_limit":
            WEEKLY_LIMIT,

        "max_tts_characters":
            MAX_TTS_CHARACTERS,

        "week":
            get_current_week()
    }


# =========================================================
# ADMIN USERS
# =========================================================

@app.get(
    "/admin/users",
    response_model=dict
)
async def admin_users(

    _: bool = Depends(require_admin),

    search: str = "",

    limit: int = 100,

    offset: int = 0
):

    limit = max(
        1,
        min(limit, 500)
    )

    offset = max(
        0,
        offset
    )

    search = search.strip()

    conn = get_db()

    if search:

        pattern = (
            "%"
            +
            search
            +
            "%"
        )

        rows = conn.execute("""
            SELECT
                id,
                telegram_user_id,
                activation_code,
                characters_used,
                activated,
                week_id,
                code_week_id
            FROM users
            WHERE
                telegram_user_id LIKE ?
                OR activation_code LIKE ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, (
            pattern,
            pattern,
            limit,
            offset
        )).fetchall()

        total = conn.execute("""
            SELECT COUNT(*)
            FROM users
            WHERE
                telegram_user_id LIKE ?
                OR activation_code LIKE ?
        """, (
            pattern,
            pattern
        )).fetchone()[0]

    else:

        rows = conn.execute("""
            SELECT
                id,
                telegram_user_id,
                activation_code,
                characters_used,
                activated,
                week_id,
                code_week_id
            FROM users
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, (
            limit,
            offset
        )).fetchall()

        total = conn.execute("""
            SELECT COUNT(*)
            FROM users
        """).fetchone()[0]

    conn.close()

    users = []

    for row in rows:

        used = row["characters_used"]

        users.append({

            "id":
                row["id"],

            "telegram_user_id":
                row["telegram_user_id"],

            "activation_code":
                row["activation_code"],

            "characters_used":
                used,

            "characters_remaining":
                max(
                    0,
                    WEEKLY_LIMIT - used
                ),

            "activated":
                bool(
                    row["activated"]
                ),

            "week_id":
                row["week_id"],

            "code_week_id":
                row["code_week_id"]
        })

    return {

        "success":
            True,

        "total":
            total,

        "limit":
            limit,

        "offset":
            offset,

        "users":
            users
    }


# =========================================================
# ADMIN USER DETAILS
# =========================================================

@app.get(
    "/admin/users/{telegram_user_id}",
    response_model=dict
)
async def admin_user_details(

    telegram_user_id: str,

    _: bool = Depends(require_admin)
):

    conn = get_db()

    user = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (
        telegram_user_id,
    )).fetchone()

    if not user:

        conn.close()

        raise HTTPException(
            status_code=404,
            detail=
                "User not found"
        )

    devices = conn.execute("""
        SELECT
            id,
            installation_id,
            manufacturer,
            model,
            android_version,
            app_version,
            platform,
            first_seen,
            last_seen
        FROM devices
        WHERE telegram_user_id = ?
        ORDER BY id DESC
    """, (
        telegram_user_id,
    )).fetchall()

    requests = conn.execute("""
        SELECT
            id,
            installation_id,
            character_count,
            voice,
            rate,
            pitch,
            generation_time_ms,
            file_size_bytes,
            success,
            created_at
        FROM tts_requests
        WHERE telegram_user_id = ?
        ORDER BY id DESC
        LIMIT 100
    """, (
        telegram_user_id,
    )).fetchall()

    conn.close()

    used = user["characters_used"]

    return {

        "success":
            True,

        "user": {

            "id":
                user["id"],

            "telegram_user_id":
                user["telegram_user_id"],

            "activation_code":
                user["activation_code"],

            "api_key":
                user["api_key"],

            "characters_used":
                used,

            "characters_remaining":
                max(
                    0,
                    WEEKLY_LIMIT - used
                ),

            "activated":
                bool(
                    user["activated"]
                ),

            "week_id":
                user["week_id"],

            "code_week_id":
                user["code_week_id"]
        },

        "devices": [
            dict(row)
            for row in devices
        ],

        "requests": [
            dict(row)
            for row in requests
        ]
    }


# =========================================================
# ADMIN USER HELPER
# =========================================================

def get_admin_user_or_404(
    conn,
    telegram_user_id
):

    row = conn.execute("""
        SELECT telegram_user_id
        FROM users
        WHERE telegram_user_id = ?
    """, (
        telegram_user_id,
    )).fetchone()

    if not row:

        raise HTTPException(
            status_code=404,
            detail=
                "User not found"
        )

    return row


# =========================================================
# ADMIN ENABLE
# =========================================================

@app.post(
    "/admin/users/{telegram_user_id}/enable",
    response_model=dict
)
async def admin_enable_user(

    telegram_user_id: str,

    _: bool = Depends(require_admin)
):

    conn = get_db()

    get_admin_user_or_404(
        conn,
        telegram_user_id
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

        "success":
            True,

        "message":
            "User enabled",

        "telegram_user_id":
            telegram_user_id
    }


# =========================================================
# ADMIN DISABLE
# =========================================================

@app.post(
    "/admin/users/{telegram_user_id}/disable",
    response_model=dict
)
async def admin_disable_user(

    telegram_user_id: str,

    _: bool = Depends(require_admin)
):

    conn = get_db()

    get_admin_user_or_404(
        conn,
        telegram_user_id
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

        "success":
            True,

        "message":
            "User disabled",

        "telegram_user_id":
            telegram_user_id
    }


# =========================================================
# ADMIN RESET USAGE
# =========================================================

@app.post(
    "/admin/users/{telegram_user_id}/reset-usage",
    response_model=dict
)
async def admin_reset_usage(

    telegram_user_id: str,

    _: bool = Depends(require_admin)
):

    current_week = get_current_week()

    conn = get_db()

    get_admin_user_or_404(
        conn,
        telegram_user_id
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

        "success":
            True,

        "message":
            "Usage reset",

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
    "/admin/users/{telegram_user_id}/regenerate-key",
    response_model=dict
)
async def admin_regenerate_key(

    telegram_user_id: str,

    _: bool = Depends(require_admin)
):

    new_api_key = generate_api_key()

    conn = get_db()

    get_admin_user_or_404(
        conn,
        telegram_user_id
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

        "success":
            True,

        "message":
            "API key regenerated",

        "telegram_user_id":
            telegram_user_id,

        "api_key":
            new_api_key
    }


# =========================================================
# ADMIN RENEW CODE
# =========================================================

@app.post(
    "/admin/users/{telegram_user_id}/renew-code",
    response_model=dict
)
async def admin_renew_code(

    telegram_user_id: str,

    _: bool = Depends(require_admin)
):

    current_week = get_current_week()

    new_code = generate_code()

    conn = get_db()

    get_admin_user_or_404(
        conn,
        telegram_user_id
    )

    conn.execute("""
        UPDATE users
        SET
            activation_code = ?,
            code_week_id = ?,
            activated = 0
        WHERE telegram_user_id = ?
    """, (
        new_code,
        current_week,
        telegram_user_id
    ))

    conn.commit()

    conn.close()

    return {

        "success":
            True,

        "message":
            "Activation code renewed",

        "telegram_user_id":
            telegram_user_id,

        "activation_code":
            new_code,

        "week":
            current_week,

        "activated":
            False
    }


# =========================================================
# ADMIN DEVICES
# =========================================================

@app.get(
    "/admin/devices",
    response_model=dict
)
async def admin_devices(

    _: bool = Depends(require_admin),

    search: str = "",

    limit: int = 100,

    offset: int = 0
):

    limit = max(
        1,
        min(limit, 500)
    )

    offset = max(
        0,
        offset
    )

    search = search.strip()

    conn = get_db()

    if search:

        pattern = (
            "%"
            +
            search
            +
            "%"
        )

        rows = conn.execute("""
            SELECT *
            FROM devices
            WHERE
                telegram_user_id LIKE ?
                OR installation_id LIKE ?
                OR manufacturer LIKE ?
                OR model LIKE ?
                OR app_version LIKE ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, (
            pattern,
            pattern,
            pattern,
            pattern,
            pattern,
            limit,
            offset
        )).fetchall()

        total = conn.execute("""
            SELECT COUNT(*)
            FROM devices
            WHERE
                telegram_user_id LIKE ?
                OR installation_id LIKE ?
                OR manufacturer LIKE ?
                OR model LIKE ?
                OR app_version LIKE ?
        """, (
            pattern,
            pattern,
            pattern,
            pattern,
            pattern
        )).fetchone()[0]

    else:

        rows = conn.execute("""
            SELECT *
            FROM devices
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, (
            limit,
            offset
        )).fetchall()

        total = conn.execute("""
            SELECT COUNT(*)
            FROM devices
        """).fetchone()[0]

    conn.close()

    return {

        "success":
            True,

        "total":
            total,

        "limit":
            limit,

        "offset":
            offset,

        "devices": [
            dict(row)
            for row in rows
        ]
    }


# =========================================================
# ADMIN REQUESTS
# =========================================================

@app.get(
    "/admin/requests",
    response_model=dict
)
async def admin_requests(

    _: bool = Depends(require_admin),

    telegram_user_id: str = "",

    limit: int = 100,

    offset: int = 0
):

    limit = max(
        1,
        min(limit, 500)
    )

    offset = max(
        0,
        offset
    )

    telegram_user_id = (
        telegram_user_id.strip()
    )

    conn = get_db()

    if telegram_user_id:

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

        total = conn.execute("""
            SELECT COUNT(*)
            FROM tts_requests
            WHERE telegram_user_id = ?
        """, (
            telegram_user_id,
        )).fetchone()[0]

    else:

        rows = conn.execute("""
            SELECT *
            FROM tts_requests
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, (
            limit,
            offset
        )).fetchall()

        total = conn.execute("""
            SELECT COUNT(*)
            FROM tts_requests
        """).fetchone()[0]

    conn.close()

    return {

        "success":
            True,

        "total":
            total,

        "limit":
            limit,

        "offset":
            offset,

        "requests": [
            dict(row)
            for row in rows
        ]
    }


# =========================================================
# ADMIN HEALTH
# =========================================================

@app.get(
    "/admin/health",
    response_model=dict
)
async def admin_health(

    _: bool = Depends(require_admin)
):

    database_status = "ok"

    try:

        conn = get_db()

        conn.execute(
            "SELECT 1"
        ).fetchone()

        conn.close()

    except Exception as exc:

        print(
            "Database health error:",
            repr(exc)
        )

        database_status = "error"

    telegram_status = (

        "running"

        if telegram_app is not None

        else

        "not_running"
    )

    return {

        "success":
            True,

        "server":
            "ok",

        "database":
            database_status,

        "telegram":
            telegram_status,

        "telegram_webhook":
            bool(
                TELEGRAM_BOT_TOKEN
                and
                PUBLIC_URL
                and
                TELEGRAM_WEBHOOK_SECRET
            ),

        "edge_tts":
            "available",

        "admin_api":
            ADMIN_API_ENABLED,

        "admin_auth":
            "database_session",

        "admin_session_hours":
            ADMIN_SESSION_HOURS,

        "week":
            get_current_week()
    }


# =========================================================
# PUBLIC MODELS
# =========================================================

class TTSRequest(
    BaseModel
):

    text: str = Field(
        min_length=1,
        max_length=MAX_TTS_CHARACTERS
    )

    voice: str = Field(
        default="ar-EG-SalmaNeural",
        min_length=1,
        max_length=200
    )

    rate: str = Field(
        default="+0%",
        max_length=20
    )

    pitch: str = Field(
        default="+0Hz",
        max_length=20
    )

    installation_id: str | None = Field(
        default=None,
        max_length=200
    )

    manufacturer: str | None = Field(
        default=None,
        max_length=200
    )

    model: str | None = Field(
        default=None,
        max_length=200
    )

    android_version: str | None = Field(
        default=None,
        max_length=100
    )

    app_version: str | None = Field(
        default=None,
        max_length=100
    )

    platform: str = Field(
        default="Android",
        max_length=50
    )


class ActivateRequest(
    BaseModel
):

    code: str = Field(
        min_length=1,
        max_length=50
    )


# =========================================================
# HOME API
# =========================================================

@app.get(
    "/",
    response_model=dict
)
async def home():

    return {

        "status":
            "online",

        "service":
            "Edge TTS API",

        "version":
            "2.0.0",

        "weekly_limit":
            WEEKLY_LIMIT,

        "max_tts_characters":
            MAX_TTS_CHARACTERS,

        "telegram_mode":
            "webhook",

        "telegram_webhook":
            bool(
                TELEGRAM_BOT_TOKEN
                and
                PUBLIC_URL
                and
                TELEGRAM_WEBHOOK_SECRET
            ),

        "admin_api":
            ADMIN_API_ENABLED,

        "admin_auth":
            "database_session"
    }


# =========================================================
# PUBLIC AUTH HELPERS
# =========================================================

def extract_bearer(
    authorization
):

    if not authorization:

        raise HTTPException(
            status_code=401,
            detail=
                "Authorization required"
        )

    if not authorization.startswith(
        "Bearer "
    ):

        raise HTTPException(
            status_code=401,
            detail=
                "Invalid authorization"
        )

    token = (
        authorization[7:]
        .strip()
    )

    if not token:

        raise HTTPException(
            status_code=401,
            detail=
                "Invalid authorization"
        )

    return token


def get_api_user(
    api_key
):

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
            detail=
                "Invalid API key"
        )

    return user


# =========================================================
# ACTIVATE API
# =========================================================

@app.post(
    "/activate",
    response_model=dict
)
async def activate(
    data: ActivateRequest
):

    code = (
        data.code
        .strip()
        .upper()
    )

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

        raise HTTPException(
            status_code=404,
            detail=
                "Invalid activation code"
        )

    if user["code_week_id"] != current_week:

        conn.close()

        raise HTTPException(
            status_code=400,
            detail=
                "Activation code expired"
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

        "success":
            True,

        "message":
            "Account activated",

        "api_key":
            api_key,

        "weekly_limit":
            WEEKLY_LIMIT,

        "characters_used":
            0,

        "characters_remaining":
            WEEKLY_LIMIT
    }


# =========================================================
# ACCOUNT API
# =========================================================

@app.get(
    "/account",
    response_model=dict
)
async def account(

    authorization:
        str = Header(None)

):

    api_key = extract_bearer(
        authorization
    )

    user = get_api_user(
        api_key
    )

    reset_week_if_needed(
        user
    )

    user = get_user_by_telegram_id(
        user["telegram_user_id"]
    )

    used = user["characters_used"]

    return {

        "success":
            True,

        "weekly_limit":
            WEEKLY_LIMIT,

        "characters_used":
            used,

        "characters_remaining":
            max(
                0,
                WEEKLY_LIMIT - used
            ),

        "activated":
            bool(
                user["activated"]
            ),

        "week":
            get_current_week()
    }


# =========================================================
# QUOTA LOCK
# =========================================================

quota_lock = asyncio.Lock()


async def reserve_quota(
    api_key,
    character_count
):

    async with quota_lock:

        conn = get_db()

        try:

            current_week = (
                get_current_week()
            )

            row = conn.execute("""
                SELECT *
                FROM users
                WHERE api_key = ?
            """, (
                api_key,
            )).fetchone()

            if not row:

                raise HTTPException(
                    status_code=401,
                    detail=
                        "Invalid API key"
                )

            if not row["activated"]:

                raise HTTPException(
                    status_code=403,
                    detail=
                        "Account is not activated"
                )

            # ---------------------------------------------
            # WEEK RESET
            # ---------------------------------------------

            if row["week_id"] != current_week:

                conn.execute("""
                    UPDATE users
                    SET
                        characters_used = 0,
                        week_id = ?
                    WHERE api_key = ?
                """, (
                    current_week,
                    api_key
                ))

                conn.commit()

                row = conn.execute("""
                    SELECT *
                    FROM users
                    WHERE api_key = ?
                """, (
                    api_key,
                )).fetchone()

            remaining = max(
                0,
                WEEKLY_LIMIT
                -
                row["characters_used"]
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
                            row["characters_used"],

                        "characters_remaining":
                            remaining,

                        "requested":
                            character_count
                    }
                )

            # ---------------------------------------------
            # ATOMIC RESERVATION
            # ---------------------------------------------

            cursor = conn.execute("""

                UPDATE users

                SET
                    characters_used =
                        characters_used + ?

                WHERE
                    api_key = ?

                    AND activated = 1

                    AND week_id = ?

                    AND characters_used + ? <= ?

            """, (
                character_count,
                api_key,
                current_week,
                character_count,
                WEEKLY_LIMIT
            ))

            conn.commit()

            if cursor.rowcount != 1:

                raise HTTPException(

                    status_code=429,

                    detail=
                        "Weekly character limit exceeded"
                )

            return row

        finally:

            conn.close()


# =========================================================
# TTS API
# =========================================================

@app.post(
    "/tts"
)
async def tts(

    data: TTSRequest,

    authorization:
        str = Header(None)

):

    # -----------------------------------------------------
    # AUTH
    # -----------------------------------------------------

    api_key = extract_bearer(
        authorization
    )

    # -----------------------------------------------------
    # TEXT
    # -----------------------------------------------------

    text = data.text

    if not text.strip():

        raise HTTPException(
            status_code=400,
            detail=
                "Text is empty"
        )

    character_count = len(
        text
    )

    if character_count > MAX_TTS_CHARACTERS:

        raise HTTPException(

            status_code=413,

            detail=(
                "Maximum text length is "
                f"{MAX_TTS_CHARACTERS} characters"
            )
        )

    # -----------------------------------------------------
    # RESERVE QUOTA
    # -----------------------------------------------------

    user = await reserve_quota(
        api_key,
        character_count
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

    temp_file = (
        tempfile.NamedTemporaryFile(
            suffix=".mp3",
            delete=False
        )
    )

    temp_file.close()

    start_time = time.perf_counter()

    try:

        communicate = edge_tts.Communicate(

            text=text,

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
                -
                start_time
            )
            *
            1000
        )

        file_size_bytes = len(
            audio
        )

        # -------------------------------------------------
        # LOG SUCCESS
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
                    'attachment; filename="speech.mp3"',

                "X-Character-Count":
                    str(
                        character_count
                    ),

                "X-Generation-Time-Ms":
                    str(
                        generation_time_ms
                    )
            }
        )

    except HTTPException:

        raise

    except Exception as exc:

        generation_time_ms = int(

            (
                time.perf_counter()
                -
                start_time
            )
            *
            1000
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

            file_size_bytes=
                0,

            success=False
        )

        # لا نكشف تفاصيل Edge TTS الداخلية
        print(
            "TTS generation error:",
            repr(exc)
        )

        raise HTTPException(

            status_code=502,

            detail=
                "TTS generation failed"
        )

    finally:

        try:

            if os.path.exists(
                temp_file.name
            ):

                os.remove(
                    temp_file.name
                )

        except Exception as exc:

            print(
                "Temporary file cleanup error:",
                repr(exc)
            )


# =========================================================
# STARTUP
# =========================================================

@app.on_event(
    "startup"
)
async def startup_event():

    init_db()

    cleanup_expired_admin_sessions()

    if not ADMIN_API_ENABLED:

        print(
            "WARNING: "
            "ADMIN_USERNAME/ADMIN_PASSWORD "
            "are not configured"
        )

    if not TELEGRAM_BOT_TOKEN:

        print(
            "WARNING: "
            "TELEGRAM_BOT_TOKEN is not set"
        )

    if not PUBLIC_URL:

        print(
            "WARNING: "
            "PUBLIC_URL is not set"
        )

    if not TELEGRAM_WEBHOOK_SECRET:

        print(
            "WARNING: "
            "TELEGRAM_WEBHOOK_SECRET is not set"
        )

    try:

        await start_telegram_bot()

    except Exception as exc:

        print(
            "Telegram startup error:",
            repr(exc)
        )


# =========================================================
# SHUTDOWN
# =========================================================

@app.on_event(
    "shutdown"
)
async def shutdown_event():

    global telegram_app

    if telegram_app is None:
        return

    try:

        await telegram_app.bot.delete_webhook()

    except Exception as exc:

        print(
            "Telegram webhook delete error:",
            repr(exc)
        )

    try:

        await telegram_app.stop()

    except Exception as exc:

        print(
            "Telegram stop error:",
            repr(exc)
        )

    try:

        await telegram_app.shutdown()

    except Exception as exc:

        print(
            "Telegram shutdown error:",
            repr(exc)
        )

    telegram_app = None
