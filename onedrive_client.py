"""
Client SharePoint via Microsoft Graph API - Efficace Impianti Srl
"""

import os
import logging
import requests
import msal
from urllib.parse import quote

logger = logging.getLogger(__name__)

GRAPH_BASE       = "https://graph.microsoft.com/v1.0"
TOKEN_CACHE_FILE = "token_cache.json"
SCOPES           = ["Files.ReadWrite.All"]


class OneDriveClient:
    def __init__(self):
        self.client_id = os.environ["MS_CLIENT_ID"]
        self.tenant_id = os.environ.get("MS_TENANT_ID", "common")
        self._cache = msal.SerializableTokenCache()
        if os.path.exists(TOKEN_CACHE_FILE):
            try:
                with open(TOKEN_CACHE_FILE) as f:
                    self._cache.deserialize(f.read())
                logger.info("Token cache caricata")
            except Exception:
                logger.warning("Impossibile caricare token cache")
        self._app = msal.PublicClientApplication(
            self.client_id,
            authority="https://login.microsoftonline.com/" + self.tenant_id,
            token_cache=self._cache,
        )

    def _save_cache(self):
        if self._cache.has_state_changed:
            with open(TOKEN_CACHE_FILE, "w") as f:
                f.write(self._cache.serialize())

    def _get_token(self):
        accounts = self._app.get_accounts()
        if accounts:
            result = self._app.acquire_token_silent(SCOPES, account=accounts[0])
            self._save_cache()
            if result and "access_token" in result:
                return result["access_token"]
        raise RuntimeError("Non autenticato. Usa /auth nel bot.")

    def initiate_device_flow(self):
        flow = self._app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError("Errore device flow: " + str(flow.get("error_description", flow)))
        return flow

    def acquire_token_by_device_flow(self, flow):
        result = self._app.acquire_token_by_device_flow(flow)
        if "access_token" in result:
            self._save_cache()
            logger.info("Autenticazione completata")
            return True
        logger.error("Autenticazione fallita: " + str(result.get("error_description", result)))
        return False

    def _headers(self):
        return {"Authorization": "Bearer " + self._get_token(), "Accept": "application/json"}

    def _get(self, url, params=None):
        if params:
            qs = "&".join(k + "=" + str(v) for k, v in params.items())
            url = url + "?" + qs
        r = requests.get(url, headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, url, payload):
        r = requests.post(
            url,
            headers={**self._headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def list_subfolders(self, drive_id, subfolder=None):
        if subfolder:
            encoded = quote(subfolder, safe="/")
            url = GRAPH_BASE + "/drives/" + drive_id + "/root:/" + encoded + ":/children"
        else:
            url = GRAPH_BASE + "/drives/" + drive_id + "/root/children"
        data = self._get(url, {"$top": 200})
        folders = [
            {"id": item["id"], "name": item["name"]}
            for item in data.get("value", [])
            if "folder" in item and item["name"] != "FOTO"
        ]
        folders.sort(key=lambda x: x["name"].lower())
        return folders

    def get_or_create_foto_folder(self, drive_id, parent_folder_id):
        url = GRAPH_BASE + "/drives/" + drive_id + "/items/" + parent_folder_id + "/children"
        payload = {"name": "FOTO", "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
        try:
            result = self._post(url, payload)
            logger.info("Cartella FOTO creata")
            return result["id"]
        except requests.HTTPError as e:
            if e.response.status_code == 409:
                data = self._get(url, {"$top": 200})
                for item in data.get("value", []):
                    if item.get("name") == "FOTO" and "folder" in item:
                        return item["id"]
                raise RuntimeError("Cartella FOTO non trovata.")
            raise

    def upload_file(self, drive_id, folder_id, filename, content, content_type="image/jpeg"):
        url = (GRAPH_BASE + "/drives/" + drive_id
               + "/items/" + folder_id + ":/" + quote(filename) + ":/content")
        headers = {**self._headers(), "Content-Type": content_type}
        r = requests.put(url, headers=headers, data=content, timeout=120)
        r.raise_for_status()
        result = r.json()
        logger.info("Caricato: " + result.get("name", filename))
        return result
