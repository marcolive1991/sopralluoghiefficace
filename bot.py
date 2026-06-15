"""
Bot Telegram per archiviazione foto sopralluoghi su OneDrive
Efficace Impianti Srl
"""

import os
import asyncio
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from onedrive_client import OneDriveClient

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurazione categorie / percorsi OneDrive
# ---------------------------------------------------------------------------
CATEGORIES: dict = {
    "preventivo": {
        "label": "📋 Preventivo Privato",
        "path": "Gare/005. PREVENTIVI LAVORI PRIVATI",
    },
    "gara": {
        "label": "🏆 Gara in Preparazione",
        "path": "Gare/004. GARE IN PREPARAZIONE",
    },
    "appalto": {
        "label": "🔧 Appalto in Corso",
        "path": "Appalti in Corso",
    },
}

# ---------------------------------------------------------------------------
# Utenti autorizzati (chat ID separati da virgola in ALLOWED_CHAT_IDS)
# Se non impostato, chiunque può usare il bot.
# ---------------------------------------------------------------------------
_raw = os.environ.get("ALLOWED_CHAT_IDS", "")
ALLOWED_IDS: set = {int(x.strip()) for x in _raw.split(",") if x.strip()}

# ---------------------------------------------------------------------------
# Stati conversazione (per chat_id)
# ---------------------------------------------------------------------------
STATE_IDLE = "idle"
STATE_WAITING_CATEGORY = "waiting_category"
STATE_WAITING_SUBFOLDER = "waiting_subfolder"
STATE_WAITING_CAPTION = "waiting_caption"

# ---------------------------------------------------------------------------
# Storage globale (sufficiente per uso mono/pochi utenti)
# ---------------------------------------------------------------------------
photo_buffers: dict = {}    # chat_id -> [file_id, ...]
media_timers: dict = {}     # chat_id -> asyncio.Task
user_states: dict = {}      # chat_id -> stato corrente
user_data_store: dict = {}  # chat_id -> dati sessione

onedrive = OneDriveClient()

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def get_state(chat_id: int) -> str:
    return user_states.get(chat_id, STATE_IDLE)


def set_state(chat_id: int, state: str) -> None:
    user_states[chat_id] = state


def get_udata(chat_id: int) -> dict:
    return user_data_store.setdefault(chat_id, {})


def clear_session(chat_id: int) -> None:
    photo_buffers.pop(chat_id, None)
    user_states.pop(chat_id, None)
    user_data_store.pop(chat_id, None)
    t = media_timers.pop(chat_id, None)
    if t:
        t.cancel()


async def check_auth(update: Update) -> bool:
    """Restituisce True se l'utente è autorizzato."""
    if ALLOWED_IDS and update.effective_user.id not in ALLOWED_IDS:
        await update.effective_message.reply_text("⛔ Non autorizzato.")
        return False
    return True


def sanitize(text: str) -> str:
    """Rende il testo sicuro per un nome file."""
    safe = "".join(c if c.isalnum() or c in " -_àèìòùáéíóú" else "" for c in text)
    return safe.strip().replace(" ", "_")


# ---------------------------------------------------------------------------
# Handlers comandi
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👷 *Bot Sopralluoghi – Efficace Impianti*\n\n"
        "Mandami le foto e le archivio automaticamente su OneDrive.\n\n"
        "Al primo utilizzo:\n"
        "• /auth — collega il tuo account Microsoft\n\n"
        "Durante l'uso:\n"
        "• Manda le foto (anche più in una volta)\n"
        "• /annulla — interrompi un'operazione in corso",
        parse_mode="Markdown",
    )


async def cmd_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_auth(update):
        return
    try:
        flow = onedrive.initiate_device_flow()
        await update.message.reply_text(
            f"🔑 *Autenticazione Microsoft*\n\n"
            f"1. Vai su: {flow['verification_uri']}\n"
            f"2. Inserisci il codice: `{flow['user_code']}`\n\n"
            "⏳ In attesa di conferma (max 15 minuti)...",
            parse_mode="Markdown",
        )
        asyncio.create_task(_poll_auth(update, flow))
    except Exception as exc:
        logger.exception("Errore avvio device flow")
        await update.message.reply_text(f"❌ Errore: {exc}")


async def _poll_auth(update: Update, flow: dict) -> None:
    try:
        success = await asyncio.to_thread(onedrive.acquire_token_by_device_flow, flow)
        if success:
            await update.message.reply_text(
                "✅ Account Microsoft collegato! Ora puoi mandarmi le foto."
            )
        else:
            await update.message.reply_text(
                "❌ Autenticazione fallita o scaduta. Riprova con /auth"
            )
    except Exception as exc:
        logger.exception("Errore durante poll auth")
        await update.message.reply_text(f"❌ Errore autenticazione: {exc}")


