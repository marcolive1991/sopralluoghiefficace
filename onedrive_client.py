"""
Client OneDrive for Business via Microsoft Graph API
Autenticazione con MSAL (device code flow)
"""

import os
import json
import logging
import requests
import msal
from urllib.parse import quote

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.microsoft.com/v1.0"
TOKEN_CACHE_FILE = "token_cache.json"

# Scopes necessari per leggere/scrivere file su OneDrive for Business
SCOPES = ["Files.ReadWrite"]


class OneDriveClient:
    def __init__(self):
        self.client_id = os.environ["MS_CLIENT_ID"]
        self.tenant_id = os.environ.get("MS_TENANT_ID", "common")

        # Cache token (persiste su file tra i riavvii)
        self._cache = msal.SerializableTokenCache()
        if os.path.exists(TOKEN_CACHE_FILE):
            try:
                with open(TOKEN_CACHE_FILE) as f:
                    self._cache.deserialize(f.read())
                logger.info("Token cache caricata da file")
            except Exception:
                logger.warning("Impossibile caricare token cache, si ripartirà da zero")

        self._app = msal.PublicClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            token_cache=self._cache,
        )

    # -----------------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------------

    def _save_cache(self) -> None:
        if self._cache.has_state_changed:
            with open(TOKEN_CACHE_FILE, "w") as f:
                f.write(self._cache.serialize())

    def _get_token(self) -> str:
        """Ottiene un access token valido (silenzioso se possibile)."""
        accounts = self._app.get_accounts()
        if accounts:
            result = self._app.acquire_token_silent(SCOPES, account=accounts[0])
            self._save_cache()
            if result and "access_token" in result:
                return result["access_token"]

        raise RuntimeError(
            "Non autenticato. Usa /auth nel bot per collegare l'account Microsoft."
        )

    def initiate_device_flow(self) -> dict:
        """Avvia il device code flow e restituisce i dati per l'utente."""
        flow = self._app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Errore device flow: {flow.get('error_description', flow)}")
        return flow

    def acquire_token_by_device_flow(self, flow: dict) -> bool:
        """Blocca fino al completamento del device flow. Restituisce True se ok."""
        result = self._app.acquire_token_by_device_flow(flow)
        if "access_token" in result:
            self._save_cache()
            logger.info("Autenticazione Microsoft completata")
            return True
        logger.error(f"Autenticazione fallita: {result.get('error_description', result)}")
        return False

    # -----------------------------------------------------------------------
    # Helper HTTP
    # -----------------------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Accept": "application/json",
        }

    def _get(self, url: str, params: dict = None) -> dict:
        # Passa i parametri OData direttamente nell'URL per evitare
        # che requests codifichi il '$' come '%24' (non accettato da Graph API)
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{qs}"
        r = requests.get(url, headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, url: str, payload: dict) -> dict:
        r = requests.post(
            url,
            headers={**self._headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    # -----------------------------------------------------------------------
    # Operazioni OneDrive
    # -----------------------------------------------------------------------

    def list_subfolders(self, path: str) -> list:
        """
        Elenca le sottocartelle dirette nel percorso OneDrive indicato.
        Restituisce lista di dict {"id": ..., "name": ...}.
        """
        encoded = quote(path, safe="/")
        url = f"{GRAPH_API}/me/drive/root:/{encoded}:/children"
        params = {
            "$select": "id,name,folder",
            "$top": 200,
        }
        data = self._get(url, params=params)
        items = data.get("value", [])

        # Filtra solo cartelle, escludi cartella FOTO (non è un progetto)
        folders = [
            {"id": item["id"], "name": item["name"]}
            for item in items
            if "folder" in item and item["name"] != "FOTO"
        ]

        # Ordine alfabetico
        folders.sort(key=lambda x: x["name"].lower())
        return folders

    def get_or_create_foto_folder(self, parent_folder_id: str) -> str:
        """
        Recupera l'ID della cartella FOTO dentro parent_folder_id.
        La crea se non esiste. Restituisce l'ID.
        """
        # Prova a crearla con conflictBehavior=fail
        url = f"{GRAPH_API}/me/drive/items/{parent_folder_id}/children"
        payload = {
            "name": "FOTO",
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        }
        try:
            result = self._post(url, payload)
            logger.info("Cartella FOTO creata")
            return result["id"]
        except requests.HTTPError as e:
            if e.response.status_code == 409:
                # Esiste già: recupera l'ID
                return self._get_existing_foto_folder(parent_folder_id)
            raise

    def _get_existing_foto_folder(self, parent_folder_id: str) -> str:
        """Recupera l'ID della cartella FOTO esistente."""
        url = f"{GRAPH_API}/me/drive/items/{parent_folder_id}/children"
        params = {"$select": "id,name,folder", "$top": 200}
        data = self._get(url, params=params)
        for item in data.get("value", []):
            if item.get("name") == "FOTO" and "folder" in item:
                return item["id"]
        raise RuntimeError("Cartella FOTO non trovata e non creabile.")

    def upload_file(self, folder_id: str, filename: str, content: bytes) -> dict:
        """
        Carica un file nella cartella indicata.
        Usa upload semplice (max ~4 MB per foto telefono).
        Per file più grandi usare upload session.
        """
        url = f"{GRAPH_API}/me/drive/items/{folder_id}:/{quote(filename)}:/content"
        headers = {
            **self._headers(),
            "Content-Type": "image/jpeg",
        }
        r = requests.put(url, headers=headers, data=content, timeout=60)
        r.raise_for_status()
        result = r.json()
        logger.info(f"Caricato: {result.get('name')} ({len(content)} bytes)")
        return result
