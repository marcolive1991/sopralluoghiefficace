# Setup – Bot Sopralluoghi Efficace Impianti

## 1. Crea il bot Telegram

1. Apri Telegram e cerca **@BotFather**
2. Invia `/newbot`, scegli un nome (es. "Sopralluoghi Efficace") e uno username (es. `efficace_sopralluoghi_bot`)
3. Copia il token che ti dà BotFather → sarà `TELEGRAM_BOT_TOKEN`

## 2. Registra l'app su Azure AD

1. Vai su https://portal.azure.com → **Azure Active Directory** → **Registrazioni app** → **Nuova registrazione**
2. Nome: `Bot Sopralluoghi`, tipo account: *Account dell'organizzazione corrente*
3. URI di reindirizzamento: lascia vuoto
4. Copia il **Client ID** e il **Tenant ID** dalla panoramica dell'app
5. Vai su **Autorizzazioni API** → **Aggiungi autorizzazione** → **Microsoft Graph** → **Autorizzazioni delegate**
   - Aggiungi: `Files.ReadWrite`, `offline_access`
6. Clicca **Concedi consenso amministratore**

Non serve un segreto client (si usa il device code flow, autenticazione pubblica).

## 3. Trova il tuo Chat ID Telegram

Scrivi al bot `@userinfobot` su Telegram: ti risponde con il tuo Chat ID.

## 4. Deploy su Render

1. Carica i file su un repo GitHub privato
2. Crea un **Web Service** su Render (o Worker)
3. Imposta le variabili d'ambiente nella sezione *Environment*:
   - `TELEGRAM_BOT_TOKEN`
   - `MS_CLIENT_ID`
   - `MS_TENANT_ID`
   - `ALLOWED_CHAT_IDS`
4. Build command: `pip install -r requirements.txt`
5. Start command: `python bot.py`

> **Importante – token persistence su Render:**
> Il file `token_cache.json` viene perso ad ogni restart del servizio.
> Soluzione: aggiungi un **Persistent Disk** su Render (dal pannello del servizio,
> tab *Disks*, monta su `/opt/render/project/src`). Costo: ~$1/mese.
> In alternativa, ri-esegui `/auth` dopo ogni restart.

## 5. Prima autenticazione

1. Avvia il bot su Telegram
2. Scrivi `/auth`
3. Vai al link indicato, inserisci il codice
4. Confirma con il tuo account Microsoft aziendale
5. Il bot confermerà: *"Account collegato"*

## Utilizzo quotidiano

1. Dal telefono: apri Telegram → bot
2. Manda le foto (anche un album intero)
3. Scegli la categoria (Preventivo / Gara / Appalto)
4. Seleziona il progetto dal menù
5. Scrivi la didascalia (es. `quadro elettrico`)
6. Le foto vengono caricate in `[Progetto]/FOTO/YYYY-MM-DD_didascalia_01.jpg`
