"""
Bot Telegram per archiviazione foto e video sopralluoghi - Efficace Impianti Srl
"""

import os
import io
import asyncio
import logging
from datetime import datetime

from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
from onedrive_client import OneDriveClient

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DRIVE_GARE    = "b!1pSdbxvScUKQu-3NepOh_krK3H-eReFDj-YualkN1p4c4BLfWmEwSKzK9MSg7lnS"
DRIVE_APPALTI = "b!1pSdbxvScUKQu-3NepOh_krK3H-eReFDj-YualkN1p67T48VA0_YRLZ2nDJQpml_"

CATEGORIES = {
    "preventivo": {
        "label": "Preventivo Privato",
        "drive_id": DRIVE_GARE,
        "subfolder": "005. PREVENTIVI LAVORI PRIVATI",
    },
    "gara": {
        "label": "Gara in Preparazione",
        "drive_id": DRIVE_GARE,
        "subfolder": "004. GARE IN PREPARAZIONE",
    },
    "appalto": {
        "label": "Appalto in Corso",
        "drive_id": DRIVE_APPALTI,
        "subfolder": None,
    },
}

_raw = os.environ.get("ALLOWED_CHAT_IDS", "")
ALLOWED_IDS = {int(x.strip()) for x in _raw.split(",") if x.strip()}

STATE_IDLE              = "idle"
STATE_WAITING_CATEGORY  = "waiting_category"
STATE_WAITING_SUBFOLDER = "waiting_subfolder"
STATE_WAITING_CAPTION   = "waiting_caption"

# Buffer: lista di dict {"file_id": str, "is_doc": bool, "mime": str}
photo_buffers   = {}
media_timers    = {}
user_states     = {}
user_data_store = {}

onedrive = OneDriveClient()

# MIME type → estensione file
MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png":  ".png",
    "image/heic": ".heic",
    "image/heif": ".heic",
    "video/mp4":        ".mp4",
    "video/quicktime":  ".mov",
    "video/x-msvideo":  ".avi",
    "video/3gpp":       ".3gp",
}


def get_state(chat_id):
    return user_states.get(chat_id, STATE_IDLE)

def set_state(chat_id, state):
    user_states[chat_id] = state

def get_udata(chat_id):
    return user_data_store.setdefault(chat_id, {})

def clear_session(chat_id):
    photo_buffers.pop(chat_id, None)
    user_states.pop(chat_id, None)
    user_data_store.pop(chat_id, None)
    t = media_timers.pop(chat_id, None)
    if t:
        t.cancel()

async def check_auth(update):
    if ALLOWED_IDS and update.effective_user.id not in ALLOWED_IDS:
        await update.effective_message.reply_text("Non autorizzato.")
        return False
    return True

def sanitize(text):
    safe = "".join(c if c.isalnum() or c in " -_" else "" for c in text)
    return safe.strip().replace(" ", "_")

