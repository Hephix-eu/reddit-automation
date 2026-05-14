"""Multilogin X API client — reusable across agents.

All endpoints + gotchas from references/api.md, encapsulated as methods.
Verified May 2026 against MLX X v12.2.0 (EU env), Mimic core 147.

Usage:
    from mlx_client import Client
    c = Client(email, password)
    c.signin()
    pid, port = c.start_by_user("HovercraftWeary8654")
    # ...drive via Playwright at http://127.0.0.1:{port}...
    c.save_state(pid, day=1, total_karma=10)
    c.stop(pid)
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

import requests

CLOUD = "https://api.multilogin.com"
LAUNCH = "https://launcher.mlx.yt:45001"

DEFAULT_FLAGS_DESKTOP = {
    "audio_masking": "natural",
    "fonts_masking": "natural",
    "geolocation_masking": "natural",
    "geolocation_popup": "prompt",
    "graphics_masking": "mask",
    "graphics_noise": "mask",
    "localization_masking": "natural",
    "media_devices_masking": "natural",
    "navigator_masking": "mask",
    "ports_masking": "natural",
    "proxy_masking": "custom",
    "screen_masking": "natural",
    "timezone_masking": "natural",
    "webrtc_masking": "mask",
}

# Mobile profiles reject "natural" for fonts/geo/etc — use "mask" everywhere.
DEFAULT_FLAGS_MOBILE = {k: "mask" for k in [
    "audio_masking", "fonts_masking", "geolocation_masking", "graphics_masking",
    "graphics_noise", "localization_masking", "media_devices_masking",
    "navigator_masking", "ports_masking", "screen_masking", "timezone_masking",
    "webrtc_masking",
]}
DEFAULT_FLAGS_MOBILE.update({"geolocation_popup": "prompt", "proxy_masking": "custom"})

# Backwards compat alias
DEFAULT_FLAGS = DEFAULT_FLAGS_DESKTOP


class Client:
    def __init__(self, email: str, password: str, mappings_path: str = "mappings.json"):
        self.email = email
        self.password = password
        self.token: Optional[str] = None
        self.mappings_path = Path(mappings_path)

    def _auth(self):
        if not self.token:
            raise RuntimeError("call signin() first")
        return {"Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json", "Accept": "application/json"}

    # ------------------------------------------------------------------ auth
    def signin(self) -> str:
        md5 = hashlib.md5(self.password.encode()).hexdigest()
        r = requests.post(f"{CLOUD}/user/signin",
                          json={"email": self.email, "password": md5},
                          headers={"Content-Type": "application/json"}, timeout=30)
        r.raise_for_status()
        self.token = r.json()["data"]["token"]
        return self.token

    # ----------------------------------------------------- folders & profiles
    def folders(self) -> list:
        r = requests.get(f"{CLOUD}/workspace/folders", headers=self._auth(), timeout=30)
        r.raise_for_status()
        return r.json()["data"]["folders"]

    def search_profiles(self, search_text: str = "", limit: int = 100) -> list:
        r = requests.post(f"{CLOUD}/profile/search", headers=self._auth(),
                          json={"search_text": search_text, "limit": limit}, timeout=30)
        r.raise_for_status()
        return r.json()["data"]["profiles"]

    def get_profile(self, profile_id: str) -> dict:
        # API rejects limit > 100. Paginate via search_text fallback (id prefix) if needed.
        for p in self.search_profiles("", limit=100):
            if p["id"] == profile_id:
                return p
        # Fallback: targeted search by id prefix (works for >100 profile accounts)
        for p in self.search_profiles(profile_id[:8], limit=100):
            if p["id"] == profile_id:
                return p
        raise KeyError(f"profile {profile_id} not found")

    def create_profile(self, folder_id: str, name: str, proxy: Optional[dict] = None,
                       flags: Optional[dict] = None, os_type: str = "windows") -> str:
        """Create a persistent profile.

        os_type:
          - "windows" / "macos" / "linux"  → desktop, uses DEFAULT_FLAGS_DESKTOP
          - "android"                      → mobile, uses DEFAULT_FLAGS_MOBILE
            (mobile rejects "natural" for fonts/geo/etc — only "mask" works)
        """
        if flags is None:
            flags = DEFAULT_FLAGS_MOBILE if os_type == "android" else DEFAULT_FLAGS_DESKTOP
        params = {"flags": dict(flags)}
        if proxy:
            params["proxy"] = proxy
        else:
            params["flags"]["proxy_masking"] = "disabled"
        body = {"name": name, "folder_id": folder_id,
                "browser_type": "mimic", "os_type": os_type,
                "parameters": params}
        r = requests.post(f"{CLOUD}/profile/create", headers=self._auth(),
                          json=body, timeout=60)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"create_profile failed [{r.status_code}]: {r.text}")
        return r.json()["data"]["ids"][0]

    def copy_proxy_from(self, source_profile_id: str) -> dict:
        return self.get_profile(source_profile_id)["proxy"]

    def delete_profile(self, profile_id: str, permanently: bool = True) -> None:
        r = requests.post(f"{CLOUD}/profile/remove", headers=self._auth(),
                          json={"ids": [profile_id], "permanently": permanently},
                          timeout=30)
        r.raise_for_status()

    # --------------------------------------------------------------- lifecycle
    def start(self, folder_id: str, profile_id: str,
              automation_type: str = "playwright", headless: bool = False) -> int:
        url = (f"{LAUNCH}/api/v2/profile/f/{folder_id}/p/{profile_id}/start"
               f"?automation_type={automation_type}"
               f"&headless_mode={'true' if headless else 'false'}")
        r = requests.get(url, headers=self._auth(), timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"start failed [{r.status_code}]: {r.text}")
        return r.json()["data"]["port"]

    def stop(self, profile_id: str) -> bool:
        # PATH-style endpoint, NOT query-string
        r = requests.get(f"{LAUNCH}/api/v1/profile/stop/p/{profile_id}",
                         headers=self._auth(), timeout=30)
        return r.status_code == 200

    def statuses(self) -> dict:
        r = requests.get(f"{LAUNCH}/api/v1/profile/statuses",
                         headers=self._auth(), timeout=15)
        r.raise_for_status()
        return r.json()["data"]

    # --------------------------------------------- partial update / save_state
    def partial_update(self, profile_id: str, **fields) -> None:
        body = {"profile_id": profile_id, **fields}
        r = requests.post(f"{CLOUD}/profile/partial_update",
                          headers=self._auth(), json=body, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"partial_update failed [{r.status_code}]: {r.text}")

    def save_state(self, profile_id: str, **state) -> dict:
        """Merge state into the profile's notes JSON. Server is source of truth."""
        try:
            existing = json.loads(self.get_profile(profile_id).get("notes") or "{}")
        except json.JSONDecodeError:
            existing = {}
        merged = {**existing, **state, "last_updated": int(time.time())}
        self.partial_update(profile_id, notes=json.dumps(merged, ensure_ascii=False))
        return merged

    def rename(self, profile_id: str, name: str) -> None:
        self.partial_update(profile_id, name=name)

    # -------------------------------------------------- account binding helpers
    def _read_mappings(self) -> dict:
        if not self.mappings_path.exists():
            return {}
        try:
            return json.loads(self.mappings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _write_mappings(self, mapping: dict) -> None:
        self.mappings_path.write_text(json.dumps(mapping, indent=2, ensure_ascii=False),
                                      encoding="utf-8")

    def bind_user(self, profile_id: str, account: str, device: str = "desktop",
                  purpose: str = "reddit-warmup", extra: Optional[dict] = None) -> dict:
        """Tie an account+device to a profile in three places:
        - Multilogin profile name (e.g. "reddit-{user}-{device}")
        - Multilogin profile notes (JSON metadata, includes device)
        - Local mappings.json (nested: {account: {devices: {device: {...}}}})
        """
        notes = json.dumps({
            "purpose": purpose,
            "reddit_user": account,
            "device": device,
            "bound_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **(extra or {}),
        }, ensure_ascii=False)
        prefix = purpose.split("-")[0]
        self.partial_update(profile_id, name=f"{prefix}-{account}-{device}", notes=notes)

        profile = self.get_profile(profile_id)
        proxy_country = "??"
        uname = (profile.get("proxy") or {}).get("username", "")
        if "country-" in uname:
            proxy_country = uname.split("country-")[1].split("-")[0]

        mapping = self._read_mappings()
        entry = mapping.setdefault(account, {"devices": {}, "bound_at": int(time.time())})
        # Migrate legacy flat-format entry if present
        if "profile_id" in entry and "devices" not in entry:
            entry = {"devices": {"desktop": {
                "profile_id": entry.pop("profile_id"),
                "folder_id": entry.pop("folder_id"),
                "os_type": "windows", "browser_type": "mimic",
            }}, **entry}
            mapping[account] = entry
        entry["devices"][device] = {
            "profile_id": profile_id,
            "folder_id": profile["folder_id"],
            "os_type": profile.get("os_type", "windows"),
            "browser_type": profile.get("browser_type", "mimic"),
        }
        if proxy_country != "??":
            entry["primary_proxy"] = f"{proxy_country}-{profile.get('os_type','?')}"
        self._write_mappings(mapping)
        return entry["devices"][device]

    def resolve(self, account: str, device: str = "desktop") -> dict:
        """Look up the {profile_id, folder_id, ...} for account+device in mappings.json."""
        mapping = self._read_mappings().get(account)
        if not mapping:
            raise KeyError(f"{account} not in {self.mappings_path}")
        # Handle both nested and legacy flat
        if "devices" in mapping:
            if device not in mapping["devices"]:
                raise KeyError(f"{account} has no '{device}' device bound (have: "
                               f"{list(mapping['devices'])})")
            return mapping["devices"][device]
        # legacy flat = treat as desktop
        if device != "desktop":
            raise KeyError(f"{account} stored in legacy flat form; only 'desktop' available")
        return {"profile_id": mapping["profile_id"], "folder_id": mapping["folder_id"],
                "os_type": "windows", "browser_type": "mimic"}

    def start_by_user(self, account: str, device: str = "desktop",
                      automation_type: str = "playwright",
                      headless: bool = False) -> tuple[str, int]:
        """Look up account+device, start its profile, return (profile_id, port)."""
        m = self.resolve(account, device)
        port = self.start(m["folder_id"], m["profile_id"], automation_type, headless)
        return m["profile_id"], port

    # ----------------------------------------------- Reddit-specific convenience
    @staticmethod
    def discover_reddit_user(playwright_page) -> Optional[str]:
        """Use /user/me redirect to get the canonical logged-in username.
        DO NOT scrape /user/<x>/ links from DOM — those include promoted ads.
        """
        playwright_page.goto("https://www.reddit.com/user/me",
                             timeout=30000, wait_until="domcontentloaded")
        time.sleep(4)
        url = playwright_page.url
        if "/user/" not in url:
            return None
        tail = url.split("/user/", 1)[1].split("/")[0].strip()
        return tail if tail and tail.lower() != "me" else None
