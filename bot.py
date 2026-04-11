import os
import logging
import asyncio
import uuid
from datetime import datetime
import pytz
import httpx
from telegram import Update, Bot, MenuButtonWebApp, WebAppInfo
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from supabase import create_client
import json
import re

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Config
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY  = os.environ["GEMINI_API_KEY"]
SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]
MINI_APP_URL    = os.environ.get("MINI_APP_URL", "")  # URL de GitHub Pages, ej: https://usuario.github.io/fosfovita-bot
# Soporta uno o varios IDs separados por coma: "123456,789012"
# Si está vacío o es "0", permite a todos
_raw_ids = os.environ.get("ALLOWED_USER_IDS", os.environ.get("ALLOWED_USER_ID", "0"))
ALLOWED_USER_IDS = set(int(x.strip()) for x in _raw_ids.split(",") if x.strip()) - {0}
TZ = pytz.timezone("America/Argentina/Buenos_Aires")

# Clients
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
supabase   = create_client(SUPABASE_URL, SUPABASE_KEY)

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

def upsert_usuario(chat_id: int, nombre: str):
    supabase.table("usuarios").upsert({
        "chat_id": chat_id,
        "nombre":  nombre,
    }).execute()

def get_usuario_por_nombre(nombre: str) -> dict | None:
    """Busca usuario por nombre (case-insensitive, coincidencia parcial)."""
    result = supabase.table("usuarios").select("*").execute()
    nombre_lower = nombre.lower().strip()
    for u in (result.data or []):
        if nombre_lower in u["nombre"].lower():
            return u
    return None

def get_nombre_usuario(chat_id: int) -> str:
    """Devuelve el nombre del usuario o 'alguien'."""
    result = supabase.table("usuarios").select("nombre").eq("chat_id", chat_id).execute()
    if result.data:
        return result.data[0]["nombre"]
    return "alguien"

# ─────────────────────────────────────────────
# GEMINI helpers
# ─────────────────────────────────────────────

async def _gemini_post(payload: dict, timeout: int = 45, notify_cb=None) -> str:
    """Llama a Gemini via REST con retry automático en caso de 429."""
    waits = [20, 40, 65]  # segundos de espera entre reintentos (RPM se resetea cada 60s)
    last_err = None
    for attempt, wait in enumerate([0] + waits):
        if wait:
            logger.warning(f"Gemini 429 — esperando {wait}s antes del intento {attempt+1}")
            if notify_cb:
                try:
                    await notify_cb(wait)
                except Exception:
                    pass
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(f"{GEMINI_URL}?key={GEMINI_API_KEY}", json=payload)
                if r.status_code == 429:
                    last_err = Exception(f"429 Too Many Requests")
                    continue
                r.raise_for_status()
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                last_err = e
                continue
            raise
    raise last_err

async def _gemini_text(prompt: str, notify_cb=None) -> str:
    """Llama a Gemini via REST y devuelve el texto de la respuesta."""
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    return await _gemini_post(payload, timeout=30, notify_cb=notify_cb)

async def _gemini_media(media_bytes: bytes, mime_type: str, prompt: str, notify_cb=None) -> str:
    """Llama a Gemini con audio o imagen + texto via REST."""
    import base64
    b64 = base64.b64encode(media_bytes).decode()
    payload = {"contents": [{"parts": [
        {"inline_data": {"mime_type": mime_type, "data": b64}},
        {"text": prompt},
    ]}]}
    return await _gemini_post(payload, timeout=45, notify_cb=notify_cb)