def exif_date(image_bytes):
    """Estrae la data di scatto dai dati EXIF (solo per immagini JPEG originali)."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        exif = img.getexif()
        if not exif:
            exif = img._getexif() or {}
        logger.info("EXIF tags trovati: " + str(list(exif.keys()) if exif else "nessuno"))
        for tag_id in (36867, 36868, 306):
            value = exif.get(tag_id)
            if value:
                logger.info("Data EXIF (tag " + str(tag_id) + "): " + str(value))
                dt = datetime.strptime(str(value).strip(), "%Y:%m:%d %H:%M:%S")
                return dt.strftime("%Y-%m-%d")
        logger.info("Nessuna data EXIF trovata nei tag standard")
    except Exception as exc:
        logger.warning("Errore lettura EXIF: " + str(exc))
    return None


async def cmd_start(update, context):
    await update.message.reply_text(
        "Bot Sopralluoghi - Efficace Impianti\n\n"
        "Invia foto e video del sopralluogo e li archivio su SharePoint.\n\n"
        "Modalita di invio:\n"
        "- Come FOTO/VIDEO: Telegram li comprime, usa la data di oggi\n"
        "- Come FILE: invia l'originale; per le foto legge la data EXIF\n\n"
        "Prima di iniziare: /auth per collegare il tuo account Microsoft.\n"
        "Per interrompere: /annulla"
    )

async def cmd_auth(update, context):
    if not await check_auth(update):
        return
    try:
        flow = onedrive.initiate_device_flow()
        await update.message.reply_text(
            "Autenticazione Microsoft\n\n"
            "1. Vai su: " + flow["verification_uri"] + "\n"
            "2. Inserisci il codice: " + flow["user_code"] + "\n\n"
            "In attesa di conferma (max 15 minuti)..."
        )
        asyncio.create_task(_poll_auth(update, flow))
    except Exception as exc:
        logger.exception("Errore avvio device flow")
        await update.message.reply_text("Errore: " + str(exc))

async def _poll_auth(update, flow):
    try:
        success = await asyncio.to_thread(onedrive.acquire_token_by_device_flow, flow)
        if success:
            await update.message.reply_text("Account Microsoft collegato! Ora puoi mandarmi foto e video.")
        else:
            await update.message.reply_text("Autenticazione fallita o scaduta. Riprova con /auth")
    except Exception as exc:
        logger.exception("Errore poll auth")
        await update.message.reply_text("Errore autenticazione: " + str(exc))

async def cmd_annulla(update, context):
    clear_session(update.effective_chat.id)
    await update.message.reply_text("Operazione annullata.")


# ---------------------------------------------------------------------------
# Ricezione file
# ---------------------------------------------------------------------------

async def _add_to_buffer(chat_id, file_id, is_doc, mime, update):
    photo_buffers.setdefault(chat_id, [])
    photo_buffers[chat_id].append({"file_id": file_id, "is_doc": is_doc, "mime": mime})
    old = media_timers.pop(chat_id, None)
    if old:
        old.cancel()
    media_timers[chat_id] = asyncio.create_task(_start_category_flow(update, chat_id, delay=1.5))

async def handle_photo(update, context):
    """Foto inviata come immagine compressa."""
    if not await check_auth(update):
        return
    chat_id = update.effective_chat.id
    if get_state(chat_id) != STATE_IDLE:
        await update.message.reply_text("C'e' gia' un'operazione in corso. Usa /annulla.")
        return
    await _add_to_buffer(chat_id, update.message.photo[-1].file_id, is_doc=False, mime="image/jpeg", update=update)

async def handle_video(update, context):
    """Video inviato come video compresso."""
    if not await check_auth(update):
        return
    chat_id = update.effective_chat.id
    if get_state(chat_id) != STATE_IDLE:
        await update.message.reply_text("C'e' gia' un'operazione in corso. Usa /annulla.")
        return
    await _add_to_buffer(chat_id, update.message.video.file_id, is_doc=False, mime="video/mp4", update=update)

async def handle_document(update, context):
    """Foto o video inviati come file originale."""
    if not await check_auth(update):
        return
    chat_id = update.effective_chat.id
    doc = update.message.document
    mime = doc.mime_type or ""
    if not (mime.startswith("image/") or mime.startswith("video/")):
        return
    if get_state(chat_id) != STATE_IDLE:
        await update.message.reply_text("C'e' gia' un'operazione in corso. Usa /annulla.")
        return
    logger.info("Documento ricevuto: " + mime + " (" + str(doc.file_size) + " bytes)")
    await _add_to_buffer(chat_id, doc.file_id, is_doc=True, mime=mime, update=update)

async def _start_category_flow(update, chat_id, delay):
    await asyncio.sleep(delay)
    items = photo_buffers.get(chat_id, [])
    if not items:
        return
    n = len(items)
    set_state(chat_id, STATE_WAITING_CATEGORY)
    keyboard = [
        [InlineKeyboardButton("Preventivo Privato",   callback_data="cat_preventivo")],
        [InlineKeyboardButton("Gara in Preparazione", callback_data="cat_gara")],
        [InlineKeyboardButton("Appalto in Corso",     callback_data="cat_appalto")],
    ]
    await update.message.reply_text(
        str(n) + " file ricevuti.\n\nChe tipo di sopralluogo?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ---------------------------------------------------------------------------
# Callback tastiere inline
# ---------------------------------------------------------------------------

async def handle_callback(update, context):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data    = query.data
    state   = get_state(chat_id)
    if data.startswith("cat_") and state == STATE_WAITING_CATEGORY:
        await _on_category_selected(query, chat_id)
    elif data.startswith("sf_") and state == STATE_WAITING_SUBFOLDER:
        await _on_subfolder_selected(query, chat_id)

async def _on_category_selected(query, chat_id):
    cat_key = query.data.replace("cat_", "")
    get_udata(chat_id)["category_key"] = cat_key
    await query.edit_message_text("Carico le cartelle...")
    try:
        cat = CATEGORIES[cat_key]
        subfolders = await asyncio.to_thread(onedrive.list_subfolders, cat["drive_id"], cat["subfolder"])
    except Exception as exc:
        logger.exception("Errore lista sottocartelle")
        await query.edit_message_text("Errore nel caricare le cartelle:\n" + str(exc))
        clear_session(chat_id)
        return
    if not subfolders:
        await query.edit_message_text(
            "Nessuna cartella trovata per " + CATEGORIES[cat_key]["label"] + ".\n"
            "Crea prima la cartella del progetto su SharePoint."
        )
        clear_session(chat_id)
        return
    get_udata(chat_id)["subfolders"] = {f["id"]: f["name"] for f in subfolders}
    set_state(chat_id, STATE_WAITING_SUBFOLDER)
    keyboard = [[InlineKeyboardButton(f["name"], callback_data="sf_" + f["id"])] for f in subfolders]
    await query.edit_message_text("Seleziona il progetto:", reply_markup=InlineKeyboardMarkup(keyboard))

async def _on_subfolder_selected(query, chat_id):
    folder_id = query.data.replace("sf_", "")
    udata = get_udata(chat_id)
    folder_name = udata["subfolders"].get(folder_id, folder_id)
    cat_key     = udata["category_key"]
    udata["target_folder_id"]   = folder_id
    udata["target_folder_name"] = folder_name
    udata["target_drive_id"]    = CATEGORIES[cat_key]["drive_id"]
    set_state(chat_id, STATE_WAITING_CAPTION)
    await query.edit_message_text(
        folder_name + "\n\n"
        "Scrivi una didascalia per i file\n"
        "Es: quadro elettrico, locale contatori, ingresso principale"
    )


# ---------------------------------------------------------------------------
# Testo (didascalia) e upload
# ---------------------------------------------------------------------------

async def handle_text(update, context):
    chat_id = update.effective_chat.id
    if get_state(chat_id) != STATE_WAITING_CAPTION:
        return
    caption      = update.message.text.strip()
    safe_caption = sanitize(caption)
    if not safe_caption:
        await update.message.reply_text("Didascalia non valida. Riprova.")
        return

    today_str   = datetime.now().strftime("%Y-%m-%d")
    udata       = get_udata(chat_id)
    folder_id   = udata["target_folder_id"]
    folder_name = udata["target_folder_name"]
    drive_id    = udata["target_drive_id"]
    items       = photo_buffers.get(chat_id, [])

    if not items:
        await update.message.reply_text("Nessun file da caricare. Riprova.")
        clear_session(chat_id)
        return

    total = len(items)
    await update.message.reply_text("Carico " + str(total) + " file su SharePoint...")

    try:
        foto_folder_id = await asyncio.to_thread(onedrive.get_or_create_foto_folder, drive_id, folder_id)
    except Exception as exc:
        logger.exception("Errore creazione cartella FOTO")
        await update.message.reply_text("Errore creazione cartella FOTO:\n" + str(exc))
        clear_session(chat_id)
        return

    uploaded = 0
    errors   = 0
    for i, item in enumerate(items, 1):
        try:
            tg_file    = await context.bot.get_file(item["file_id"])
            file_bytes = bytes(await tg_file.download_as_bytearray())
            mime       = item["mime"]
            ext        = MIME_EXT.get(mime, ".jpg" if mime.startswith("image/") else ".mp4")

            # Data EXIF solo per immagini originali
            date_str = today_str
            if item["is_doc"] and mime.startswith("image/"):
                date_str = exif_date(file_bytes) or today_str

            filename = (
                date_str + "_" + safe_caption + ext
                if total == 1
                else date_str + "_" + safe_caption + "_" + str(i).zfill(2) + ext
            )
            await asyncio.to_thread(onedrive.upload_file, drive_id, foto_folder_id, filename, file_bytes, mime)
            uploaded += 1
        except Exception as exc:
            logger.error("Errore upload file " + str(i) + "/" + str(total) + ": " + str(exc))
            errors += 1

    clear_session(chat_id)

    icon = "OK" if errors == 0 else "ATTENZIONE"
    msg = (
        icon + " " + str(uploaded) + "/" + str(total) + " file caricati\n\n"
        "Cartella: " + folder_name + "/FOTO/\n"
        "Nome: " + today_str + "_" + safe_caption + "_XX"
    )
    if errors:
        msg += "\n\n" + str(errors) + " file non caricati per errore."
    await update.message.reply_text(msg)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("auth",    cmd_auth))
    app.add_handler(CommandHandler("annulla", cmd_annulla))
    app.add_handler(MessageHandler(filters.PHOTO,          handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO,          handle_video))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    app.add_handler(MessageHandler(filters.Document.VIDEO, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot avviato")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
