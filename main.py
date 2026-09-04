from fastapi import (
    FastAPI,
    HTTPException,
    Header,
    Request,
)

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
import json
import base64
import hashlib
import hmac

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

ADMIN_TOKEN_EXPIRE_SECONDS = 60 * 60 * 12


# =========================================================
# ENVIRONMENT VARIABLES
# =========================================================

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN"
)

PUBLIC_URL = os.getenv(
    "PUBLIC_URL",
    ""
).rstrip("/")

TELEGRAM_WEBHOOK_SECRET = os.getenv(
    "TELEGRAM_WEBHOOK_SECRET",
    ""
)

# ---------------------------------------------------------
# ADMIN
# ---------------------------------------------------------

ADMIN_USERNAME = os.getenv(
    "ADMIN_USERNAME",
    ""
)

ADMIN_PASSWORD = os.getenv(
    "ADMIN_PASSWORD",
    ""
)

ADMIN_SECRET_KEY = os.getenv(
    "ADMIN_SECRET_KEY",
    ""
)


if not TELEGRAM_BOT_TOKEN:
    print(
        "WARNING: TELEGRAM_BOT_TOKEN is not set"
    )


if not PUBLIC_URL:
    print(
        "WARNING: PUBLIC_URL is not set"
    )


if not TELEGRAM_WEBHOOK_SECRET:
    print(
        "WARNING: TELEGRAM_WEBHOOK_SECRET is not set"
    )


if not ADMIN_USERNAME:
    print(
        "WARNING: ADMIN_USERNAME is not set"
    )


if not ADMIN_PASSWORD:
    print(
        "WARNING: ADMIN_PASSWORD is not set"
    )


if not ADMIN_SECRET_KEY:
    print(
        "WARNING: ADMIN_SECRET_KEY is not set"
    )


# =========================================================
# FASTAPI
# =========================================================

app = FastAPI(
    title="Edge TTS API"
)


# =========================================================
# DATABASE
# =========================================================

