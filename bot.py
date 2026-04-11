import os
import logging
import asyncio
import uuid
from datetime import datetime
import pytz
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import google.generativeai as genai
from supabase import create_client
import json
import re

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Config
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
TZ = pytz.timezone("America/Argentina/Buenos_Aires")

# Clients
genai.configure(api_key=GEMINI_API_KEY)
gemini  = genai.GenerativeModel("gemini-1.5-flash")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Scheduler en memoria (jobs se persisten en Supabase)
scheduler = AsyncIOScheduler(timezone=TZ)

# ─────────────────────────────────────────────
# SUPABASE helpers
# ─────────────────────────────────────────────

def save_reminder(chat_id: int, reminder_text: str, scheduled_at: datetime, job_id: str):
    supabase.table("recordatorios").insert({
        "chat_id":      chat_id,
        "texto":        reminder_text,
        "scheduled_at": scheduled_at.isoformat(),
        "job_id":       job_id,
        "enviado":      False,
    }).execute()

def mark_sent(job_id: str):
    supabase.table("recordatorios").update({"enviado": True}).eq("job_id", job_id).execute()

def get_pending_reminders(chat_id: int):
    result = supabase.table("recordatorios")\
        .select("*")\
        .eq("chat_id", chat_id)\
        .eq("enviado", False)\
        .order("scheduled_at")\
        .execute()
    return result.data

def delete_reminder(job_id: str, chat_id: int):
    supabase.table("recordatorios").delete()\
        .eq("job_id", job_id)\
        .eq("chat_id", chat_id)\
        .execute()

def load_pending_from_supabase():
    """Al iniciar, recarga todos los recordatorios pendientes."""
    result = supabase.table("recordatorios")\
        .select("*")\
        .eq("enviado", False)\
        .execute()
    return result.data

# ─────────────────────────────────────────────
# GEMINI helpers
# ─────────────────────────────────────────────

def parse_reminder_with_gemini(user_text: str, now: datetime) -> dict | None:
    """Extrae fecha/hora y texto del recordatorio usando Gemini."""
    prompt = f"""Hoy es {now.strftime('%A %d/%m/%Y %H:%M')} (hora Argentina, UTC-3).

El usuario escribió: "{user_text}"

Si es un pedido de recordatorio, extraé:
- fecha y hora exacta en formato ISO 8601 con timezone -03:00
- el texto del recordatorio (qué hay que recordar)

Respondé SOLO con JSON válido, sin markdown, sin explicaciones:
{{"es_recordatorio": true, "datetime": "2025-01-15T10:00:00-03:00", "texto": "llamar al médico"}}

Si NO es un pedido de recordatorio:
{{"es_recordatorio": false}}

Reglas:
- "mañana" = día siguiente
- "la semana que viene" = lunes próximo
- si dice solo hora sin día = hoy si la hora no pasó, mañana si ya pasó
- "a la tardecita" = 18:00, "a la mañana" = 09:00, "al mediodía" = 12:00
- "en X minutos/horas" = ahora + X
"""
    try:
        response = gemini.generate_content(prompt)
        text = response.text.strip()
        text = re.sub(r"```json|```", "", text).strip()
        return json.loads(text)
    except json.JSONDecodeError:
        logger.error(f"Error parseando JSON de Gemini: {text}")
        return None
    except Exception as e:
        logger.error(f"Error llamando a Gemini: {e}")
        return None

def analyze_image_with_gemini(image_bytes: bytes, mime_type: str, caption: str = "") -> str:
    """Analiza una imagen y devuelve descripción o respuesta."""
    prompt = caption if caption else "Describí esta imagen en detalle en español."

    if any(w in caption.lower() for w in ["recordame", "recordá", "recorda", "reminder"]):
        prompt = f"""El usuario mandó esta imagen con el mensaje: "{caption}"
Describí qué hay en la imagen y si hay fechas, eventos, o información relevante para un recordatorio."""

    response = gemini.generate_content([
        {"mime_type": mime_type, "data": image_bytes},
        prompt
    ])
    return response.text