async def parse_reminder_with_gemini(user_text: str, now: datetime, notify_cb=None) -> dict | list | None:
    """
    Extrae recordatorio(s) usando Gemini.
    Devuelve: dict (uno), list (varios), o None (error).
    """
    prompt = f"""Hoy es {now.strftime('%A %d/%m/%Y %H:%M')} (hora Argentina, UTC-3). AÑO ACTUAL: {now.year}.

El usuario escribió: "{user_text}"

Respondé SOLO con JSON válido, sin markdown, sin explicaciones.

━━ CASO 1: UN solo recordatorio ━━
{{"es_recordatorio": true, "datetime": "{now.year}-05-15T09:00:00-03:00", "texto": "cumple de Juan"}}

Con destinatario explícito:
{{"es_recordatorio": true, "datetime": "{now.year}-05-15T09:00:00-03:00", "texto": "cumple de Juan", "para": "Lore"}}

━━ CASO 2: MÚLTIPLES recordatorios (lista, agenda, cumpleaños, etc.) ━━
Devolvé un array JSON con cada uno:
[
  {{"es_recordatorio": true, "datetime": "{now.year}-05-15T09:00:00-03:00", "texto": "cumple de Juan"}},
  {{"es_recordatorio": true, "datetime": "{now.year}-06-23T09:00:00-03:00", "texto": "cumple de María"}}
]

━━ CASO 3: NO es un recordatorio ━━
{{"es_recordatorio": false}}

Reglas de fecha (MUY IMPORTANTE):
- El año SIEMPRE es {now.year} salvo que el usuario diga explícitamente otro año
- "mañana" = {now.strftime('%Y-%m-%d')} + 1 día
- "la semana que viene" = lunes próximo de la semana siguiente
- si dice solo hora sin día = hoy ({now.strftime('%Y-%m-%d')}) si la hora no pasó, mañana si ya pasó
- "a la tardecita" = 18:00, "a la mañana" = 09:00, "al mediodía" = 12:00
- "en X minutos/horas" = ahora + X
- si NO se menciona hora = 09:00 para fechas futuras, 1 hora desde ahora para hoy/inmediato
"""
    try:
        text = await _gemini_text(prompt, notify_cb=notify_cb)
        text = re.sub(r"```json|```", "", text).strip()
        return json.loads(text)
    except json.JSONDecodeError:
        logger.error(f"Error parseando JSON de Gemini: {text}")
        return None
    except Exception as e:
        logger.error(f"Error llamando a Gemini: {e}")
        return None

async def analyze_image_with_gemini(image_bytes: bytes, mime_type: str, caption: str = "", notify_cb=None) -> str:
    """Analiza una imagen y devuelve descripción o respuesta."""
    prompt = caption if caption else "Describí esta imagen en detalle en español."
    if any(w in caption.lower() for w in ["recordame", "recordá", "recorda", "reminder"]):
        prompt = f"""El usuario mandó esta imagen con el mensaje: "{caption}"
Describí qué hay en la imagen y si hay fechas, eventos, o información relevante para un recordatorio."""
    try:
        return await _gemini_media(image_bytes, mime_type, prompt, notify_cb=notify_cb)
    except Exception as e:
        logger.error(f"Error analizando imagen con Gemini: {e}")
        return "No pude analizar la imagen en este momento."

async def analyze_voice_with_gemini(audio_bytes: bytes, now: datetime, notify_cb=None) -> dict:
    """
    Transcribe y analiza un mensaje de voz.
    Devuelve dict con 'transcripcion' y opcionalmente datos de recordatorio.
    """
    prompt = f"""Hoy es {now.strftime('%A %d/%m/%Y %H:%M')} (hora Argentina, UTC-3). AÑO ACTUAL: {now.year}.

Escuchá este mensaje de voz y:
1. Transcribilo literalmente
2. Si es un pedido de recordatorio, extraé fecha/hora y texto

Respondé SOLO con JSON válido, sin markdown:
Si es recordatorio: {{"transcripcion": "...", "es_recordatorio": true, "datetime": "{now.year}-01-15T10:00:00-03:00", "texto": "llamar al médico"}}
Si NO es recordatorio: {{"transcripcion": "...", "es_recordatorio": false}}

Reglas de fecha (MUY IMPORTANTE):
- El año SIEMPRE es {now.year} salvo que el usuario diga explícitamente otro año
- "mañana" = {now.strftime('%Y-%m-%d')} + 1 día
- si dice solo hora sin día = hoy ({now.strftime('%Y-%m-%d')}) si la hora no pasó, mañana si ya pasó
- "a la tardecita" = 18:00, "a la mañana" = 09:00, "al mediodía" = 12:00
- "en X minutos/horas" = ahora + X
- si NO se menciona ninguna hora ni fecha = exactamente 1 hora desde ahora
"""
    try:
        text = await _gemini_media(audio_bytes, "audio/ogg", prompt, notify_cb=notify_cb)
        text = re.sub(r"```json|```", "", text).strip()
        return json.loads(text)
    except json.JSONDecodeError:
        logger.error(f"Error parseando JSON de voz: {text}")
        return {"transcripcion": text, "es_recordatorio": False}
    except Exception as e:
        logger.error(f"Error analizando voz con Gemini: {e}")
        return None

# ─────────────────────────────────────────────
# SCHEDULER: enviar recordatorio
# ─────────────────────────────────────────────