def get_db():

    conn = sqlite3.connect(
        DATABASE
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

        CREATE INDEX IF NOT EXISTS
        idx_requests_installation

        ON tts_requests(installation_id)

    """)

    conn.execute("""

        CREATE INDEX IF NOT EXISTS
        idx_requests_created_at

        ON tts_requests(created_at)

    """)

    conn.commit()

    conn.close()


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

    user = update.effective_user

    if not update.message:

        return

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

    reset_week_if_needed(
        row
    )

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

    code = row[
        "activation_code"
    ]

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

    api_key = row[
        "api_key"
    ]

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

    reset_week_if_needed(
        row
    )

    row = get_user_by_telegram_id(
        telegram_id
    )

    used = row[
        "characters_used"
    ]

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

    if not query:

        return

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

    if not update.message:

        return

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

    api_key = user[
        "api_key"
    ]

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

    reset_week_if_needed(
        row
    )

    row = get_user_by_telegram_id(

        telegram_id

    )

    used = row[
        "characters_used"
    ]

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
# WEBHOOK MODE
# =========================================================

telegram_app = None


async def start_telegram_bot():

    global telegram_app

    if telegram_app is not None:

        print(
            "Telegram application already initialized"
        )

        return

    if not TELEGRAM_BOT_TOKEN:

        print(
            "Telegram bot disabled: "
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

    # -----------------------------------------------------
    # COMMANDS
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # BUTTONS
    # -----------------------------------------------------

    telegram_app.add_handler(

        CallbackQueryHandler(

            telegram_button_handler

        )

    )

    # -----------------------------------------------------
    # INITIALIZE
    # -----------------------------------------------------

    await telegram_app.initialize()

    await telegram_app.start()

    # -----------------------------------------------------
    # WEBHOOK
    # -----------------------------------------------------

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
# TELEGRAM WEBHOOK ENDPOINT
# =========================================================

@app.post(
    "/telegram/webhook"
)
async def telegram_webhook(
    request: Request
):

    if telegram_app is None:

        raise HTTPException(

            status_code=503,

            detail="Telegram bot is not ready"

        )

    secret = request.headers.get(

        "X-Telegram-Bot-Api-Secret-Token"

    )

    if secret != TELEGRAM_WEBHOOK_SECRET:

        raise HTTPException(

            status_code=403,

            detail="Invalid Telegram webhook secret"

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
# FASTAPI STARTUP
# =========================================================

@app.on_event("startup")
async def startup_event():

    try:

        await start_telegram_bot()

    except Exception as e:

        print(

            "Telegram startup error:",

            repr(e)

        )


# =========================================================
# FASTAPI SHUTDOWN
# =========================================================

@app.on_event("shutdown")
async def shutdown_event():

    global telegram_app

    if telegram_app is None:

        return

    try:

        await telegram_app.bot.delete_webhook()

    except Exception as e:

        print(

            "Telegram webhook delete error:",

            repr(e)

        )

    try:

        await telegram_app.stop()

    except Exception as e:

        print(

            "Telegram stop error:",

            repr(e)

        )

    try:

        await telegram_app.shutdown()

    except Exception as e:

        print(

            "Telegram shutdown error:",

            repr(e)

        )

    telegram_app = None


# =========================================================
# MODELS
# =========================================================

class TTSRequest(
    BaseModel
):

    text: str

    voice: str = (
        "ar-EG-SalmaNeural"
    )

    rate: str = "+0%"

    pitch: str = "+0Hz"

    installation_id: (
        str | None
    ) = None

    manufacturer: (
        str | None
    ) = None

    model: (
        str | None
    ) = None

    android_version: (
        str | None
    ) = None

    app_version: (
        str | None
    ) = None

    platform: str = "Android"


class ActivateRequest(
    BaseModel
):

    code: str


# =========================================================
# ADMIN MODELS
# =========================================================

class AdminLoginRequest(
    BaseModel
):

    username: str

    password: str


# =========================================================
# ADMIN AUTHENTICATION
# =========================================================

def _b64encode(data: bytes):

    return base64.urlsafe_b64encode(
        data
    ).rstrip(
        b"="
    ).decode(
        "ascii"
    )


def _b64decode(data: str):

    padding = "=" * (
        4 - len(data) % 4
    )

    return base64.urlsafe_b64decode(

        (
            data + padding
        ).encode(
            "ascii"
        )

    )


def create_admin_token():

    if not ADMIN_SECRET_KEY:

        raise RuntimeError(
            "ADMIN_SECRET_KEY is not configured"
        )

    payload = {

        "sub": "admin",

        "iat": int(
            time.time()
        ),

        "exp": int(
            time.time()
        ) + ADMIN_TOKEN_EXPIRE_SECONDS

    }

    payload_bytes = json.dumps(

        payload,

        separators=(
            ",",
            ":"
        ),

        sort_keys=True

    ).encode(
        "utf-8"
    )

    encoded_payload = _b64encode(
        payload_bytes
    )

    signature = hmac.new(

        ADMIN_SECRET_KEY.encode(
            "utf-8"
        ),

        encoded_payload.encode(
            "ascii"
        ),

        hashlib.sha256

    ).digest()

    return (

        encoded_payload

        + "."

        + _b64encode(
            signature
        )

    )


def verify_admin_token(
    token: str
):

    if not ADMIN_SECRET_KEY:

        return False

    if not token:

        return False

    try:

        parts = token.split(
            "."
        )

        if len(parts) != 2:

            return False

        encoded_payload = parts[0]

        encoded_signature = parts[1]

        expected_signature = hmac.new(

            ADMIN_SECRET_KEY.encode(
                "utf-8"
            ),

            encoded_payload.encode(
                "ascii"
            ),

            hashlib.sha256

        ).digest()

        received_signature = _b64decode(
            encoded_signature
        )

        if not hmac.compare_digest(

            expected_signature,

            received_signature

        ):

            return False

        payload = json.loads(

            _b64decode(
                encoded_payload
            ).decode(
                "utf-8"
            )

        )

        if payload.get("sub") != "admin":

            return False

        if int(payload.get("exp", 0)) < int(
            time.time()
        ):

            return False

        return True

    except Exception:

        return False


def require_admin(
    authorization: str | None
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

    token = authorization[7:].strip()

    if not verify_admin_token(token):

        raise HTTPException(

            status_code=401,

            detail="Invalid or expired admin token"

        )

    return True


# =========================================================
# ADMIN LOGIN
# =========================================================

@app.post(
    "/admin/login"
)
async def admin_login(
    data: AdminLoginRequest
):

    if not ADMIN_USERNAME or not ADMIN_PASSWORD:

        raise HTTPException(

            status_code=503,

            detail="Admin authentication is not configured"

        )

    username_ok = hmac.compare_digest(

        data.username,

        ADMIN_USERNAME

    )

    password_ok = hmac.compare_digest(

        data.password,

        ADMIN_PASSWORD

    )

    if not username_ok or not password_ok:

        raise HTTPException(

            status_code=401,

            detail="Invalid admin credentials"

        )

    token = create_admin_token()

    return {

        "success": True,

        "token": token,

        "token_type": "Bearer",

        "expires_in": ADMIN_TOKEN_EXPIRE_SECONDS

    }


# =========================================================
# ADMIN ME
# =========================================================

@app.get(
    "/admin/me"
)
async def admin_me(

    authorization: str = Header(
        None
    )

):

    require_admin(
        authorization
    )

    return {

        "success": True,

        "role": "admin",

        "username": ADMIN_USERNAME

    }


# =========================================================
# ADMIN HEALTH
# =========================================================

@app.get(
    "/admin/health"
)
async def admin_health(

    authorization: str = Header(
        None
    )

):

    require_admin(
        authorization
    )

    database_ok = False

    try:

        conn = get_db()

        conn.execute(
            "SELECT 1"
        ).fetchone()

        conn.close()

        database_ok = True

    except Exception:

        database_ok = False

    return {

        "success": True,

        "server": "online",

        "service": "Edge TTS API",

        "database": (

            "online"

            if database_ok

            else "offline"

        ),

        "telegram_webhook": bool(

            TELEGRAM_BOT_TOKEN

            and PUBLIC_URL

            and TELEGRAM_WEBHOOK_SECRET

        ),

        "weekly_limit": WEEKLY_LIMIT,

        "current_week": get_current_week(),

        "time_utc":
            datetime.now(
                timezone.utc
            ).isoformat()

    }


# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.get(
    "/admin/dashboard"
)
async def admin_dashboard(

    authorization: str = Header(
        None
    )

):

    require_admin(
        authorization
    )

    current_week = get_current_week()

    conn = get_db()

    # -----------------------------------------------------
    # USERS
    # -----------------------------------------------------

    total_users = conn.execute("""

        SELECT COUNT(*)

        FROM users

    """).fetchone()[0]

    active_users = conn.execute("""

        SELECT COUNT(*)

        FROM users

        WHERE activated = 1

    """).fetchone()[0]

    inactive_users = (
        total_users
        -
        active_users
    )

    # -----------------------------------------------------
    # CURRENT WEEK USAGE
    # -----------------------------------------------------

    weekly_characters = conn.execute("""

        SELECT COALESCE(
            SUM(characters_used),
            0
        )

        FROM users

        WHERE week_id = ?

    """, (

        current_week,

    )).fetchone()[0]

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

    # -----------------------------------------------------
    # ALL-TIME CHARACTERS
    # -----------------------------------------------------

    total_characters_all_time = conn.execute("""

        SELECT COALESCE(
            SUM(character_count),
            0
        )

        FROM tts_requests

        WHERE success = 1

    """).fetchone()[0]

    # -----------------------------------------------------
    # TODAY REQUESTS
    # -----------------------------------------------------

    today_prefix = datetime.now(
        timezone.utc
    ).date().isoformat()

    today_requests = conn.execute("""

        SELECT COUNT(*)

        FROM tts_requests

        WHERE created_at LIKE ?

    """, (

        today_prefix + "%",

    )).fetchone()[0]

    # -----------------------------------------------------
    # TODAY CHARACTERS
    # -----------------------------------------------------

    today_characters = conn.execute("""

        SELECT COALESCE(
            SUM(character_count),
            0
        )

        FROM tts_requests

        WHERE success = 1

        AND created_at LIKE ?

    """, (

        today_prefix + "%",

    )).fetchone()[0]

    conn.close()

    usage_percent = 0

    if WEEKLY_LIMIT > 0:

        usage_percent = round(

            (
                weekly_characters
                /
                WEEKLY_LIMIT
            )
            *
            100,

            2

        )

    return {

        "success": True,

        "server": {

            "status": "online",

            "service": "Edge TTS API",

            "current_week":
                current_week

        },

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

        "requests": {

            "total":
                total_requests,

            "successful":
                successful_requests,

            "failed":
                failed_requests,

            "today":
                today_requests

        },

        "usage": {

            "weekly_limit":
                WEEKLY_LIMIT,

            "characters_used":
                weekly_characters,

            "characters_remaining":
                max(
                    0,
                    WEEKLY_LIMIT
                    -
                    weekly_characters
                ),

            "usage_percent":
                usage_percent,

            "today_characters":
                today_characters,

            "all_time_characters":
                total_characters_all_time

        }

    }


# =========================================================
# ADMIN USERS LIST
# =========================================================

@app.get(
    "/admin/users"
)
async def admin_users(

    authorization: str = Header(
        None
    ),

    search: str | None = None,

    activated: int | None = None,

    limit: int = 50,

    offset: int = 0

):

    require_admin(
        authorization
    )

    limit = max(
        1,
        min(
            limit,
            200
        )
    )

    offset = max(
        0,
        offset
    )

    conn = get_db()

    where = []

    params = []

    if search:

        where.append("""

            telegram_user_id LIKE ?

        """)

        params.append(
            f"%{search}%"
        )

    if activated is not None:

        where.append("""

            activated = ?

        """)

        params.append(
            1 if activated else 0
        )

    where_sql = ""

    if where:

        where_sql = (
            " WHERE "
            +
            " AND ".join(
                where
            )
        )

    total = conn.execute(

        "SELECT COUNT(*) FROM users"
        +
        where_sql,

        params

    ).fetchone()[0]

    rows = conn.execute(

        """

        SELECT *

        FROM users

        """

        +
        where_sql

        +

        """

        ORDER BY id DESC

        LIMIT ?

        OFFSET ?

        """,

        params
        +
        [
            limit,
            offset
        ]

    ).fetchall()

    result = []

    for row in rows:

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

        request_count = conn.execute("""

            SELECT COUNT(*)

            FROM tts_requests

            WHERE telegram_user_id = ?

        """, (

            telegram_id,

        )).fetchone()[0]

        last_activity = conn.execute("""

            SELECT MAX(created_at)

            FROM tts_requests

            WHERE telegram_user_id = ?

        """, (

            telegram_id,

        )).fetchone()[0]

        result.append({

            "id":
                row["id"],

            "telegram_user_id":
                telegram_id,

            "activated":
                bool(row["activated"]),

            "characters_used":
                row["characters_used"],

            "characters_remaining":
                max(
                    0,
                    WEEKLY_LIMIT
                    -
                    row["characters_used"]
                ),

            "week_id":
                row["week_id"],

            "code_week_id":
                row["code_week_id"],

            "device_count":
                device_count,

            "request_count":
                request_count,

            "last_activity":
                last_activity

        })

    conn.close()

    return {

        "success": True,

        "total": total,

        "limit": limit,

        "offset": offset,

        "users": result

    }


# =========================================================
# ADMIN USER DETAILS
# =========================================================

@app.get(
    "/admin/users/{telegram_id}"
)
async def admin_user_details(

    telegram_id: str,

    authorization: str = Header(
        None
    )

):

    require_admin(
        authorization
    )

    conn = get_db()

    user = conn.execute("""

        SELECT *

        FROM users

        WHERE telegram_user_id = ?

    """, (

        telegram_id,

    )).fetchone()

    if not user:

        conn.close()

        raise HTTPException(

            status_code=404,

            detail="User not found"

        )

    devices = conn.execute("""

        SELECT *

        FROM devices

        WHERE telegram_user_id = ?

        ORDER BY last_seen DESC

    """, (

        telegram_id,

    )).fetchall()

    requests = conn.execute("""

        SELECT *

        FROM tts_requests

        WHERE telegram_user_id = ?

        ORDER BY id DESC

        LIMIT 100

    """, (

        telegram_id,

    )).fetchall()

    total_requests = conn.execute("""

        SELECT COUNT(*)

        FROM tts_requests

        WHERE telegram_user_id = ?

    """, (

        telegram_id,

    )).fetchone()[0]

    successful_requests = conn.execute("""

        SELECT COUNT(*)

        FROM tts_requests

        WHERE telegram_user_id = ?

        AND success = 1

    """, (

        telegram_id,

    )).fetchone()[0]

    failed_requests = conn.execute("""

        SELECT COUNT(*)

        FROM tts_requests

        WHERE telegram_user_id = ?

        AND success = 0

    """, (

        telegram_id,

    )).fetchone()[0]

    conn.close()

    device_list = []

    for device in devices:

        device_list.append({

            "id":
                device["id"],

            "installation_id":
                device["installation_id"],

            "telegram_user_id":
                device["telegram_user_id"],

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

    request_list = []

    for request in requests:

        request_list.append({

            "id":
                request["id"],

            "installation_id":
                request["installation_id"],

            "character_count":
                request["character_count"],

            "voice":
                request["voice"],

            "rate":
                request["rate"],

            "pitch":
                request["pitch"],

            "generation_time_ms":
                request["generation_time_ms"],

            "file_size_bytes":
                request["file_size_bytes"],

            "success":
                bool(request["success"]),

            "created_at":
                request["created_at"]

        })

    return {

        "success": True,

        "user": {

            "id":
                user["id"],

            "telegram_user_id":
                user["telegram_user_id"],

            "activated":
                bool(user["activated"]),

            "characters_used":
                user["characters_used"],

            "characters_remaining":
                max(
                    0,
                    WEEKLY_LIMIT
                    -
                    user["characters_used"]
                ),

            "week_id":
                user["week_id"],

            "code_week_id":
                user["code_week_id"]

        },

        "statistics": {

            "total_requests":
                total_requests,

            "successful_requests":
                successful_requests,

            "failed_requests":
                failed_requests

        },

        "devices":
            device_list,

        "requests":
            request_list

    }


# =========================================================
# ADMIN ACTIVATE USER
# =========================================================

@app.post(
    "/admin/users/{telegram_id}/activate"
)
async def admin_activate_user(

    telegram_id: str,

    authorization: str = Header(
        None
    )

):

    require_admin(
        authorization
    )

    current_week = get_current_week()

    conn = get_db()

    user = conn.execute("""

        SELECT *

        FROM users

        WHERE telegram_user_id = ?

    """, (

        telegram_id,

    )).fetchone()

    if not user:

        conn.close()

        raise HTTPException(

            status_code=404,

            detail="User not found"

        )

    api_key = user["api_key"]

    if not api_key:

        api_key = generate_api_key()

    conn.execute("""

        UPDATE users

        SET

            activated = 1,

            api_key = ?,

            week_id = ?

        WHERE telegram_user_id = ?

    """, (

        api_key,

        current_week,

        telegram_id

    ))

    conn.commit()

    conn.close()

    return {

        "success": True,

        "message": "User activated",

        "telegram_user_id":
            telegram_id,

        "activated": True

    }


# =========================================================
# ADMIN DEACTIVATE USER
# =========================================================

@app.post(
    "/admin/users/{telegram_id}/deactivate"
)
async def admin_deactivate_user(

    telegram_id: str,

    authorization: str = Header(
        None
    )

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

        telegram_id,

    )).fetchone()

    if not user:

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

        telegram_id,

    ))

    conn.commit()

    conn.close()

    return {

        "success": True,

        "message": "User deactivated",

        "telegram_user_id":
            telegram_id,

        "activated": False

    }


# =========================================================
# ADMIN RESET USAGE
# =========================================================

@app.post(
    "/admin/users/{telegram_id}/reset-usage"
)
async def admin_reset_usage(

    telegram_id: str,

    authorization: str = Header(
        None
    )

):

    require_admin(
        authorization
    )

    current_week = get_current_week()

    conn = get_db()

    user = conn.execute("""

        SELECT id

        FROM users

        WHERE telegram_user_id = ?

    """, (

        telegram_id,

    )).fetchone()

    if not user:

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

        telegram_id

    ))

    conn.commit()

    conn.close()

    return {

        "success": True,

        "message":
            "User weekly usage reset",

        "telegram_user_id":
            telegram_id,

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
    "/admin/users/{telegram_id}/regenerate-key"
)
async def admin_regenerate_key(

    telegram_id: str,

    authorization: str = Header(
        None
    )

):

    require_admin(
        authorization
    )

    new_api_key = generate_api_key()

    conn = get_db()

    user = conn.execute("""

        SELECT id

        FROM users

        WHERE telegram_user_id = ?

    """, (

        telegram_id,

    )).fetchone()

    if not user:

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

        telegram_id

    ))

    conn.commit()

    conn.close()

    return {

        "success": True,

        "message":
            "API key regenerated",

        "telegram_user_id":
            telegram_id,

        "api_key":
            new_api_key

    }


# =========================================================
# ADMIN REGENERATE ACTIVATION CODE
# =========================================================

@app.post(
    "/admin/users/{telegram_id}/regenerate-code"
)
async def admin_regenerate_code(

    telegram_id: str,

    authorization: str = Header(
        None
    )

):

    require_admin(
        authorization
    )

    new_code = generate_code()

    current_week = get_current_week()

    conn = get_db()

    user = conn.execute("""

        SELECT id

        FROM users

        WHERE telegram_user_id = ?

    """, (

        telegram_id,

    )).fetchone()

    if not user:

        conn.close()

        raise HTTPException(

            status_code=404,

            detail="User not found"

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

        telegram_id

    ))

    conn.commit()

    conn.close()

    return {

        "success": True,

        "message":
            "Activation code regenerated",

        "telegram_user_id":
            telegram_id,

        "activation_code":
            new_code,

        "code_week_id":
            current_week

    }


# =========================================================
# ADMIN DEVICES
# =========================================================

@app.get(
    "/admin/devices"
)
async def admin_devices(

    authorization: str = Header(
        None
    ),

    search: str | None = None,

    limit: int = 100,

    offset: int = 0

):

    require_admin(
        authorization
    )

    limit = max(
        1,
        min(
            limit,
            300
        )
    )

    offset = max(
        0,
        offset
    )

    conn = get_db()

    if search:

        pattern = f"%{search}%"

        total = conn.execute("""

            SELECT COUNT(*)

            FROM devices

            WHERE

                telegram_user_id LIKE ?

                OR installation_id LIKE ?

                OR manufacturer LIKE ?

                OR model LIKE ?

        """, (

            pattern,
            pattern,
            pattern,
            pattern

        )).fetchone()[0]

        rows = conn.execute("""

            SELECT *

            FROM devices

            WHERE

                telegram_user_id LIKE ?

                OR installation_id LIKE ?

                OR manufacturer LIKE ?

                OR model LIKE ?

            ORDER BY last_seen DESC

            LIMIT ?

            OFFSET ?

        """, (

            pattern,
            pattern,
            pattern,
            pattern,
            limit,
            offset

        )).fetchall()

    else:

        total = conn.execute("""

            SELECT COUNT(*)

            FROM devices

        """).fetchone()[0]

        rows = conn.execute("""

            SELECT *

            FROM devices

            ORDER BY last_seen DESC

            LIMIT ?

            OFFSET ?

        """, (

            limit,
            offset

        )).fetchall()

    conn.close()

    devices = []

    for row in rows:

        devices.append({

            "id":
                row["id"],

            "installation_id":
                row["installation_id"],

            "telegram_user_id":
                row["telegram_user_id"],

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

    return {

        "success": True,

        "total": total,

        "limit": limit,

        "offset": offset,

        "devices": devices

    }


# =========================================================
# ADMIN REQUESTS
# =========================================================

@app.get(
    "/admin/requests"
)
async def admin_requests(

    authorization: str = Header(
        None
    ),

    telegram_id: str | None = None,

    success: int | None = None,

    limit: int = 100,

    offset: int = 0

):

    require_admin(
        authorization
    )

    limit = max(
        1,
        min(
            limit,
            300
        )
    )

    offset = max(
        0,
        offset
    )

    conn = get_db()

    where = []

    params = []

    if telegram_id:

        where.append("""

            telegram_user_id = ?

        """)

        params.append(
            telegram_id
        )

    if success is not None:

        where.append("""

            success = ?

        """)

        params.append(
            1 if success else 0
        )

    where_sql = ""

    if where:

        where_sql = (

            " WHERE "

            +

            " AND ".join(
                where
            )

        )

    total = conn.execute(

        """

        SELECT COUNT(*)

        FROM tts_requests

        """

        +

        where_sql,

        params

    ).fetchone()[0]

    rows = conn.execute(

        """

        SELECT *

        FROM tts_requests

        """

        +

        where_sql

        +

        """

        ORDER BY id DESC

        LIMIT ?

        OFFSET ?

        """,

        params
        +
        [
            limit,
            offset
        ]

    ).fetchall()

    conn.close()

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

    return {

        "success": True,

        "total": total,

        "limit": limit,

        "offset": offset,

        "requests": requests

    }


# =========================================================
# ADMIN USER DEVICES
# =========================================================

@app.get(
    "/admin/users/{telegram_id}/devices"
)
async def admin_user_devices(

    telegram_id: str,

    authorization: str = Header(
        None
    )

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

        telegram_id,

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

        telegram_id,

    )).fetchall()

    conn.close()

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

    return {

        "success": True,

        "telegram_user_id":
            telegram_id,

        "devices":
            devices

    }


# =========================================================
# ADMIN USER REQUESTS
# =========================================================

@app.get(
    "/admin/users/{telegram_id}/requests"
)
async def admin_user_requests(

    telegram_id: str,

    authorization: str = Header(
        None
    ),

    limit: int = 100,

    offset: int = 0

):

    require_admin(
        authorization
    )

    limit = max(
        1,
        min(
            limit,
            300
        )
    )

    offset = max(
        0,
        offset
    )

    conn = get_db()

    user = conn.execute("""

        SELECT id

        FROM users

        WHERE telegram_user_id = ?

    """, (

        telegram_id,

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

        telegram_id,

    )).fetchone()[0]

    rows = conn.execute("""

        SELECT *

        FROM tts_requests

        WHERE telegram_user_id = ?

        ORDER BY id DESC

        LIMIT ?

        OFFSET ?

    """, (

        telegram_id,
        limit,
        offset

    )).fetchall()

    conn.close()

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

    return {

        "success": True,

        "telegram_user_id":
            telegram_id,

        "total":
            total,

        "limit":
            limit,

        "offset":
            offset,

        "requests":
            requests

    }


# =========================================================
# HOME API
# =========================================================

@app.get("/")
async def home():

    return {

        "status": "online",

        "service":
            "Edge TTS API",

        "weekly_limit":
            WEEKLY_LIMIT,

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
            bool(

                ADMIN_USERNAME

                and

                ADMIN_PASSWORD

                and

                ADMIN_SECRET_KEY

            )

    }


# =========================================================
# ACTIVATE API
# =========================================================

@app.post(
    "/activate"
)
async def activate(

    data: ActivateRequest

):

    code = data.code.strip().upper()

    if not code:

        raise HTTPException(

            status_code=400,

            detail=
                "Activation code is required"

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

            detail=
                "Invalid activation code"

        )

    current_week = get_current_week()

    if (
        user["code_week_id"]
        !=
        current_week
    ):

        conn.close()

        raise HTTPException(

            status_code=400,

            detail=
                "Activation code expired"

        )

    api_key = user[
        "api_key"
    ]

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
    "/account"
)
async def account(

    authorization: str = Header(
        None
    )

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

            detail=
                "Invalid API key"

        )

    reset_week_if_needed(
        user
    )

    user = get_user_by_telegram_id(

        user["telegram_user_id"]

    )

    used = user[
        "characters_used"
    ]

    remaining = max(

        0,

        WEEKLY_LIMIT - used

    )

    return {

        "success": True,

        "weekly_limit":
            WEEKLY_LIMIT,

        "characters_used":
            used,

        "characters_remaining":
            remaining,

        "week":
            get_current_week()

    }


# =========================================================
# TTS API
# =========================================================

@app.post(
    "/tts"
)
async def tts(

    data: TTSRequest,

    authorization: str = Header(
        None
    )

):

    # -----------------------------------------------------
    # AUTH
    # -----------------------------------------------------

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

            detail=
                "Invalid API key"

        )

    if not user["activated"]:

        raise HTTPException(

            status_code=403,

            detail=
                "Account is not activated"

        )

    # -----------------------------------------------------
    # WEEK RESET
    # -----------------------------------------------------

    reset_week_if_needed(
        user
    )

    user = get_user_by_telegram_id(

        user["telegram_user_id"]

    )

    # -----------------------------------------------------
    # TEXT VALIDATION
    # -----------------------------------------------------

    if (

        not data.text

        or

        not data.text.strip()

    ):

        raise HTTPException(

            status_code=400,

            detail=
                "Text is empty"

        )

    character_count = len(
        data.text
    )

    # -----------------------------------------------------
    # QUOTA CHECK
    # -----------------------------------------------------

    remaining = (

        WEEKLY_LIMIT

        -

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
