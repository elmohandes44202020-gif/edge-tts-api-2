from fastapi import FastAPI, HTTPException, Header
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    print("WARNING: TELEGRAM_BOT_TOKEN is not set")

app = FastAPI(title="Edge TTS API")


# =========================================================
# DATABASE
# =========================================================

def get_db():

    conn = sqlite3.connect(DATABASE)

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
        CREATE INDEX IF NOT EXISTS idx_requests_installation
        ON tts_requests(installation_id)
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
# USER HELPERS
# =========================================================

def get_user_by_telegram_id(telegram_id):

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


def create_or_get_user(telegram_id):

    telegram_id = str(telegram_id)

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

    now = datetime.now(timezone.utc).isoformat()

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

    now = datetime.now(timezone.utc).isoformat()

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

    return InlineKeyboardMarkup(keyboard)


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

    telegram_id = str(user.id)

    row = create_or_get_user(telegram_id)

    reset_week_if_needed(row)

    row = get_user_by_telegram_id(telegram_id)

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

    telegram_id = str(user.id)

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

        row = create_or_get_user(telegram_id)

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

    telegram_id = str(user.id)

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

    telegram_id = str(query.from_user.id)

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
"""
        ,

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

        await button_start_use(query)

    elif action == "activate_account":

        await button_activate_account(query)

    elif action == "confirm_activation":

        await button_confirm_activation(query)

    elif action == "account":

        await button_account(query)

    elif action == "link_app":

        await button_link_app(query)

    elif action == "help":

        await button_help(query)

    elif action == "home":

        await button_home(query)


# =========================================================
# LEGACY /TRIAL
# =========================================================

async def telegram_trial(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    telegram_id = str(user.id)

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
""",

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


async def start_telegram_bot():

    global telegram_app

    if not TELEGRAM_BOT_TOKEN:

        print(
            "Telegram bot disabled: TELEGRAM_BOT_TOKEN missing"
        )

        return

    telegram_app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
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

    await telegram_app.initialize()

    await telegram_app.start()

    await telegram_app.updater.start_polling()

    print("Telegram bot started")


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

    if not authorization.startswith("Bearer "):

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

    if not authorization.startswith("Bearer "):

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

        file_size_bytes = len(audio)

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