async def send_reminder(chat_id: int, text: str, job_id: str):
    # Verificar que el recordatorio no fue eliminado desde la Mini App
    result = supabase.table("recordatorios")\
        .select("job_id")\
        .eq("job_id", job_id)\
        .eq("enviado", False)\
        .execute()
    if not result.data:
        logger.info(f"Recordatorio {job_id} ya fue eliminado o marcado — no se envía")
        return

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
    if not ALLOWED_USER_IDS:  # vacío = todos permitidos
        return True
    return update.effective_user.id in ALLOWED_USER_IDS

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    text    = update.message.text
    chat_id = update.effective_chat.id
    now     = datetime.now(TZ)

    _notified = False
    async def notify(secs):
        nonlocal _notified
        if not _notified:
            await update.message.reply_text("⏳ La API está ocupada, esperá un momento y lo proceso...")
            _notified = True

    parsed = await parse_reminder_with_gemini(text, now, notify_cb=notify)

    if parsed is None:
        await update.message.reply_text("⚠️ No pude procesar tu mensaje ahora (límite de API). Intentá en un momento.")
        return

    # ── Lista de recordatorios ──
    if isinstance(parsed, list):
        items = [p for p in parsed if isinstance(p, dict) and p.get("es_recordatorio")]
        if not items:
            await update.message.reply_text("❓ No encontré recordatorios válidos en la lista.")
            return
        ok, fail = 0, 0
        lines = []
        for item in items:
            try:
                resultado = await agendar_recordatorio(update, item, chat_id, silent=True)
                if resultado:
                    dt = datetime.fromisoformat(item["datetime"]).astimezone(TZ)
                    lines.append(f"✅ {dt.strftime('%d/%m/%Y')} — {item['texto']}")
                    ok += 1
                else:
                    lines.append(f"⚠️ (fecha pasada) — {item.get('texto','')}")
                    fail += 1
            except Exception as e:
                logger.error(f"Error agendando item de lista: {e}")
                lines.append(f"❌ Error — {item.get('texto','')}")
                fail += 1
        resumen = f"📋 Agendé *{ok}* de *{len(items)}* recordatorios:\n\n" + "\n".join(lines)
        await update.message.reply_text(resumen, parse_mode="Markdown")
        return

    # ── Recordatorio único ──
    if parsed.get("es_recordatorio"):
        try:
            await agendar_recordatorio(update, parsed, chat_id)
        except Exception as e:
            logger.error(f"Error agendando recordatorio: {e}")
            await update.message.reply_text("❌ No pude agendar el recordatorio. Intentá de nuevo.")
    else:
        try:
            response = await _gemini_text(
                f"Sos un asistente personal amigable. Respondé en español rioplatense, breve y directo.\n\nUsuario: {text}",
                notify_cb=notify,
            )
            await update.message.reply_text(response)
        except Exception as e:
            logger.error(f"Error Gemini respuesta libre: {e}")
            await update.message.reply_text("⚠️ No puedo responder ahora (límite de API). Intentá en un momento.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    caption = update.message.caption or ""
    now     = datetime.now(TZ)

    _notified = False
    async def notify(secs):
        nonlocal _notified
        if not _notified:
            await update.message.reply_text("⏳ La API está ocupada, esperá un momento y lo proceso...")
            _notified = True

    photo       = update.message.photo[-1]
    file        = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()

    analysis = await analyze_image_with_gemini(bytes(image_bytes), "image/jpeg", caption, notify_cb=notify)

    if any(w in caption.lower() for w in ["recordame", "recordá", "recorda", "recordale"]):
        combined_text = f"{caption} — Imagen: {analysis}"
        parsed = await parse_reminder_with_gemini(combined_text, now, notify_cb=notify)
        if parsed and parsed.get("es_recordatorio"):
            try:
                await agendar_recordatorio(update, parsed, chat_id)
                return
            except Exception as e:
                logger.error(f"Error agendando recordatorio con imagen: {e}")

    await update.message.reply_text(f"🖼️ {analysis}")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    now     = datetime.now(TZ)

    await update.message.reply_text("🎙️ Escuchando...")

    _notified = False
    async def notify(secs):
        nonlocal _notified
        if not _notified:
            await update.message.reply_text("⏳ La API está ocupada, esperá un momento y lo proceso...")
            _notified = True

    voice = update.message.voice
    file  = await context.bot.get_file(voice.file_id)
    audio_bytes = await file.download_as_bytearray()

    result = await analyze_voice_with_gemini(bytes(audio_bytes), now, notify_cb=notify)

    if result is None:
        await update.message.reply_text("⚠️ No pude procesar el audio ahora. Intentá en un momento.")
        return

    transcripcion = result.get("transcripcion", "")

    if result.get("es_recordatorio"):
        if not result.get("texto"):
            result["texto"] = transcripcion[:200]
        try:
            await agendar_recordatorio(update, result, chat_id, transcripcion)
        except Exception as e:
            logger.error(f"Error agendando recordatorio de voz: {e}")
            await update.message.reply_text("❌ No pude agendar el recordatorio. Intentá de nuevo.")
    else:
        # Respuesta libre al audio
        try:
            respuesta = await _gemini_text(
                f"Sos un asistente personal amigable. El usuario dijo por voz: \"{transcripcion}\"\nRespondé en español rioplatense, breve y directo."
            )
            await update.message.reply_text(f"🎙️ _\"{transcripcion}\"_\n\n{respuesta}", parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(f"🎙️ Escuché: _{transcripcion}_", parse_mode="Markdown")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    doc = update.message.document
    if doc.mime_type and doc.mime_type.startswith("image/"):
        file        = await context.bot.get_file(doc.file_id)
        image_bytes = await file.download_as_bytearray()
        caption     = update.message.caption or ""
        analysis    = await analyze_image_with_gemini(bytes(image_bytes), doc.mime_type, caption)
        await update.message.reply_text(f"🖼️ {analysis}")
    else:
        await update.message.reply_text("Por ahora solo proceso imágenes. ¡Mandame una foto!")

# ─────────────────────────────────────────────
# COMANDOS
# ─────────────────────────────────────────────

async def agendar_recordatorio(update: Update, parsed: dict, sender_chat_id: int, transcripcion: str = "", silent: bool = False) -> bool:
    """
    Agenda un recordatorio según parsed. Maneja destinatario propio o de otra persona.
    silent=True: no envía mensaje de confirmación (para uso en lotes).
    Devuelve True si se agendó, False si falló.
    """
    now           = datetime.now(TZ)
    scheduled_at  = datetime.fromisoformat(parsed["datetime"]).astimezone(TZ)
    reminder_text = parsed["texto"]
    nombre_para   = parsed.get("para", "").strip()

    if scheduled_at <= now:
        prefix = f'🎙️ _"{transcripcion}"_\n\n' if transcripcion else ""
        await update.message.reply_text(
            prefix + "⚠️ Esa fecha/hora ya pasó. ¿Querés que lo programe para otro momento?",
            parse_mode="Markdown"
        )
        return False

    # Resolver destinatario
    dest_chat_id  = sender_chat_id
    dest_nombre   = None
    if nombre_para:
        usuario = get_usuario_por_nombre(nombre_para)
        if usuario:
            dest_chat_id = usuario["chat_id"]
            dest_nombre  = usuario["nombre"]
        else:
            await update.message.reply_text(
                f"⚠️ No encontré a *{nombre_para}* en mis contactos. "
                f"Pedile que le escriba /start al bot primero.",
                parse_mode="Markdown"
            )
            return False

    job_id = str(uuid.uuid4())
    mi_nombre = get_nombre_usuario(sender_chat_id)

    # Texto que llegará al destinatario
    if dest_chat_id != sender_chat_id:
        texto_recordatorio = f"{mi_nombre} te mandó este recordatorio:\n{reminder_text}"
    else:
        texto_recordatorio = reminder_text

    scheduler.add_job(
        send_reminder,
        trigger="date",
        run_date=scheduled_at,
        args=[dest_chat_id, texto_recordatorio, job_id],
        id=job_id,
    )
    save_reminder(dest_chat_id, texto_recordatorio, scheduled_at, job_id)

    fecha_str = scheduled_at.strftime("%A %d/%m/%Y a las %H:%M")
    prefix    = f'🎙️ _"{transcripcion}"_\n\n' if transcripcion else ""

    if not silent:
        if dest_chat_id != sender_chat_id:
            await update.message.reply_text(
                prefix + f"✅ Le agendé a *{dest_nombre}* para el *{fecha_str}*:\n_{reminder_text}_",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                prefix + f"✅ ¡Listo! Te recuerdo el *{fecha_str}*:\n_{reminder_text}_",
                parse_mode="Markdown"
            )
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id    = update.effective_chat.id
    first_name = update.effective_user.first_name or "Usuario"
    upsert_usuario(chat_id, first_name)
    await update.message.reply_text(
        f"👋 ¡Hola, {first_name}! Soy tu bot de recordatorios.\n\n"
        f"Podés decirme cosas como:\n"
        f"• _Recordame mañana a las 10hs llamar al médico_\n"
        f"• _Recordame el viernes a las 18hs comprar pan_\n"
        f"• _Recordame en 2 horas revisar el horno_\n"
        f"• _Recordale a Lore que llame al banco el lunes a las 11_\n\n"
        f"También podés mandarme fotos o audios.",
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

    # Configurar botón de Mini App si se definió la URL
    if MINI_APP_URL:
        try:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="📋 Recordatorios",
                    web_app=WebAppInfo(url=MINI_APP_URL),
                )
            )
            logger.info(f"Mini App button configurado: {MINI_APP_URL}")
        except Exception as e:
            logger.warning(f"No se pudo configurar el botón de Mini App: {e}")

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
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # run_polling() es síncrono en PTB v21 — maneja su propio event loop
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