async def cmd_annulla(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_session(update.effective_chat.id)
    await update.message.reply_text("❌ Operazione annullata.")


# ---------------------------------------------------------------------------
# Ricezione foto
# ---------------------------------------------------------------------------

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_auth(update):
        return

    chat_id = update.effective_chat.id

    if get_state(chat_id) != STATE_IDLE:
        await update.message.reply_text(
            "⚠️ C'è già un'operazione in corso. Usa /annulla per cancellarla."
        )
        return

    # Aggiungi foto al buffer (massima qualità = ultimo elemento)
    photo_buffers.setdefault(chat_id, [])
    photo_buffers[chat_id].append(update.message.photo[-1].file_id)

    # Cancella timer precedente e avvia nuovo (aspetta eventuali foto album)
    old = media_timers.pop(chat_id, None)
    if old:
        old.cancel()

    media_timers[chat_id] = asyncio.create_task(
        _start_category_flow(update, chat_id, delay=1.5)
    )


async def _start_category_flow(update: Update, chat_id: int, delay: float) -> None:
    """Avvia il flusso dopo aver raccolto tutte le foto dell'album."""
    await asyncio.sleep(delay)

    n = len(photo_buffers.get(chat_id, []))
    if n == 0:
        return

    set_state(chat_id, STATE_WAITING_CATEGORY)

    keyboard = [
        [InlineKeyboardButton(cat["label"], callback_data=f"cat_{key}")]
        for key, cat in CATEGORIES.items()
    ]
    await update.message.reply_text(
        f"📸 {n} {'foto ricevuta' if n == 1 else 'foto ricevute'}.\n\nChe tipo di sopralluogo?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ---------------------------------------------------------------------------
# Callback tastiere inline
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    data = query.data
    state = get_state(chat_id)

    if data.startswith("cat_") and state == STATE_WAITING_CATEGORY:
        await _on_category_selected(query, chat_id)
    elif data.startswith("sf_") and state == STATE_WAITING_SUBFOLDER:
        await _on_subfolder_selected(query, chat_id)


async def _on_category_selected(query, chat_id: int) -> None:
    cat_key = query.data.replace("cat_", "")
    udata = get_udata(chat_id)
    udata["category_key"] = cat_key

    await query.edit_message_text("⏳ Carico le cartelle…")

    try:
        path = CATEGORIES[cat_key]["path"]
        subfolders = await asyncio.to_thread(onedrive.list_subfolders, path)
    except Exception as exc:
        logger.exception("Errore lista sottocartelle")
        await query.edit_message_text(f"❌ Errore nel caricare le cartelle:\n{exc}")
        clear_session(chat_id)
        return

    if not subfolders:
        await query.edit_message_text(
            f"⚠️ Nessuna cartella trovata in:\n`{CATEGORIES[cat_key]['path']}`\n\n"
            "Crea prima la cartella del progetto su OneDrive.",
            parse_mode="Markdown",
        )
        clear_session(chat_id)
        return

    udata["subfolders"] = {f["id"]: f["name"] for f in subfolders}
    set_state(chat_id, STATE_WAITING_SUBFOLDER)

    keyboard = [
        [InlineKeyboardButton(f["name"], callback_data=f"sf_{f['id']}")]
        for f in subfolders
    ]
    await query.edit_message_text(
        "📁 Seleziona il progetto:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _on_subfolder_selected(query, chat_id: int) -> None:
    folder_id = query.data.replace("sf_", "")
    udata = get_udata(chat_id)
    folder_name = udata["subfolders"].get(folder_id, folder_id)

    udata["target_folder_id"] = folder_id
    udata["target_folder_name"] = folder_name
    set_state(chat_id, STATE_WAITING_CAPTION)

    await query.edit_message_text(
        f"📁 *{folder_name}*\n\n"
        "✏️ Scrivi una didascalia per le foto\n"
        "_Es: quadro elettrico, locale contatori, ingresso principale_",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Ricezione didascalia e upload
# ---------------------------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    if get_state(chat_id) != STATE_WAITING_CAPTION:
        return  # testo ignorato negli altri stati

    caption = update.message.text.strip()
    safe_caption = sanitize(caption)
    if not safe_caption:
        await update.message.reply_text("⚠️ Didascalia non valida. Riprova con un testo diverso.")
        return

    date_str = datetime.now().strftime("%Y-%m-%d")
    udata = get_udata(chat_id)
    folder_id = udata["target_folder_id"]
    folder_name = udata["target_folder_name"]
    file_ids = photo_buffers.get(chat_id, [])

    if not file_ids:
        await update.message.reply_text("❌ Nessuna foto da caricare. Riprova mandando le foto.")
        clear_session(chat_id)
        return

    total = len(file_ids)
    await update.message.reply_text(f"⏳ Carico {total} foto su OneDrive…")

    # Recupera o crea la cartella FOTO dentro la sottocartella scelta
    try:
        foto_folder_id = await asyncio.to_thread(
            onedrive.get_or_create_foto_folder, folder_id
        )
    except Exception as exc:
        logger.exception("Errore creazione cartella FOTO")
        await update.message.reply_text(f"❌ Errore creazione cartella FOTO:\n{exc}")
        clear_session(chat_id)
        return

    # Upload
    uploaded = 0
    errors = 0
    for i, file_id in enumerate(file_ids, 1):
        try:
            tg_file = await context.bot.get_file(file_id)
            file_bytes = bytes(await tg_file.download_as_bytearray())

            filename = (
                f"{date_str}_{safe_caption}.jpg"
                if total == 1
                else f"{date_str}_{safe_caption}_{i:02d}.jpg"
            )
            await asyncio.to_thread(
                onedrive.upload_file, foto_folder_id, filename, file_bytes
            )
            uploaded += 1
        except Exception as exc:
            logger.error(f"Errore upload foto {i}/{total}: {exc}")
            errors += 1

    clear_session(chat_id)

    icon = "✅" if errors == 0 else "⚠️"
    suffix = f"_{total:02d}" if total > 1 else ""
    msg = (
        f"{icon} *{uploaded}/{total} foto caricate*\n\n"
        f"📁 `{folder_name}/FOTO/`\n"
        f"🏷️ `{date_str}_{safe_caption}{suffix}.jpg`"
    )
    if errors:
        msg += f"\n\n❌ {errors} foto non caricate per errore."

    await update.message.reply_text(msg, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("auth", cmd_auth))
    app.add_handler(CommandHandler("annulla", cmd_annulla))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot avviato")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
