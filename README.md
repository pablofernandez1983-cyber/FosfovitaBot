# Bot de Recordatorios — Telegram + Gemini + Supabase

## Setup paso a paso

### 1. Crear el bot en Telegram
1. Abrí Telegram y buscá `@BotFather`
2. Mandá `/newbot`
3. Elegí un nombre (ej: "Recordatorios Pablo") y un username (ej: `pablorecordatorios_bot`)
4. BotFather te da el **TELEGRAM_TOKEN** → guardalo

### 2. Obtener tu Chat ID
1. Buscá `@userinfobot` en Telegram
2. Mandá cualquier mensaje → te devuelve tu **ID numérico**
3. Ese es tu **ALLOWED_USER_ID** (así solo vos podés usar el bot)

### 3. Crear tabla en Supabase
1. Entrá a tu proyecto en supabase.com
2. Abrí el **SQL Editor**
3. Pegá y ejecutá el contenido de `supabase_setup.sql`

### 4. Variables de entorno en Railway
Agregar en el servicio de Railway:

```
TELEGRAM_TOKEN=tu_token_de_botfather
GEMINI_API_KEY=tu_api_key_de_gemini
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_KEY=tu_service_role_key  (o anon key)
ALLOWED_USER_ID=tu_chat_id_numerico
```

### 5. Deploy en Railway
```bash
git init
git add .
git commit -m "bot recordatorios"
# Conectar repo a Railway o usar Railway CLI:
railway up
```

---

## Uso del bot

| Mensaje | Resultado |
|---|---|
| `Recordame mañana a las 10hs llamar al médico` | Agenda recordatorio |
| `Recordame el viernes a las 18hs comprar vino` | Agenda para el viernes |
| `Recordame en 2 horas revisar el horno` | Agenda en 2 horas |
| `/lista` | Ver recordatorios pendientes |
| `/borrar <id>` | Borrar un recordatorio |
| Foto con caption `Recordame el lunes esto` | Analiza imagen y agenda |
| Foto sin caption | Describe la imagen |

---

## Notas
- Si Railway reinicia el servicio, los recordatorios pendientes se recargan automáticamente desde Supabase
- Los recordatorios que vencieron mientras el bot estaba caído se envían al reiniciar con una nota de retraso
- Gemini entiende lenguaje natural: "a la tardecita", "la semana que viene", "en un rato", etc.
