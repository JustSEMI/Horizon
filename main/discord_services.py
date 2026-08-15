import os
import json
import time
import signal
import sys
import logging
import threading
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
import pystray
from PIL import Image, ImageDraw

# Konfigurasi Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

def create_image():
    # Membuat ikon kotak sederhana untuk System Tray.
    image = Image.new('RGB', (64, 64), color=(88, 101, 242)) # Discord Blurple
    d = ImageDraw.Draw(image)
    d.rectangle([16, 16, 48, 48], fill=(255, 255, 255))
    return image

class StorageManager:
    # Mengelola penyimpanan state game yang sudah dinotifikasi.
    
    def __init__(self, filepath: str):
        self.filepath = filepath
        self._ensure_file()
        
    def _ensure_file(self) -> None:
        if not os.path.exists(self.filepath):
            self.save_data({"notified_ids": {}, "last_updated": datetime.now(timezone.utc).isoformat()})
            
    def load_data(self) -> dict[str, Any]:
        with open(self.filepath, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                # Kompatibilitas jika struktur lama berupa list
                if isinstance(data.get("notified_ids"), list):
                    now = datetime.now(timezone.utc).isoformat()
                    data["notified_ids"] = {str(gid): now for gid in data["notified_ids"]}
                return data
            except json.JSONDecodeError:
                return {"notified_ids": {}, "last_updated": datetime.now(timezone.utc).isoformat()}
                
    def save_data(self, data: dict[str, Any]) -> None:
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            
    def is_notified(self, game_id: str) -> bool:
        data = self.load_data()
        return str(game_id) in data.get("notified_ids", {})
        
    def mark_notified(self, game_id: str) -> None:
        data = self.load_data()
        data.setdefault("notified_ids", {})[str(game_id)] = datetime.now(timezone.utc).isoformat()
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        self.save_data(data)
        
    def clean_old_records(self, days: int = 30) -> None:
        # Membersihkan ID game yang berumur lebih dari `days` hari.
        data = self.load_data()
        notified_ids = data.get("notified_ids", {})
        threshold_date = datetime.now(timezone.utc) - timedelta(days=days)
        
        cleaned_ids = {}
        for gid, timestamp_str in notified_ids.items():
            try:
                # Membersihkan akhiran 'Z' yang tidak didukung langsung oleh fromisoformat python versi lama
                clean_ts = timestamp_str.replace("Z", "+00:00")
                dt = datetime.fromisoformat(clean_ts)
                if dt > threshold_date:
                    cleaned_ids[gid] = timestamp_str
            except ValueError:
                pass # Hapus entry jika format error
                
        if len(cleaned_ids) != len(notified_ids):
            logger.info(f"StorageManager: Cleaned up {len(notified_ids) - len(cleaned_ids)} old records.")
            data["notified_ids"] = cleaned_ids
            self.save_data(data)


class FreebieFetcher:
    # Mengambil data game gratis dari GamerPower API.
    
    API_URL = "https://www.gamerpower.com/api/giveaways?type=game&platform=pc"
    TARGET_PLATFORMS = ["Epic Games Store", "Steam", "GOG", "Ubisoft"]
    
    def fetch_free_games(self) -> list[dict[str, Any]]:
        try:
            response = requests.get(self.API_URL, timeout=15)
            response.raise_for_status()
            games = response.json()
            
            valid_games = []
            
            # Keyword untuk mendeteksi giveaway pihak ketiga
            spam_keywords = ["alienware", "gleam.io", "arp", "steelseries", "hitsquad", "crucial", "task", "points"]
            
            for game in games:
                platform = game.get("platforms", "")
                
                # Filter platform eksplisit
                if not any(target.lower() in platform.lower() for target in self.TARGET_PLATFORMS):
                    continue
                    
                # Pastikan giveaway masih aktif
                if game.get("status") != "Active":
                    continue
                    
                # Abaikan giveaway berjenis "Key" (biasanya butuh task) atau yang mengandung keyword spam
                title_lower = game.get("title", "").lower()
                desc_lower = game.get("description", "").lower()
                inst_lower = game.get("instructions", "").lower()
                
                is_key_drop = " key " in f" {title_lower} "
                has_tasks = any(kw in desc_lower or kw in inst_lower for kw in spam_keywords)
                
                if is_key_drop or has_tasks:
                    continue
                    
                valid_games.append(game)
                
            return valid_games
        except requests.exceptions.RequestException as e:
            logger.error(f"Gagal mengambil data dari API GamerPower: {e}")
            return []


class DiscordNotifier:
    # Mengirim notifikasi ke Discord Webhook dengan mapping visual.
    
    BRANDING = {
        "epic": {"hex": 0x000000, "footer": "Epic Games Store • Freebie Alert"},
        "steam": {"hex": 0x1b2838, "footer": "Steam Store • Freebie Alert"},
        "gog": {"hex": 0x8a2be2, "footer": "GOG.com • Freebie Alert"},
        "ubisoft": {"hex": 0x0070d1, "footer": "Ubisoft Connect • Freebie Alert"},
        "default": {"hex": 0x5865F2, "footer": "PC Game • Freebie Alert"}
    }
    
    def __init__(self, webhook_url: str):
        if not webhook_url or not webhook_url.startswith("https://discord.com/api/webhooks/"):
            raise ValueError("Format DISCORD_WEBHOOK_URL tidak valid atau kosong.")
        self.webhook_url = webhook_url

    def _get_branding(self, platform_name: str) -> dict[str, Any]:
        plat_lower = platform_name.lower()
        if "epic" in plat_lower: return self.BRANDING["epic"]
        if "steam" in plat_lower: return self.BRANDING["steam"]
        if "gog" in plat_lower: return self.BRANDING["gog"]
        if "ubisoft" in plat_lower: return self.BRANDING["ubisoft"]
        if "itch" in plat_lower: return self.BRANDING["itch"]
        return self.BRANDING["default"]

    def clean_title(self, raw_title: str) -> str:
        # Hapus teks di dalam kurung seperti (Epic Games) atau (Steam)
        cleaned = re.sub(r'\s*\([^)]*\)', '', raw_title)
        # Hapus kata-kata promosi di akhir judul
        cleaned = re.sub(r'(?i)\s*(key|giveaway|free|early access)+(\s+(key|giveaway|free|early access)+)*$', '', cleaned)
        return cleaned.strip()

    def send_notification(self, game: dict[str, Any]) -> bool:
        platform = game.get("platforms", "PC")
        branding = self._get_branding(platform)
        
        raw_title = game.get("title", "")
        clean_title_str = self.clean_title(raw_title)
        
        description = game.get("description", "")
        if len(description) > 150:
            description = description[:147] + "..."
            
        # Karena Discord API menolak tombol dari Webhook standar, kita gunakan link markdown yang tebal
        claim_url = game.get("open_giveaway_url", "")
        description += f"\n\n**[▶ KLIK DI SINI UNTUK CLAIM GAME]({claim_url})**"
        
        worth = game.get("worth", "N/A")
        end_date = game.get("end_date", "N/A")
        
        embed = {
            "author": {
                "name": platform
            },
            "title": clean_title_str,
            "url": claim_url, 
            "description": description,
            "color": branding["hex"],
            "fields": [
                {"name": "Harga", "value": f"~~{worth}~~  →  **FREE**", "inline": True},
                {"name": "Batas Waktu", "value": end_date if end_date != "N/A" else "Tidak ditentukan", "inline": True}
            ],
            "image": {"url": game.get("image")},
            "footer": {"text": branding["footer"]},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        payload = {
            "username": "P-HORIZON",
            "avatar_url": "https://avatars.githubusercontent.com/u/197373255?v=4",
            "embeds": [embed]
        }
        
        while True:
            try:
                response = requests.post(self.webhook_url, json=payload, timeout=10)
                if response.status_code == 204:
                    logger.info(f"Notification sent successfully: {game.get('title')}")
                    return True
                elif response.status_code == 429:
                    # Menangani Webhook Rate Limit
                    retry_after = response.json().get("retry_after", 5.0)
                    logger.warning(f"Rate limited oleh Discord. Menunggu {retry_after} detik...")
                    time.sleep(retry_after)
                    continue
                else:
                    logger.error(f"Discord API Error {response.status_code}: {response.text}")
                    return False
            except requests.exceptions.RequestException as e:
                logger.error(f"Kesalahan jaringan saat mengirim webhook: {e}")
                return False


class Daemon:
    # Orkestrator utama untuk menjalankan loop daemon.
    
    def __init__(self):
        import tempfile
        import os
        HORIZON_DIR = os.environ.get("DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        config_path = os.path.join(HORIZON_DIR, "settings.json")
        try:
            with open(config_path, 'r') as f:
                data = json.load(f)
                freebie_config = data.get("freebie", {})
                webhook_url = freebie_config.get("discord_webhook", "")
                check_interval_mins = int(freebie_config.get("check_interval_mins", "360"))
        except Exception as e:
            logger.error(f"Gagal memuat settings.json: {e}")
            sys.exit(1)
            
        self.check_interval = check_interval_mins * 60
        logger.setLevel(logging.INFO)
        
        self.fetcher = FreebieFetcher()
        self.notifier = DiscordNotifier(webhook_url)
        self.storage = StorageManager(os.path.join(os.path.dirname(__file__), "notified_games.json"))
        
        self.is_running = True
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum: int, frame: Any) -> None:
        logger.info("Received termination signal. Stopping daemon safely...")
        self.is_running = False
        if hasattr(self, 'icon'):
            self.icon.stop()
        sys.exit(0)
        
    def _run_loop(self) -> None:
        logger.info(f"Daemon started. Check interval: {self.check_interval // 60} minutes.")
        
        while self.is_running:
            self.storage.clean_old_records(days=30)
            
            games = self.fetcher.fetch_free_games()
            logger.info(f"Found {len(games)} target free games from API.")
            
            for game in games:
                game_id = str(game.get("id"))
                if not self.storage.is_notified(game_id):
                    success = self.notifier.send_notification(game)
                    if success:
                        self.storage.mark_notified(game_id)
                        time.sleep(2) # Backoff tambahan antar notifikasi
            
            # Loop sleep aman yang bisa diinterupsi
            sleep_count = 0
            while sleep_count < self.check_interval and self.is_running:
                time.sleep(1)
                sleep_count += 1
                
    def run(self) -> None:
        # Jalankan loop pengecekan langsung (tanpa system tray)
        logger.info("Game Webhook application running under P-HORIZON Dashboard control.")
        self._run_loop()


def run_webhook() -> None:
    try:
        daemon = Daemon()
        daemon.run()
    except ValueError as e:
        logger.error(f"Konfigurasi tidak valid: {e}")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Daemon gagal dijalankan: {e}")
        sys.exit(1)

#


import os
import time
import signal
import sys
import logging
import json
from typing import Any

from pypresence import Presence
from pypresence.exceptions import PyPresenceException
import win32gui
import win32process
import psutil

# Konfigurasi Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

def load_config() -> dict[str, str]:
    import tempfile
    import os
    HORIZON_DIR = os.environ.get("DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    config_path = os.path.join(HORIZON_DIR, "settings.json")
    if not os.path.exists(config_path):
        raise ValueError("File settings.json tidak ditemukan!")
        
    with open(config_path, 'r') as f:
        data = json.load(f)
        
    client_id = data.get("app_sync", {}).get("discord_client_id", "")
    if not client_id:
        raise ValueError("Discord Client ID belum disetel di settings.json!")
        
    return {"DISCORD_CLIENT_ID": client_id}

def get_active_window_info() -> tuple[str, str] | None:
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
            
        window_title = win32gui.GetWindowText(hwnd)
        if not window_title.strip():
            return None
            
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process = psutil.Process(pid)
        process_name = process.name().replace('.exe', '').capitalize()
        
        return process_name, window_title
    except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
        return None

class AppDiscordSync:
    def __init__(self, client_id: str):
        self.rpc = Presence(client_id)
        
        self.is_running = True
        self.last_window_title = None
        self.window_start_time = int(time.time())

        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum: int, frame: Any) -> None:
        logger.info("Received termination signal. Cleaning up Discord status...")
        self.is_running = False
        try:
            self.rpc.clear()
            self.rpc.close()
        except Exception as e:
            logger.error(f"Gagal membersihkan RPC: {e}")
        sys.exit(0)

    def connect(self) -> None:
        self.rpc.connect()
        logger.info("Connected to Discord RPC.")

    def update_window_presence(self, process_name: str, window_title: str) -> None:
        if self.last_window_title == window_title:
            return

        details = window_title[:128]
        state = f"App: {process_name}"[:128]

        self.rpc.update(
            details=details,
            state=state,
            large_text=process_name,
            start=self.window_start_time,
            small_image="https://avatars.githubusercontent.com/u/197373255?v=4", 
            small_text="PROJECT HORIZON v0.1b"
        )
        self.last_window_title = window_title
        logger.info(f"RPC [ACTIVE APP]: [{process_name}] {details}")

    def run(self, interval: int = 5) -> None:
        self.connect()
        logger.info("Starting App Status Sync...")
        
        while self.is_running:
            try:
                window_info = get_active_window_info()
                if window_info:
                    process_name, window_title = window_info
                    
                    p_name = process_name.lower()
                    w_title = window_title.lower()
                    
                    is_ignored = (
                        p_name in ["searchapp", "startmenuexperiencehost", "shellexperiencehost"] or
                        "discord" in p_name or "discord" in w_title or
                        "antigravity" in p_name or "antigravity" in w_title
                    )
                    
                    if not is_ignored:
                        self.update_window_presence(process_name, window_title)
                    else:
                        if self.last_window_title is not None:
                            self.rpc.clear()
                            self.last_window_title = None
                else:
                    if self.last_window_title is not None:
                        self.rpc.clear()
                        self.last_window_title = None
                        
            except PyPresenceException as e:
                logger.error(f"Discord RPC Error: {e}")
                self._reconnect_discord()
            except Exception as e:
                logger.error(f"Kesalahan tak terduga: {e}")
            
            time.sleep(interval)
            
    def _reconnect_discord(self) -> None:
        logger.info("Attempting to reconnect to Discord in 10 seconds...")
        time.sleep(10)
        try:
            self.rpc.connect()
            self.last_window_title = None 
            logger.info("Successfully reconnected to Discord RPC.")
        except Exception as e:
            logger.error(f"Gagal menghubungkan ulang: {e}")

def run_rpcsync() -> None:
    try:
        config = load_config()
        sync_app = AppDiscordSync(config["DISCORD_CLIENT_ID"])
        sync_app.run(interval=5)
        
    except ValueError as e:
        logger.error(f"Konfigurasi tidak valid: {e}")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Aplikasi gagal dimulai: {e}")
        sys.exit(1)

#
    main()


import argparse
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["rpc", "webhook"], required=True)
    args = parser.parse_args()
    
    if args.mode == "rpc":
        run_rpcsync()
    elif args.mode == "webhook":
        run_webhook()
