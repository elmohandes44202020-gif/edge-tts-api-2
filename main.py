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

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
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

    conn.commit()
    conn.close()


init_db()


# =========================================================
# WEEK SYSTEM
# =========================================================

def get_current_week():

    from datetime import datetime, timezone

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
        "".join(secrets.choice(chars) for _ in range(4))
        for _ in range(3)
    )


def generate_api_key():

    return secrets.token_urlsafe(32)


# =========================================================
# TELEGRAM BOT
# =========================================================

async def telegram_start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    await update.message.reply_text(
        f"""
أهلاً {user.first_name} 👋

أنت الآن داخل بوت Edge TTS.

🎁 للحصول على الكود المجاني الأسبوعي:

/trial

لمعرفة حسابك ورصيدك:

/account
"""
    )


async def telegram_trial(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    telegram_id = str(user.id)

    current_week = get_current_week()

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (telegram_id,)).fetchone()

    # -----------------------------------------------------
    # USER EXISTS
    # -----------------------------------------------------

    if row:

        # لو أسبوع جديد
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

افتح التطبيق وأدخل الكود.
""",
                parse_mode="Markdown"
            )

            return

        # نفس الأسبوع
        else:

            conn.close()

            await update.message.reply_text(
                """
⚠️ لقد حصلت بالفعل على كودك المجاني هذا الأسبوع.

كل حساب Telegram يحصل على:

🎁 200,000 حرف أسبوعيًا

انتظر بداية الأسبوع القادم للحصول على الكود الجديد.
"""
            )

            return

    # -----------------------------------------------------
    # NEW USER
    # -----------------------------------------------------

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

افتح تطبيق Edge TTS وأدخل الكود.

📅 الرصيد يتجدد كل أسبوع.
""",
        parse_mode="Markdown"
    )


async def telegram_account(update: Update, context: ContextTypes.DEFAULT_TYPE):

    telegram_id = str(update.effective_user.id)

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (telegram_id,)).fetchone()

    conn.close()

    if not row:

        await update.message.reply_text(
            "ليس لديك حساب بعد.\n\nاستخدم /trial للحصول على التجربة المجانية."
        )

        return

    reset_week_if_needed(row)

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE telegram_user_id = ?
    """, (telegram_id,)).fetchone()

    conn.close()

    used = row["characters_used"]

    remaining = max(
        0,
        WEEKLY_LIMIT - used
    )

    await update.message.reply_text(
        f"""
📊 حسابك

🎁 الحد الأسبوعي:
{WEEKLY_LIMIT:,} حرف

📝 المستخدم:
{used:,} حرف

💰 المتبقي:
{remaining:,} حرف

📅 التجديد:
أسبوعيًا
"""
    )


# =========================================================
# TELEGRAM BOT STARTUP
# =========================================================

telegram_app = None


async def start_telegram_bot():

    global telegram_app

    if not TELEGRAM_BOT_TOKEN:

        print("Telegram bot disabled: TELEGRAM_BOT_TOKEN missing")

        return

    telegram_app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    telegram_app.add_handler(
        CommandHandler("start", telegram_start)
    )

    telegram_app.add_handler(
        CommandHandler("trial", telegram_trial)
    )

    telegram_app.add_handler(
        CommandHandler("account", telegram_account)
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


class ActivateRequest(BaseModel):

    code: str


# =========================================================
# HOME
# =========================================================

@app.get("/")
async def home():

    return {
        "status": "online",
        "service": "Edge TTS API",
        "weekly_limit": WEEKLY_LIMIT
    }


# =========================================================
# ACTIVATE
# =========================================================

@app.post("/activate")
async def activate(data: ActivateRequest):

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
    """, (code,)).fetchone()

    if not user:

        conn.close()

        raise HTTPException(
            status_code=404,
            detail="Invalid activation code"
        )

    current_week = get_current_week()

    # -----------------------------------------------------
    # Make sure code belongs to current week
    # -----------------------------------------------------

    if user["code_week_id"] != current_week:

        conn.close()

        raise HTTPException(
            status_code=400,
            detail="Activation code expired"
        )

    # -----------------------------------------------------
    # Generate API key if needed
    # -----------------------------------------------------

    api_key = user["api_key"]

    if not api_key:

        api_key = generate_api_key()

    # -----------------------------------------------------
    # Activate account
    # -----------------------------------------------------

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
# ACCOUNT
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
    """, (api_key,)).fetchone()

    conn.close()

    if not user:

        raise HTTPException(
            status_code=401,
            detail="Invalid API key"
        )

    reset_week_if_needed(user)

    conn = get_db()

    user = conn.execute("""
        SELECT *
        FROM users
        WHERE api_key = ?
    """, (api_key,)).fetchone()

    conn.close()

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
# TTS
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
    """, (api_key,)).fetchone()

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

    conn = get_db()

    user = conn.execute("""
        SELECT *
        FROM users
        WHERE api_key = ?
    """, (api_key,)).fetchone()

    conn.close()

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

    remaining = WEEKLY_LIMIT - user["characters_used"]

    if character_count > remaining:

        raise HTTPException(
            status_code=429,
            detail={
                "message": "Weekly character limit exceeded",
                "weekly_limit": WEEKLY_LIMIT,
                "characters_used": user["characters_used"],
                "characters_remaining": remaining,
                "requested": character_count
            }
        )

    # -----------------------------------------------------
    # CREATE TEMP FILE
    # -----------------------------------------------------

    temp_file = tempfile.NamedTemporaryFile(
        suffix=".mp3",
        delete=False
    )

    temp_file.close()

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

        # -------------------------------------------------
        # DEDUCT CHARACTERS ONLY AFTER SUCCESS
        # -------------------------------------------------

        conn = get_db()

        conn.execute("""
            UPDATE users
            SET characters_used = characters_used + ?
            WHERE api_key = ?
        """, (
            character_count,
            api_key
        ))

        conn.commit()

        conn.close()

        return Response(
            content=audio,
            media_type="audio/mpeg",
            headers={
                "Content-Disposition":
                    'attachment; filename="speech.mp3"'
            }
        )

    except Exception as e:

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