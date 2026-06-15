"""
Bot Telegram per archiviazione foto sopralluoghi - Efficace Impianti Srl
"""

import os
import asyncio
import logging
from datetime import datetime

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

photo_buffers   = {}
media_timers    = {}
user_states     = {}
user_data_store = {}

onedrive = OneDriveClient()


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


async def cmd_start(update, context):
    await update.message.reply_text(
        "Bot Sopralluoghi - Efficace Impianti\n\n"
        "Mandami le foto e le archivio su SharePoint.\n\n"
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
            await update.message.reply_text("Account Microsoft collegato! Ora puoi mandarmi le foto.")
        else:
            await update.message.reply_text("Autenticazione fallita o scaduta. Riprova con /auth")
    except Exception as exc:
        logger.exception("Errore poll auth")
        await update.message.reply_text("Errore autenticazione: " + str(exc))

async def cmd_annulla(update, context):
    clear_session(update.effective_chat.id)
    await update.message.reply_text("Operazione annullata.")


async def handle_photo(update, context):
    if not await check_auth(update):
        return
    chat_id = update.effective_chat.id
    if get_state(chat_id) != STATE_IDLE:
        await update.message.reply_text("C'e' gia' un'operazione in corso. Usa /annulla per cancellarla.")
        return
    photo_buffers.setdefault(chat_id, [])
    photo_buffers[chat_id].append(update.message.photo[-1].file_id)
    old = media_timers.pop(chat_id, None)
    if old:
        old.cancel()
    media_timers[chat_id] = asyncio.create_task(_start_category_flow(update, chat_id, delay=1.5))

async def _start_category_flow(update, chat_id, delay):
    await asyncio.sleep(delay)
    n = len(photo_buffers.get(chat_id, []))
    if n == 0:
        return
    set_state(chat_id, STATE_WAITING_CATEGORY)
    keyboard = [
        [InlineKeyboardButton("Preventivo Privato",  callback_data="cat_preventivo")],
        [InlineKeyboardButton("Gara in Preparazione", callback_data="cat_gara")],
        [InlineKeyboardButton("Appalto in Corso",    callback_data="cat_appalto")],
    ]
    await update.message.reply_text(
        str(n) + " foto ricevute.\n\nChe tipo di sopralluogo?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


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
        "Scrivi una didascalia per le foto\n"
        "Es: quadro elettrico, locale contatori, ingresso principale"
    )


async def handle_text(update, context):
    chat_id = update.effective_chat.id
    if get_state(chat_id) != STATE_WAITING_CAPTION:
        return
    caption      = update.message.text.strip()
    safe_caption = sanitize(caption)
    if not safe_caption:
        await update.message.reply_text("Didascalia non valida. Riprova.")
        return
    date_str    = datetime.now().strftime("%Y-%m-%d")
    udata       = get_udata(chat_id)
    folder_id   = udata["target_folder_id"]
    folder_name = udata["target_folder_name"]
    drive_id    = udata["target_drive_id"]
    file_ids    = photo_buffers.get(chat_id, [])
    if not file_ids:
        await update.message.reply_text("Nessuna foto da caricare. Riprova.")
        clear_session(chat_id)
        return
    total = len(file_ids)
    await update.message.reply_text("Carico " + str(total) + " foto su SharePoint...")
    try:
        foto_folder_id = await asyncio.to_thread(onedrive.get_or_create_foto_folder, drive_id, folder_id)
    except Exception as exc:
        logger.exception("Errore creazione cartella FOTO")
        await update.message.reply_text("Errore creazione cartella FOTO:\n" + str(exc))
        clear_session(chat_id)
        return
    uploaded = 0
    errors   = 0
    for i, file_id in enumerate(file_ids, 1):
        try:
            tg_file    = await context.bot.get_file(file_id)
            file_bytes = bytes(await tg_file.download_as_bytearray())
            filename   = (
                date_str + "_" + safe_caption + ".jpg"
                if total == 1
                else date_str + "_" + safe_caption + "_" + str(i).zfill(2) + ".jpg"
            )
            await asyncio.to_thread(onedrive.upload_file, drive_id, foto_folder_id, filename, file_bytes)
            uploaded += 1
        except Exception as exc:
            logger.error("Errore upload foto " + str(i) + "/" + str(total) + ": " + str(exc))
            errors += 1
    clear_session(chat_id)
    icon = "OK" if errors == 0 else "ATTENZIONE"
    msg = (
        icon + " " + str(uploaded) + "/" + str(total) + " foto caricate\n\n"
        "Cartella: " + folder_name + "/FOTO/\n"
        "Nome: " + date_str + "_" + safe_caption + "_XX.jpg"
    )
    if errors:
        msg += "\n\n" + str(errors) + " foto non caricate per errore."
    await update.message.reply_text(msg)


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("auth",    cmd_auth))
    app.add_handler(CommandHandler("annulla", cmd_annulla))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot avviato")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