# ─────────────────────────────────────────────
# SCHEDULER: enviar recordatorio
# ─────────────────────────────────────────────

async def send_reminder(chat_id: int, text: str, job_id: str):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(
        chat_id=chat_id,
        text=f"🔔 *Recordatorio*\n\n{text}",
        parse_mode="Markdown"
    )
    mark_sent(job_id)
    logger.info(f"Recordatorio enviado: {job_id}")

# ─────────────────────────────────────────────
# HANDLERS
# ─────────────────────────────────────────────

def is_allowed(update: Update) -> bool:
    if ALLOWED_USER_ID == 0:
        return True
    return update.effective_user.id == ALLOWED_USER_ID

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    text    = update.message.text
    chat_id = update.effective_chat.id
    now     = datetime.now(TZ)

    parsed = parse_reminder_with_gemini(text, now)

    if parsed is None:
        await update.message.reply_text("⚠️ No pude procesar tu mensaje ahora (límite de API). Intentá en un momento.")
        return

    if parsed and parsed.get("es_recordatorio"):
        try:
            scheduled_at  = datetime.fromisoformat(parsed["datetime"]).astimezone(TZ)
            reminder_text = parsed["texto"]

            if scheduled_at <= now:
                await update.message.reply_text("⚠️ Esa fecha/hora ya pasó. ¿Querés que lo programe para otro momento?")
                return

            job_id = str(uuid.uuid4())
            scheduler.add_job(
                send_reminder,
                trigger="date",
                run_date=scheduled_at,
                args=[chat_id, reminder_text, job_id],
                id=job_id,
            )
            save_reminder(chat_id, reminder_text, scheduled_at, job_id)

            fecha_str = scheduled_at.strftime("%A %d/%m/%Y a las %H:%M")
            await update.message.reply_text(
                f"✅ ¡Listo! Te recuerdo el *{fecha_str}*:\n_{reminder_text}_",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Error agendando recordatorio: {e}")
            await update.message.reply_text("❌ No pude agendar el recordatorio. Intentá de nuevo.")
    else:
        response = gemini.generate_content(
            f"Sos un asistente personal amigable. Respondé en español rioplatense, breve y directo.\n\nUsuario: {text}"
        )
        await update.message.reply_text(response.text)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    caption = update.message.caption or ""
    now     = datetime.now(TZ)

    photo      = update.message.photo[-1]
    file       = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()

    analysis = analyze_image_with_gemini(bytes(image_bytes), "image/jpeg", caption)

    if any(w in caption.lower() for w in ["recordame", "recordá", "recorda"]):
        combined_text = f"{caption} — Imagen: {analysis}"
        parsed = parse_reminder_with_gemini(combined_text, now)

        if parsed and parsed.get("es_recordatorio"):
            try:
                scheduled_at  = datetime.fromisoformat(parsed["datetime"]).astimezone(TZ)
                reminder_text = parsed.get("texto", analysis[:200])

                job_id = str(uuid.uuid4())
                scheduler.add_job(
                    send_reminder,
                    trigger="date",
                    run_date=scheduled_at,
                    args=[chat_id, f"{reminder_text}\n\n📷 _(imagen adjunta en el recordatorio original)_", job_id],
                    id=job_id,
                )
                save_reminder(chat_id, reminder_text, scheduled_at, job_id)

                fecha_str = scheduled_at.strftime("%A %d/%m/%Y a las %H:%M")
                await update.message.reply_text(
                    f"✅ Recordatorio agendado para el *{fecha_str}*:\n_{reminder_text}_\n\n📷 Imagen analizada: {analysis[:300]}",
                    parse_mode="Markdown"
                )
                return
            except Exception as e:
                logger.error(f"Error agendando recordatorio con imagen: {e}")

    await update.message.reply_text(f"🖼️ {analysis}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    doc = update.message.document
    if doc.mime_type and doc.mime_type.startswith("image/"):
        file        = await context.bot.get_file(doc.file_id)
        image_bytes = await file.download_as_bytearray()
        caption     = update.message.caption or ""
        analysis    = analyze_image_with_gemini(bytes(image_bytes), doc.mime_type, caption)
        await update.message.reply_text(f"🖼️ {analysis}")
    else:
        await update.message.reply_text("Por ahora solo proceso imágenes. ¡Mandame una foto!")

# ─────────────────────────────────────────────
# COMANDOS
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"👋 ¡Hola! Soy tu bot de recordatorios.\n\n"
        f"Tu chat ID es: `{chat_id}`\n\n"
        f"Podés decirme cosas como:\n"
        f"• _Recordame mañana a las 10hs llamar al médico_\n"
        f"• _Recordame el viernes a las 18hs comprar pan_\n"
        f"• _Recordame en 2 horas revisar el horno_\n\n"
        f"También podés mandarme fotos con o sin mensaje.",
        parse_mode="Markdown"
    )

async def cmd_lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id   = update.effective_chat.id
    reminders = get_pending_reminders(chat_id)

    if not reminders:
        await update.message.reply_text("No tenés recordatorios pendientes. 🎉")
        return

    lines = ["📋 *Tus recordatorios pendientes:*\n"]
    for i, r in enumerate(reminders, 1):
        dt        = datetime.fromisoformat(r["scheduled_at"]).astimezone(TZ)
        fecha_str = dt.strftime("%a %d/%m %H:%M")
        lines.append(f"{i}. {fecha_str} — {r['texto']}\n   ID: `{r['job_id'][:8]}...`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_borrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    if not context.args:
        await update.message.reply_text("Usá `/borrar <job_id>` con los primeros caracteres del ID que ves en /lista")
        return

    partial_id = context.args[0]
    chat_id    = update.effective_chat.id
    reminders  = get_pending_reminders(chat_id)

    match = next((r for r in reminders if r["job_id"].startswith(partial_id)), None)
    if not match:
        await update.message.reply_text("No encontré ese recordatorio.")
        return

    job_id = match["job_id"]
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    delete_reminder(job_id, chat_id)
    await update.message.reply_text(f"🗑️ Recordatorio eliminado: _{match['texto']}_", parse_mode="Markdown")

# ─────────────────────────────────────────────
# STARTUP: recargar recordatorios pendientes
# ─────────────────────────────────────────────

async def post_init(app: Application) -> None:
    """
    PTB v21: este callback corre dentro del event loop del bot,
    antes de que empiece el polling. Lugar correcto para iniciar
    el scheduler y restaurar jobs async.
    """
    scheduler.start()

    pending  = load_pending_from_supabase()
    now      = datetime.now(TZ)
    restored = 0
    expired  = 0

    for r in pending:
        scheduled_at = datetime.fromisoformat(r["scheduled_at"]).astimezone(TZ)
        job_id  = r["job_id"]
        chat_id = r["chat_id"]
        text    = r["texto"]

        if scheduled_at <= now:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"🔔 *Recordatorio* _(llegó con retraso)_\n\n{text}",
                    parse_mode="Markdown"
                )
                mark_sent(job_id)
                expired += 1
            except Exception as e:
                logger.error(f"Error enviando recordatorio expirado {job_id}: {e}")
        else:
            scheduler.add_job(
                send_reminder,
                trigger="date",
                run_date=scheduled_at,
                args=[chat_id, text, job_id],
                id=job_id,
            )
            restored += 1

    logger.info(f"Recordatorios restaurados: {restored} futuros, {expired} enviados tardíos")
    logger.info("Bot iniciado ✅")

# ─────────────────────────────────────────────
# MAIN — sin asyncio.run(), PTB maneja el loop
# ─────────────────────────────────────────────

def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("lista", cmd_lista))
    app.add_handler(CommandHandler("borrar", cmd_borrar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # run_polling() es síncrono en PTB v21 — maneja su propio event loop
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
