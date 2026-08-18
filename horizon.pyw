# main.pyw
import sys
import os
import tempfile
import shutil
import subprocess

# BOOTSTRAP
current_dir = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(tempfile.gettempdir(), "HORIZON")
LOCAL_APP_DIR = os.path.join(CACHE_DIR, "app")

if not current_dir.lower().startswith(CACHE_DIR.lower()):
    os.environ["DIR"] = current_dir
    try:
        shutil.copytree(
            current_dir, 
            LOCAL_APP_DIR, 
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns('.git', 'log', '__pycache__', '.cache')
        )
        copied_script = os.path.join(LOCAL_APP_DIR, os.path.basename(__file__))
        subprocess.Popen([sys.executable, copied_script], cwd=LOCAL_APP_DIR)
        sys.exit(0)
    except Exception as e:
        pass # Fallback run from removable drive if copy fails

os.chdir(os.path.dirname(os.path.abspath(__file__)))

import json
import time
import threading
import logging
import winreg
from collections import deque

import customtkinter as ctk
import pystray
from PIL import Image, ImageDraw
from iconipy import IconFactory
from main.System.monitor import SystemMonitor

# Cache IconFactory: satu set icon (lucide) hanya di-load sekali per
# kombinasi (ukuran, warna). Mencegah puluhan re-load saat build UI.
_icon_factory_cache = {}

def get_icon_factory(size, color, icon_set='lucide'):
    key = (icon_set, size, color)
    if key not in _icon_factory_cache:
        _icon_factory_cache[key] = IconFactory(icon_set=icon_set, icon_size=size, font_color=color)
    return _icon_factory_cache[key]

CACHE_DIR = os.path.join(tempfile.gettempdir(), "HORIZON")
os.makedirs(CACHE_DIR, exist_ok=True)

HORIZON_DIR = os.environ.get("DIR", os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(HORIZON_DIR, "settings.json")
DEFAULT_CONFIG = {
    "app_sync": {
        "discord_client_id": ""
    },
    "freebie": {
        "discord_webhook": "",
        "check_interval_mins": "15",
        "target_platforms": "Epic Games Store,Steam,GOG,Ubisoft"
    },
    "dashboard": {}
}

class ConfigManager:
    @staticmethod
    def load():
        if not os.path.exists(CONFIG_FILE):
            ConfigManager.save(DEFAULT_CONFIG)
            return DEFAULT_CONFIG
        try:
            with open(CONFIG_FILE, 'r') as f:
                loaded = json.load(f)
            
            # Merge dictionary
            for key, val in DEFAULT_CONFIG.items():
                if key not in loaded:
                    loaded[key] = val
                elif isinstance(val, dict) and isinstance(loaded.get(key), dict):
                    for sub_key, sub_val in val.items():
                        if sub_key not in loaded[key]:
                            loaded[key][sub_key] = sub_val
            
            return loaded
        except Exception:
            return DEFAULT_CONFIG

    @staticmethod
    def save(data):
        try:
            os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
            with open(CONFIG_FILE, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print(f"Error saving config: {e}")

config = ConfigManager.load()

log_queue = deque()
logger = logging.getLogger("Dashboard")
logger.setLevel(logging.INFO)

# Handler log khusus untuk CustomTkinter agar tidak memblokir thread
class CTkLogHandler(logging.Handler):
    def __init__(self, log_queue: deque, max_len=100):
        super().__init__()
        self.log_queue = log_queue
        self.max_len = max_len
        # Counter monoton: dipakai UI untuk deteksi log baru tanpa terpengaruh
        # batas maxlen deque (len() akan mentok di max_len dan berhenti berubah).
        self.counter = 0
        self.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%H:%M:%S'))

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        self.log_queue.append(msg)
        self.counter += 1
        if len(self.log_queue) > self.max_len:
            self.log_queue.popleft()

if sys.stdout is not None:
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%H:%M:%S'))
    logger.addHandler(stdout_handler)

gui_handler = CTkLogHandler(log_queue)
logger.addHandler(gui_handler)

# File Log Handler
LOG_DIR = os.path.join(HORIZON_DIR, "log")
os.makedirs(LOG_DIR, exist_ok=True)
current_time = time.strftime("%Y-%m-%d_%H-%M-%S")
LOG_FILE = os.path.join(LOG_DIR, f"{current_time}.log")

try:
    file_handler = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(file_handler)
except Exception as e:
    pass # If log creation fails (e.g. permission denied), ignore it

class BaseWorker:
    def __init__(self, name, tag, script_path, script_args=None):
        self.name = name
        self.tag = tag
        self.script_path = script_path
        self.script_args = script_args or []
        self.is_running = False
        self.process = None
        self.log_thread = None
        # Callback untuk update UI
        self.update_ui_callback = None

    def start(self):
        if not self.is_running:
            self.is_running = True
            
            # Start the subprocess with unbuffered output
            cmd = [sys.executable, "-u", self.script_path] + self.script_args
            self.process = subprocess.Popen(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT, 
                text=True, 
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            
            self.log_thread = threading.Thread(target=self._read_logs, daemon=True)
            self.log_thread.start()
            
            logger.info(f"{self.name} enabled (PID: {self.process.pid}).")
            if self.update_ui_callback:
                self.update_ui_callback(self.tag, True)

    def stop(self):
        if self.is_running:
            self.is_running = False
            if self.process:
                self.process.terminate()
                self.process = None
            logger.info(f"Stopping {self.name}...")
            if self.update_ui_callback:
                self.update_ui_callback(self.tag, False)

    def _read_logs(self):
        if not self.process:
            return
        # Read lines one by one from subprocess
        for line in iter(self.process.stdout.readline, ''):
            if not line:
                break
            line = line.strip()
            if line:
                # Add it to the main logger directly gui_handler will pick it up
                logger.info(f"[{self.tag.upper()}] {line}")
        
        if self.is_running: # Process crashed/exited unexpectedly
            self.is_running = False
            if self.update_ui_callback:
                self.update_ui_callback(self.tag, False)

class AppStatusSyncWorker(BaseWorker):
    def __init__(self):
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main", "discord_services.py")
        super().__init__("Discord RPC Sync", "app_sync", script_path, ["--mode", "rpc"])

class FreebieAlertWorker(BaseWorker):
    def __init__(self):
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main", "discord_services.py")
        super().__init__("Free Games Notifier", "freebie", script_path, ["--mode", "webhook"])

class PlatformChecklist(ctk.CTkFrame):
    AVAILABLE_PLATFORMS = ["Epic Games Store", "Steam", "GOG", "Ubisoft", "itch.io"]

    def __init__(self, master, selected_csv="", accent_color="#3b82f6", get_font=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)

        selected = {p.strip().lower() for p in selected_csv.split(",") if p.strip()}
        self.vars = {}

        self.grid_columnconfigure((0, 1), weight=1)
        for i, platform in enumerate(self.AVAILABLE_PLATFORMS):
            var = ctk.StringVar(value=platform if platform.lower() in selected else "")
            chk = ctk.CTkCheckBox(
                self, text=platform, variable=var, onvalue=platform, offvalue="",
                font=get_font(size=12) if get_font else None,
                fg_color=accent_color, hover_color=accent_color,
                text_color=("#111827", "#ffffff"), checkbox_width=18, checkbox_height=18
            )
            chk.grid(row=i // 2, column=i % 2, padx=5, pady=4, sticky="w")
            self.vars[platform] = var

    def get(self):
        # Kembalikan string CSV dari platform yang dicentang sesuai format settings.json
        return ",".join(p for p, v in self.vars.items() if v.get())

class CollapsibleFeature(ctk.CTkFrame):
    def __init__(self, master, title, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        
        self.is_expanded = False
        self.animating = False
        self.target_height = 0
        self.current_height = 0
        
        # Lucide Icons untuk chevron
        ic_fac_light = get_icon_factory(18, '#111827')
        ic_fac_dark = get_icon_factory(18, '#d4d4d8')
        self.icon_right = ctk.CTkImage(light_image=ic_fac_light.asPil('chevron-right'), dark_image=ic_fac_dark.asPil('chevron-right'), size=(18, 18))
        self.icon_down = ctk.CTkImage(light_image=ic_fac_light.asPil('chevron-down'), dark_image=ic_fac_dark.asPil('chevron-down'), size=(18, 18))
        
        # Header dropdown
        self.btn_header = ctk.CTkButton(
            self, image=self.icon_right, text=f" {title}", font=ctk.CTkFont(family="JetBrains Mono", size=14, weight="bold"), 
            fg_color=("#d1d5db", "#18181b"), hover_color=("#d1d5db", "#27272a"), text_color=("#111827", "#ffffff"), corner_radius=8,
            anchor="w", command=self.toggle, height=40
        )
        self.btn_header.pack(fill="x", pady=2)
        
        # Wrapper frame dengan tinggi statis
        self.wrapper_frame = ctk.CTkFrame(self, fg_color="transparent", height=0)
        self.wrapper_frame.pack_propagate(False)
        
        # Area isi dari dropdown
        self.content_frame = ctk.CTkFrame(self.wrapper_frame, fg_color="transparent")
        
    def toggle(self):
        self.is_expanded = not self.is_expanded
        
        if self.is_expanded:
            self.btn_header.configure(image=self.icon_down)
            
            # Tampilkan langsung (Snap open)
            self.wrapper_frame.pack(fill="x", padx=5, pady=(5, 10))
            self.content_frame.pack(fill="x")
            
            self.update_idletasks()
            self.target_height = self.content_frame.winfo_reqheight()
            
            self.wrapper_frame.configure(height=self.target_height)
            self.wrapper_frame.pack_propagate(True)
        else:
            self.btn_header.configure(image=self.icon_right)
            
            # Sembunyikan langsung (Snap close)
            self.wrapper_frame.configure(height=0)
            self.wrapper_frame.pack_forget()
            self.content_frame.pack_forget()

class DashboardApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.app_start_time = time.time()
        self.sys_monitor = SystemMonitor()
        
        saved_theme = config.get("dashboard", {}).get("color_theme", "blue")
        if saved_theme == "green":
            self.accent_color = "#10b981"
            self.accent_hover_color = "#059669"
        elif saved_theme == "dark-blue":
            self.accent_color = "#3730a3"
            self.accent_hover_color = "#312e81"
        else: # blue
            self.accent_color = "#3b82f6"
            self.accent_hover_color = "#2563eb"
            
        self._accent_widgets = []
        
        # Load custom fonts
        font_dir = os.path.join(os.path.dirname(__file__), "assets", "font", "JetBrains Mono", "ttf")
        if os.path.exists(font_dir):
            ctk.FontManager.load_font(os.path.join(font_dir, "JetBrainsMono-Regular.ttf"))
            ctk.FontManager.load_font(os.path.join(font_dir, "JetBrainsMono-Bold.ttf"))

        self.title("HORIZON Dashboard")
        self.geometry("700x440")
        self.resizable(True, True)
        self.minsize(700, 440)

        # Set Window Icon
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('horizon.dashboard.app')
        except Exception:
            pass

        from PIL import ImageTk
        ic_fac = get_icon_factory(64, self.accent_color)
        window_icon_pil = ic_fac.asPil('circle')
        
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.ico")
        os.makedirs(os.path.dirname(icon_path), exist_ok=True)
        if not os.path.exists(icon_path):
            window_icon_pil.save(icon_path, format="ICO")
        self.iconbitmap(icon_path)

        # Tangani klik silang (X) sesuai setting
        self.protocol("WM_DELETE_WINDOW", self.on_close_window)

        self.workers = [
            AppStatusSyncWorker(),
            FreebieAlertWorker()
        ]
        
        self.switches = {}
        self.status_labels = {}
        
        self.tray_icon = None
        
        # Set tema CustomTkinter
        saved_appearance = config.get("dashboard", {}).get("appearance_mode", "Dark")
        saved_scaling = config.get("dashboard", {}).get("ui_scaling", "100%")
        
        ctk.set_appearance_mode(saved_appearance)
        saved_theme = config.get("dashboard", {}).get("color_theme", "blue")
        ctk.set_default_color_theme(saved_theme)
        
        scaling_float = int(saved_scaling.replace("%", "")) / 100
        ctk.set_widget_scaling(scaling_float)
        
        # Inisialisasi startup ke windows registry berdasarkan setting terakhir
        saved_startup = config.get("dashboard", {}).get("run_on_startup", "Disabled")
        self.apply_startup_registry(saved_startup)
        
        self.build_ui()
        self.init_tray()
        
        # Inject callback UI ke setiap worker
        for w in self.workers:
            w.update_ui_callback = self.on_worker_status_changed

        # Polling queue log secara realtime
        self.after(100, self.poll_logs)

    def on_worker_status_changed(self, tag, is_running):
        def update():
            # Update posisi switch agar tersinkronisasi jika ditekan via kode
            if tag in self.switches:
                if is_running and self.switches[tag].get() == 0:
                    self.switches[tag].select()
                elif not is_running and self.switches[tag].get() == 1:
                    self.switches[tag].deselect()
            # Refresh menu tray supaya label start/stop & checkmark ikut update
            if getattr(self, 'tray_icon', None):
                self.tray_icon.update_menu()
                    
        self.after(0, update)

    def get_font(self, size=12, weight="normal", slant="roman"):
        if not hasattr(self, '_font_cache'):
            self._font_cache = {}
        key = (size, weight, slant)
        if key not in self._font_cache:
            self._font_cache[key] = ctk.CTkFont(family="JetBrains Mono", size=size, weight=weight, slant=slant)
        return self._font_cache[key]

    def build_ui(self):
        self.configure(fg_color=("#f3f4f6", "#1c1c1e")) # Background warna gelap khas Hub
        
        # Grid Layout
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        
        # SIDEBAR
        self.sidebar_frame = ctk.CTkFrame(self, width=210, corner_radius=0, fg_color=("#ffffff", "#131315"))
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(7, weight=1) # Spacer
        
        # Sidebar Header
        self.header_frame = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        self.header_frame.grid(row=0, column=0, padx=20, pady=(20, 30), sticky="w")
        
        self.logo_label = ctk.CTkLabel(self.header_frame, text="Horizon", font=self.get_font(size=17, weight="bold"), text_color=("#111827", "#ffffff"))
        self.logo_label.pack(anchor="w")
        self.sub_label = ctk.CTkLabel(self.header_frame, text="@JustSEMI", font=self.get_font(size=11), text_color=("#6b7280", "gray50"))
        self.sub_label.pack(anchor="w", pady=(0,0))
        
        # Lucide Icons
        ic_fac_light = get_icon_factory(18, '#111827')
        ic_fac_dark = get_icon_factory(18, '#d4d4d8')
        img_hide = ctk.CTkImage(light_image=ic_fac_light.asPil('minimize-2'), dark_image=ic_fac_dark.asPil('minimize-2'), size=(18, 18))

        tabs_info = [
            ("Dashboard", 'layout-dashboard'),
            ("Discord", 'squircle'),
            ("Scraping", 'squircle'),
            ("Logs", 'terminal'),
            ("Settings", 'settings'),
            ("Credits", 'star')
        ]
        
        self.sidebar_buttons = {}
        for idx, (t_name, i_name) in enumerate(tabs_info, start=1):
            img = ctk.CTkImage(light_image=ic_fac_light.asPil(i_name), dark_image=ic_fac_dark.asPil(i_name), size=(18, 18))
            btn = ctk.CTkButton(self.sidebar_frame, image=img, text=f"  {t_name}", font=self.get_font(size=13, weight="bold"), fg_color="transparent", text_color=("#374151", "gray80"), hover_color=("#e5e7eb", "#242427"), anchor="w", corner_radius=6, command=lambda name=t_name: self.select_tab(name))
            btn.grid(row=idx, column=0, padx=15, pady=2, sticky="ew")
            self.sidebar_buttons[t_name] = btn
        
        self.btn_hide = ctk.CTkButton(self.sidebar_frame, image=img_hide, text="  Hide to Tray", font=self.get_font(size=13, weight="bold"), fg_color="transparent", text_color=("#374151", "gray80"), hover_color=("#e5e7eb", "#242427"), anchor="w", corner_radius=6, command=self.hide_to_tray)
        self.btn_hide.grid(row=8, column=0, padx=15, pady=20, sticky="ew")
        
        # CONTENT AREA
        
        # Frame Dashboard Info Page
        self.frame_dash = ctk.CTkScrollableFrame(self, corner_radius=0, fg_color="transparent")
        self.frame_dash.grid_columnconfigure(0, weight=1)
        
        self.dash_title = ctk.CTkLabel(self.frame_dash, text="Dashboard", font=self.get_font(size=22, weight="bold"), text_color=("#111827", "#ffffff"))
        self.dash_title.grid(row=0, column=0, padx=30, pady=(30, 15), sticky="nw")
        
        version_str = "Unknown"
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.txt"), "r") as vf:
                version_str = vf.read().strip()
        except:
            pass
        info_text = f"Version: {version_str}\nDeveloper: @JustSEMI"
        self.lbl_info = ctk.CTkLabel(self.frame_dash, text=info_text, font=self.get_font(size=13), text_color=("#4b5563", "#9ca3af"), justify="left", anchor="nw")
        self.lbl_info.grid(row=1, column=0, padx=30, pady=10, sticky="nw")
        
        # Overview Card
        self.overview_card = ctk.CTkFrame(self.frame_dash, fg_color=("#e5e7eb", "#252529"), corner_radius=8)
        self.overview_card.grid(row=3, column=0, padx=30, pady=(10, 10), sticky="ew")
        self.overview_card.grid_columnconfigure(0, weight=1)
        self.overview_card.grid_columnconfigure(1, weight=1)
        
        self.lbl_overview_title = ctk.CTkLabel(self.overview_card, text="System Overview", font=self.get_font(size=14, weight="bold"), text_color=("#111827", "#ffffff"))
        self.lbl_overview_title.grid(row=0, column=0, columnspan=2, padx=15, pady=(15, 5), sticky="w")
        
        # Load custom icons for status and uptime
        ic_red = get_icon_factory(14, '#ef4444')
        ic_green = get_icon_factory(14, '#22c55e')
        self.img_status_red = ctk.CTkImage(light_image=ic_red.asPil('circle-dot'), dark_image=ic_red.asPil('circle-dot'), size=(14, 14))
        self.img_status_green = ctk.CTkImage(light_image=ic_green.asPil('circle-check'), dark_image=ic_green.asPil('circle-check'), size=(14, 14))
        self.img_timer = ctk.CTkImage(light_image=ic_fac_light.asPil('clock'), dark_image=ic_fac_dark.asPil('clock'), size=(14, 14))
        
        self.lbl_status = ctk.CTkLabel(self.overview_card, image=self.img_status_red, compound="left", text=" Services: 0 / 2 Active", font=self.get_font(size=12), text_color=("#374151", "#d4d4d8"))
        self.lbl_status.grid(row=1, column=0, padx=15, pady=(5, 15), sticky="w")
        
        self.lbl_uptime = ctk.CTkLabel(self.overview_card, image=self.img_timer, compound="left", text=" Uptime: 00:00:00", font=self.get_font(size=12), text_color=("#374151", "#d4d4d8"))
        self.lbl_uptime.grid(row=1, column=1, padx=15, pady=(5, 15), sticky="e")
        
        # System Monitor
        self.sysmon_card = ctk.CTkFrame(self.frame_dash, fg_color=("#e5e7eb", "#252529"), corner_radius=8)
        self.sysmon_card.grid(row=2, column=0, padx=30, pady=(20, 10), sticky="ew")
        self.sysmon_card.grid_columnconfigure(0, weight=0)
        self.sysmon_card.grid_columnconfigure(1, weight=1)
        
        self.lbl_sysmon_title = ctk.CTkLabel(self.sysmon_card, text="Resource Monitor", font=self.get_font(size=14, weight="bold"), text_color=("#111827", "#ffffff"))
        self.lbl_sysmon_title.grid(row=0, column=0, columnspan=2, padx=15, pady=(15, 10), sticky="w")
        
        ic_cpu = get_icon_factory(16, '#3b82f6')
        ic_ram = get_icon_factory(16, '#10b981')
        ic_net = get_icon_factory(16, '#f59e0b')
        
        self.img_cpu = ctk.CTkImage(light_image=ic_cpu.asPil('cpu'), dark_image=ic_cpu.asPil('cpu'), size=(16, 16))
        self.img_ram = ctk.CTkImage(light_image=ic_ram.asPil('memory-stick'), dark_image=ic_ram.asPil('memory-stick'), size=(16, 16))
        self.img_net = ctk.CTkImage(light_image=ic_net.asPil('activity'), dark_image=ic_net.asPil('activity'), size=(16, 16))
        
        # CPU Row
        self.lbl_cpu_icon = ctk.CTkLabel(self.sysmon_card, image=self.img_cpu, text="")
        self.lbl_cpu_icon.grid(row=1, column=0, padx=(15, 10), pady=5, sticky="w")
        self.lbl_cpu_text = ctk.CTkLabel(self.sysmon_card, text="CPU: 0%", font=self.get_font(size=12), text_color=("#374151", "#d4d4d8"))
        self.lbl_cpu_text.grid(row=1, column=1, padx=(0, 15), pady=5, sticky="w")
        
        self.pb_cpu = ctk.CTkProgressBar(self.sysmon_card, height=6, progress_color="#3b82f6", fg_color=("#d1d5db", "#3f3f46"))
        self.pb_cpu.grid(row=2, column=0, columnspan=2, padx=15, pady=(0, 15), sticky="ew")
        self.pb_cpu.set(0)
        
        # RAM Row
        self.lbl_ram_icon = ctk.CTkLabel(self.sysmon_card, image=self.img_ram, text="")
        self.lbl_ram_icon.grid(row=3, column=0, padx=(15, 10), pady=5, sticky="w")
        self.lbl_ram_text = ctk.CTkLabel(self.sysmon_card, text="RAM: 0 MB / 0 MB (0%)", font=self.get_font(size=12), text_color=("#374151", "#d4d4d8"))
        self.lbl_ram_text.grid(row=3, column=1, padx=(0, 15), pady=5, sticky="w")
        
        self.pb_ram = ctk.CTkProgressBar(self.sysmon_card, height=6, progress_color="#10b981", fg_color=("#d1d5db", "#3f3f46"))
        self.pb_ram.grid(row=4, column=0, columnspan=2, padx=15, pady=(0, 15), sticky="ew")
        self.pb_ram.set(0)
        
        # Network Row
        self.lbl_net_icon = ctk.CTkLabel(self.sysmon_card, image=self.img_net, text="")
        self.lbl_net_icon.grid(row=5, column=0, padx=(15, 10), pady=5, sticky="w")
        self.lbl_net_text = ctk.CTkLabel(self.sysmon_card, text="Net: ↓ 0 KB/s | ↑ 0 KB/s", font=self.get_font(size=12), text_color=("#374151", "#d4d4d8"))
        self.lbl_net_text.grid(row=5, column=1, padx=(0, 15), pady=(5, 15), sticky="w")
        
        # Frame Discord Services
        self.frame_discord = ctk.CTkScrollableFrame(self, corner_radius=0, fg_color="transparent")
        self.frame_discord.grid_columnconfigure(0, weight=1)
        
        self.discord_title = ctk.CTkLabel(self.frame_discord, text="Discord Integrations", font=self.get_font(size=22, weight="bold"), text_color=("#111827", "#ffffff"))
        self.discord_title.grid(row=0, column=0, padx=30, pady=(30, 15), sticky="nw")
        
        # Group Header
        img_plug = ctk.CTkImage(light_image=ic_fac_light.asPil('plug-2'), dark_image=ic_fac_dark.asPil('plug-2'), size=(16, 16))
        self.group_title = ctk.CTkLabel(self.frame_discord, image=img_plug, compound="left", text=" Background Services", font=self.get_font(size=13, weight="bold"), text_color=("#111827", "#ffffff"))
        self.group_title.grid(row=1, column=0, padx=30, pady=(10, 5), sticky="nw")
        
        descriptions = {
            "app_sync": "Displays your active application status on Discord profile",
            "freebie": "Checks and sends notifications for free games automatically"
        }
        
        row_idx = 2
        for worker in self.workers:
            # Dropdown Container
            accordion = CollapsibleFeature(self.frame_discord, worker.name)
            accordion.grid(row=row_idx, column=0, padx=20, pady=5, sticky="nsew")
            
            # Isi dari dropdown
            card = ctk.CTkFrame(accordion.content_frame, fg_color=("#e5e7eb", "#252529"), corner_radius=8)
            card.pack(fill="x", pady=2)
            card.grid_columnconfigure(0, weight=1)
            card.grid_columnconfigure(1, weight=0)
            
            # Left side Title & Desc
            text_frame = ctk.CTkFrame(card, fg_color="transparent")
            text_frame.grid(row=0, column=0, padx=15, pady=10, sticky="w")
            
            lbl_title = ctk.CTkLabel(text_frame, text=f"Enable {worker.name}", font=self.get_font(size=14, weight="bold"), text_color=("#111827", "#ffffff"), anchor="w")
            lbl_title.pack(anchor="w", fill="x")
            
            desc = descriptions.get(worker.tag, "Application service configuration")
            lbl_desc = ctk.CTkLabel(text_frame, text=desc, font=self.get_font(size=11), text_color=("#4b5563", "#9ca3af"), anchor="w", justify="left", wraplength=380)
            lbl_desc.pack(anchor="w", fill="x")
            
            # Right side Switch
            def make_cmd(w=worker):
                def cmd():
                    if self.switches[w.tag].get() == 1:
                        w.start()
                    else:
                        w.stop()
                return cmd

            sw = ctk.CTkSwitch(card, text="", command=make_cmd(), progress_color=self.accent_color, button_color="#ffffff", button_hover_color="#f4f4f5", width=42, switch_height=22, switch_width=42)
            sw.grid(row=0, column=1, padx=20, pady=12, sticky="e")
            self._accent_widgets.append(sw)
            self.switches[worker.tag] = sw

            # Form Pengaturan untuk masing-masing worker
            settings_card = ctk.CTkFrame(accordion.content_frame, fg_color=("#e5e7eb", "#252529"), corner_radius=8)
            settings_card.pack(fill="x", pady=2)
            
            # Dictionary untuk menyimpan reference UI fields per worker
            if not hasattr(self, 'config_entries'):
                self.config_entries = {}
            self.config_entries[worker.tag] = {}
            
            # Label header setting
            lbl_set = ctk.CTkLabel(settings_card, text="Parameter Configuration", font=self.get_font(size=12, weight="bold"), text_color=("#4b5563", "#9ca3af"))
            lbl_set.pack(anchor="w", padx=15, pady=(10, 5))
            
            if not hasattr(self, 'config_validators'):
                self.config_validators = {}
            self.config_validators[worker.tag] = {}
            
            if worker.tag in config:
                for key, val in config[worker.tag].items():
                    if key == "target_platforms":
                        # Render sebagai checklist platform, bukan text entry biasa
                        checklist_widget = self._create_platform_checklist(settings_card, str(val))
                        self.config_entries[worker.tag][key] = checklist_widget
                        
                        validator = self._get_field_validator(key)
                        if validator:
                            self.config_validators[worker.tag][key] = validator
                        continue
                    
                    row_setting = ctk.CTkFrame(settings_card, fg_color="transparent")
                    row_setting.pack(fill="x", padx=15, pady=5)
                    
                    label_text = key.replace("_", " ").title()
                    lbl_field = ctk.CTkLabel(row_setting, text=label_text, font=self.get_font(size=12, weight="bold"), text_color=("#111827", "#ffffff"), width=140, anchor="w")
                    lbl_field.pack(side="left")
                    
                    entry = ctk.CTkEntry(row_setting, font=self.get_font(size=12), fg_color=("#ffffff", "#131315"), border_color=("#9ca3af", "#3f3f46"), text_color=("#111827", "#ffffff"), height=28)
                    entry.pack(side="left", fill="x", expand=True, padx=(5, 0))
                    entry.insert(0, str(val))
                    
                    self.config_entries[worker.tag][key] = entry
                    
                    # Validasi realtime untuk field kritikal
                    validator = self._get_field_validator(key)
                    if validator:
                        self.config_validators[worker.tag][key] = validator
                        def make_validate_cmd(ent=entry, val_fn=validator):
                            def on_change(event=None):
                                is_valid = val_fn(ent.get())
                                ent.configure(border_color=("#9ca3af", "#3f3f46") if is_valid else ("#ef4444", "#ef4444"))
                            return on_change
                        cmd_validate = make_validate_cmd()
                        entry.bind("<KeyRelease>", cmd_validate)
                        cmd_validate() # Jalankan sekali di awal untuk cek nilai default
            
            def make_save_cmd(w_tag=worker.tag):
                def cmd():
                    # Cegah simpan kalau ada field yang tidak valid
                    validators = self.config_validators.get(w_tag, {})
                    for k, val_fn in validators.items():
                        ent = self.config_entries[w_tag][k]
                        if not val_fn(ent.get()):
                            self.show_toast(f"'{k.replace('_', ' ').title()}' tidak valid. Periksa kembali.", 3500)
                            return
                    # Ambil nilai dari input UI dan simpan ke config dictionary
                    for k, ent in self.config_entries[w_tag].items():
                        config[w_tag][k] = ent.get()
                    ConfigManager.save(config)
                    logger.info(f"Configuration '{w_tag}' saved to settings.json")
                    self.show_toast("Configuration saved!", 2000)
                return cmd

            btn_save = ctk.CTkButton(settings_card, text="Save Config", font=self.get_font(size=12, weight="bold"), fg_color=self.accent_color, hover_color=self.accent_hover_color, height=28, command=make_save_cmd())
            btn_save.pack(anchor="e", padx=15, pady=(5, 10))
            self._accent_widgets.append(btn_save)

            row_idx += 1
            
        # Frame Scraper
        self.frame_scraper = ctk.CTkScrollableFrame(self, corner_radius=0, fg_color="transparent")
        self.frame_scraper.grid_columnconfigure(0, weight=1)
        
        self.scrape_title = ctk.CTkLabel(self.frame_scraper, text="Data Extraction", font=self.get_font(size=22, weight="bold"), text_color=("#111827", "#ffffff"))
        self.scrape_title.grid(row=0, column=0, padx=30, pady=(30, 15), sticky="nw")
        
        img_plug = ctk.CTkImage(light_image=ic_fac_light.asPil('plug-2'), dark_image=ic_fac_dark.asPil('plug-2'), size=(16, 16))
        self.scrape_group = ctk.CTkLabel(self.frame_scraper, image=img_plug, compound="left", text=" Scraping Modules", font=self.get_font(size=13, weight="bold"), text_color=("#111827", "#ffffff"))
        self.scrape_group.grid(row=1, column=0, padx=30, pady=(10, 5), sticky="nw")
        
        self.accordion_scrape = CollapsibleFeature(self.frame_scraper, "Google Maps Scraper")
        self.accordion_scrape.grid(row=2, column=0, padx=20, pady=5, sticky="nsew")
        
        self.scrape_card = ctk.CTkFrame(self.accordion_scrape.content_frame, fg_color=("#e5e7eb", "#252529"), corner_radius=8)
        self.scrape_card.pack(fill="x", pady=2)
        self.entry_query = self._create_input_row(self.scrape_card, "Search Query", "", pady=(15, 5))
        self.entry_loc = self._create_input_row(self.scrape_card, "Location", "", pady=5)
        self.entry_max = self._create_input_row(self.scrape_card, "Max Results", "20", pady=5)
        self.entry_min_rating = self._create_input_row(self.scrape_card, "Min Rating (optional)", "", pady=5)
        
        # Membuat row untuk Export Format
        row4 = ctk.CTkFrame(self.scrape_card, fg_color="transparent")
        row4.pack(fill="x", padx=15, pady=(5, 0))
        lbl_format = ctk.CTkLabel(row4, text="Export Format", font=self.get_font(size=12, weight="bold"), text_color=("#111827", "#ffffff"), width=140, anchor="w")
        lbl_format.pack(side="left")
        self.opt_format = ctk.CTkOptionMenu(
            row4, 
            values=["Excel", "Word", "PDF", "HTML"], 
            font=self.get_font(size=12), 
            fg_color=("#ffffff", "#131315"), 
            button_color=self.accent_color, 
            button_hover_color=self.accent_hover_color, 
            text_color=("#111827", "#ffffff"), 
            dropdown_fg_color=("#ffffff", "#18181b"),
            dropdown_hover_color=("#f3f4f6", "#27272a"),
            dropdown_text_color=("#111827", "#ffffff"),
            dropdown_font=self.get_font(size=12),
            height=28
        )
        self.opt_format.pack(side="left", fill="x", expand=True, padx=(5, 0))
        self.opt_format.set("Excel")
        self._accent_widgets.append(self.opt_format)
        
        # Membuat row untuk Export Fields
        row_fields = ctk.CTkFrame(self.scrape_card, fg_color="transparent")
        row_fields.pack(fill="x", padx=15, pady=(5, 0))
        lbl_fields = ctk.CTkLabel(row_fields, text="Export Fields", font=self.get_font(size=12, weight="bold"), text_color=("#111827", "#ffffff"), width=140, anchor="w")
        lbl_fields.pack(side="left", anchor="nw")
        
        self.field_vars = {}
        fields_container = ctk.CTkFrame(row_fields, fg_color="transparent")
        fields_container.pack(side="left", fill="x", expand=True, padx=(5, 0))
        
        default_fields = ["Name", "Category", "Rating", "Address", "Phone", "Website", "URL"]
        fields_container.grid_columnconfigure((0, 1), weight=1)
        for i, field in enumerate(default_fields):
            var = ctk.StringVar(value=field)
            chk = ctk.CTkCheckBox(fields_container, text=field, variable=var, onvalue=field, offvalue="", font=self.get_font(size=11), fg_color=self.accent_color, hover_color=self.accent_hover_color, text_color=("#111827", "#ffffff"), checkbox_width=18, checkbox_height=18)
            chk.grid(row=i//2, column=i%2, padx=5, pady=5, sticky="w")
            self._accent_widgets.append(chk)
            self.field_vars[field] = var
        
        # Membuat row untuk Deskripsi Format
        row_desc = ctk.CTkFrame(self.scrape_card, fg_color="transparent")
        row_desc.pack(fill="x", padx=15, pady=(0, 5))
        lbl_spacer = ctk.CTkLabel(row_desc, text="", width=140) # Spacer to align text
        lbl_spacer.pack(side="left")
        lbl_format_desc = ctk.CTkLabel(row_desc, text="*Choose the output format for your scraped data", font=self.get_font(size=9, slant="italic"), text_color=("#6b7280", "#9ca3af"), justify="left")
        lbl_format_desc.pack(side="left", padx=(5, 0))
        
        # Membuat tombol Start
        self.btn_start_scrape = ctk.CTkButton(self.scrape_card, text="Start Scraping", font=self.get_font(size=12, weight="bold"), fg_color=self.accent_color, hover_color=self.accent_hover_color, command=self.start_scraper)
        self.btn_start_scrape.pack(fill="x", padx=15, pady=(15, 15))
        self._accent_widgets.append(self.btn_start_scrape)
        self.scraper_process = None
        self.scraper_thread = None

        # Section History (digabung ke tab Scraping supaya jumlah tab tidak bertambah)
        img_history = ctk.CTkImage(light_image=ic_fac_light.asPil('history'), dark_image=ic_fac_dark.asPil('history'), size=(16, 16))
        self.history_group = ctk.CTkLabel(self.frame_scraper, image=img_history, compound="left", text=" Scraping History", font=self.get_font(size=13, weight="bold"), text_color=("#111827", "#ffffff"))
        self.history_group.grid(row=3, column=0, padx=30, pady=(20, 5), sticky="nw")
        
        history_toolbar = ctk.CTkFrame(self.frame_scraper, fg_color="transparent")
        history_toolbar.grid(row=4, column=0, padx=30, pady=(0, 10), sticky="ew")
        
        img_refresh = ctk.CTkImage(light_image=ic_fac_light.asPil('refresh-cw'), dark_image=ic_fac_dark.asPil('refresh-cw'), size=(14, 14))
        img_folder = ctk.CTkImage(light_image=ic_fac_light.asPil('folder-open'), dark_image=ic_fac_dark.asPil('folder-open'), size=(14, 14))
        
        self.btn_history_refresh = ctk.CTkButton(history_toolbar, image=img_refresh, text=" Refresh", font=self.get_font(size=12, weight="bold"), fg_color=self.accent_color, hover_color=self.accent_hover_color, height=28, width=100, command=self.refresh_history)
        self.btn_history_refresh.pack(side="left", padx=(0, 8))
        self._accent_widgets.append(self.btn_history_refresh)
        
        self.btn_history_open_folder = ctk.CTkButton(history_toolbar, image=img_folder, text=" Open Folder", font=self.get_font(size=12, weight="bold"), fg_color=("#d1d5db", "#3f3f46"), hover_color=("#9ca3af", "#52525b"), text_color=("#111827", "#ffffff"), height=28, width=120, command=self.open_output_folder)
        self.btn_history_open_folder.pack(side="left")
        
        # CTkFrame biasa (bukan Scrollable) karena sudah di dalam frame_scraper
        # yang scrollable; nested scrollable frame bisa konflik scroll/mousewheel.
        self.history_scroll = ctk.CTkFrame(self.frame_scraper, corner_radius=0, fg_color="transparent")
        self.history_scroll.grid(row=5, column=0, padx=30, pady=(0, 30), sticky="nsew")
        self.history_scroll.grid_columnconfigure(0, weight=1)
        
        self.history_empty_label = ctk.CTkLabel(self.history_scroll, text="No scraping results yet. Run a scrape above to see it here.", font=self.get_font(size=12), text_color=("#6b7280", "#9ca3af"))
        
        self.history_rows = []

        # Frame Logs
        self.frame_logs = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.frame_logs.grid_columnconfigure(0, weight=1)
        self.frame_logs.grid_rowconfigure(2, weight=1)
        
        self.logs_title = ctk.CTkLabel(self.frame_logs, text="Debug", font=self.get_font(size=22, weight="bold"), text_color=("#111827", "#ffffff"))
        self.logs_title.grid(row=0, column=0, padx=30, pady=(30, 15), sticky="nw")
        
        # Toolbar Logs: Filter tag & Export
        logs_toolbar = ctk.CTkFrame(self.frame_logs, fg_color="transparent")
        logs_toolbar.grid(row=1, column=0, padx=30, pady=(0, 10), sticky="ew")
        
        lbl_filter = ctk.CTkLabel(logs_toolbar, text="Filter:", font=self.get_font(size=12, weight="bold"), text_color=("#111827", "#ffffff"))
        lbl_filter.pack(side="left", padx=(0, 8))
        
        self.opt_log_filter = ctk.CTkOptionMenu(
            logs_toolbar, values=["All", "App_Sync", "Freebie", "Gmaps", "System"], 
            font=self.get_font(size=12), fg_color=("#ffffff", "#131315"), button_color=self.accent_color, button_hover_color=self.accent_hover_color,
            text_color=("#111827", "#ffffff"), dropdown_fg_color=("#ffffff", "#18181b"), dropdown_hover_color=self.accent_color, 
            dropdown_text_color=("#111827", "#ffffff"), dropdown_font=self.get_font(size=12), height=28, width=130,
            command=lambda _=None: self.render_logs()
        )
        self.opt_log_filter.set("All")
        self.opt_log_filter.pack(side="left", padx=(0, 15))
        self._accent_widgets.append(self.opt_log_filter)
        
        img_export = ctk.CTkImage(light_image=ic_fac_light.asPil('download'), dark_image=ic_fac_dark.asPil('download'), size=(14, 14))
        self.btn_log_export = ctk.CTkButton(logs_toolbar, image=img_export, text=" Export Log", font=self.get_font(size=12, weight="bold"), fg_color=self.accent_color, hover_color=self.accent_hover_color, height=28, width=110, command=self.export_logs)
        self.btn_log_export.pack(side="left")
        self._accent_widgets.append(self.btn_log_export)
        
        # Textbox log
        self.console_text = ctk.CTkTextbox(self.frame_logs, state="disabled", font=self.get_font(size=12), fg_color=("#ffffff", "#131315"), text_color=("#1f2937", "#d4d4d8"), border_color=("#d1d5db", "#252529"), border_width=1, corner_radius=8)
        self.console_text.grid(row=2, column=0, padx=30, pady=(0, 30), sticky="nsew")

        # Frame Credits
        self.frame_credits = ctk.CTkScrollableFrame(self, corner_radius=0, fg_color="transparent")
        self.frame_credits.grid_columnconfigure(0, weight=1)
        
        self.credits_title = ctk.CTkLabel(self.frame_credits, text="Credits & Info", font=self.get_font(size=22, weight="bold"), text_color=("#111827", "#ffffff"))
        self.credits_title.grid(row=0, column=0, padx=30, pady=(30, 15), sticky="nw")
        
        lbl_intro = ctk.CTkLabel(self.frame_credits, text="HORIZON Dashboard was brought to life by:", font=self.get_font(size=14), text_color=("#4b5563", "#9ca3af"), justify="left", anchor="nw")
        lbl_intro.grid(row=1, column=0, padx=30, pady=(10, 5), sticky="nw")
        
        img_crown = ctk.CTkImage(light_image=ic_fac_light.asPil('crown'), dark_image=ic_fac_dark.asPil('crown'), size=(16, 16))
        lbl_creator = ctk.CTkLabel(self.frame_credits, image=img_crown, compound="left", text=" @JustSEMI (oprexz)", font=self.get_font(size=14, weight="bold"), text_color=("#111827", "#ffffff"))
        lbl_creator.grid(row=2, column=0, padx=30, pady=2, sticky="nw")
        
        img_smile = ctk.CTkImage(light_image=ic_fac_light.asPil('smile'), dark_image=ic_fac_dark.asPil('smile'), size=(16, 16))
        desc_text = (
            "Thank you for using this idle Project Dashboard\n"
            "Hopefully there are no bugs left... if there are,\n"
            "just consider them as features"
        )
        lbl_desc = ctk.CTkLabel(self.frame_credits, text=desc_text, font=self.get_font(size=14), text_color=("#4b5563", "#9ca3af"), justify="left", anchor="nw")
        lbl_desc.grid(row=4, column=0, padx=30, pady=(25, 5), sticky="nw")
        
        img_heart = ctk.CTkImage(light_image=ic_fac_light.asPil('heart'), dark_image=ic_fac_dark.asPil('heart'), size=(16, 16))
        lbl_thanks_title = ctk.CTkLabel(self.frame_credits, image=img_heart, compound="left", text=" Special thanks to:", font=self.get_font(size=14, weight="bold"), text_color=("#111827", "#ffffff"))
        lbl_thanks_title.grid(row=5, column=0, padx=30, pady=(20, 5), sticky="nw")
        
        thanks_text = "- Gemini for UI Layouting & Code Assistant\n- WindUI (Footagesus) for UI reference\n- Lucide Icons (lucide.dev)\n- JetBrains for Fonts"
        lbl_thanks_list = ctk.CTkLabel(self.frame_credits, text=thanks_text, font=self.get_font(size=14), text_color=("#4b5563", "#9ca3af"), justify="left", anchor="nw")
        lbl_thanks_list.grid(row=6, column=0, padx=55, pady=0, sticky="nw")
        
        img_package = ctk.CTkImage(light_image=ic_fac_light.asPil('package'), dark_image=ic_fac_dark.asPil('package'), size=(16, 16))
        lbl_libs_title = ctk.CTkLabel(self.frame_credits, image=img_package, compound="left", text=" Libraries Used:", font=self.get_font(size=14, weight="bold"), text_color=("#111827", "#ffffff"))
        lbl_libs_title.grid(row=7, column=0, padx=30, pady=(20, 5), sticky="nw")
        
        libs_text = "- CustomTkinter (GUI Framework)\n- Playwright, pandas, openpyxl (Data Scraper)\n- pypresence (Discord RPC)\n- pystray, Pillow, pywin32 (System Tray & OS)\n- psutil, requests, iconipy (Utils)"
        lbl_libs_list = ctk.CTkLabel(self.frame_credits, text=libs_text, font=self.get_font(size=14), text_color=("#4b5563", "#9ca3af"), justify="left", anchor="nw")
        lbl_libs_list.grid(row=8, column=0, padx=55, pady=(0, 20), sticky="nw")

        # Frame Settings Global
        self.frame_settings = ctk.CTkScrollableFrame(self, corner_radius=0, fg_color="transparent")
        self.frame_settings.grid_columnconfigure(0, weight=1)
        
        self.settings_title = ctk.CTkLabel(self.frame_settings, text="App Settings", font=self.get_font(size=22, weight="bold"), text_color=("#111827", "#ffffff"))
        self.settings_title.grid(row=0, column=0, padx=40, pady=(30, 20), sticky="w")
        
        def create_setting_card(parent, row, title, desc, values, default_val, command):
            card = ctk.CTkFrame(parent, fg_color=("#ffffff", "#131315"), corner_radius=12)
            card.grid(row=row, column=0, padx=40, pady=6, sticky="ew")
            card.grid_columnconfigure(0, weight=1)
            
            lbl_title = ctk.CTkLabel(card, text=title, font=self.get_font(size=14, weight="bold"), text_color=("#111827", "#ffffff"), anchor="w")
            lbl_title.grid(row=0, column=0, padx=(20, 10), pady=(15, 0), sticky="w")
            
            lbl_desc = ctk.CTkLabel(card, text=desc, font=self.get_font(size=11), text_color=("#6b7280", "gray50"), justify="left", anchor="w")
            lbl_desc.grid(row=1, column=0, padx=(20, 10), pady=(2, 15), sticky="w")
            
            opt_menu = ctk.CTkOptionMenu(
                card, values=values, command=command, 
                font=self.get_font(size=12), fg_color=("#f3f4f6", "#1c1c1e"), button_color=self.accent_color, button_hover_color=self.accent_hover_color,
                dropdown_fg_color=("#ffffff", "#18181b"), dropdown_hover_color=self.accent_color, dropdown_text_color=("#111827", "#ffffff"), dropdown_font=self.get_font(size=12)
            )
            opt_menu.set(default_val)
            opt_menu.grid(row=0, column=1, rowspan=2, padx=20, pady=15, sticky="e")
            self._accent_widgets.append(opt_menu)
            
            def update_wrap(event):
                new_wrap = event.width - 250
                if new_wrap > 50:
                    lbl_desc.configure(wraplength=new_wrap)
                    
            card.bind("<Configure>", update_wrap)
            return opt_menu
            
        # 1. Appearance Mode
        self.option_appearance = create_setting_card(
            self.frame_settings, 1, "Appearance Mode", "Change the theme of the application.",
            ["Dark", "Light", "System"], config.get("dashboard", {}).get("appearance_mode", "Dark"), self.change_appearance_mode
        )
        
        # 2. UI Scaling
        self.option_scaling = create_setting_card(
            self.frame_settings, 2, "UI Scaling", "Adjust the overall size of UI elements.",
            ["80%", "90%", "100%", "110%", "120%"], config.get("dashboard", {}).get("ui_scaling", "100%"), self.change_scaling_event
        )
        
        # 3. Close Window Action
        self.option_onclose = create_setting_card(
            self.frame_settings, 3, "Close Window Action", "What happens when you click the 'X' button.",
            ["Minimize to Tray", "Quit Application"], config.get("dashboard", {}).get("on_close_action", "Minimize to Tray"), self.change_on_close_action
        )
        
        # 4. Run on Startup
        self.option_startup = create_setting_card(
            self.frame_settings, 4, "Run at Startup", "Automatically launch HORIZON when Windows starts.",
            ["Disabled", "Enabled"], config.get("dashboard", {}).get("run_on_startup", "Disabled"), self.change_startup_event
        )

        # 6. Accent Color Theme
        self.option_theme = create_setting_card(
            self.frame_settings, 6, "Accent Color Theme", "Change the main highlight color of the UI.",
            ["Blue", "Green", "Dark-Blue"], config.get("dashboard", {}).get("color_theme", "blue").capitalize(), self.change_color_theme_event
        )
        
        # 7. Toast Notifications
        self.option_notif = create_setting_card(
            self.frame_settings, 7, "Toast Notifications", "Show popup messages at the bottom of the screen.",
            ["Enabled", "Disabled"], config.get("dashboard", {}).get("notifications", "Enabled"), self.change_notifications_event
        )

        # Tampilkan default tab
        self.select_tab("Dashboard")
        
        # Mulai siklus update overview dan system monitor
        self.update_overview()
        self.update_sys_monitor()

    def update_sys_monitor(self):
        # Lewati sampling & update widget saat tersembunyi di tray (hemat CPU idle)
        if self.state() == 'withdrawn':
            self.after(1000, self.update_sys_monitor)
            return
        try:
            stats = self.sys_monitor.get_stats()
            
            # CPU
            self.lbl_cpu_text.configure(text=f"CPU: {stats['cpu_percent']:.1f}%")
            self.pb_cpu.set(stats['cpu_percent'] / 100.0)
            
            # RAM
            self.lbl_ram_text.configure(text=f"RAM: {stats['ram_used_gb']:.2f} GB / {stats['ram_total_gb']:.2f} GB ({stats['ram_percent']}%)")
            self.pb_ram.set(stats['ram_percent'] / 100.0)
            
            # Network
            dl_fmt = SystemMonitor.format_speed(stats['dl_bytes'])
            ul_fmt = SystemMonitor.format_speed(stats['ul_bytes'])
            self.lbl_net_text.configure(text=f"Net: ↓ {dl_fmt}  |  ↑ {ul_fmt}")
        except Exception:
            pass
            
        self.after(1000, self.update_sys_monitor)

    def show_toast(self, message, duration=3000, error=False):
        if config.get("dashboard", {}).get("notifications", "Enabled") == "Disabled":
            return
        
        toast_color = ("#ef4444", "#dc2626") if error else ("#10b981", "#059669")
        toast = ctk.CTkFrame(self, fg_color=toast_color, corner_radius=8)
        lbl = ctk.CTkLabel(toast, text=message, font=self.get_font(size=12, weight="bold"), text_color="#ffffff")
        lbl.pack(padx=20, pady=10)
        
        # Munculkan instan di tengah bawah
        toast.place(relx=0.5, rely=0.92, anchor="center")
        
        # Hancurkan otomatis setelah waktu habis
        self.after(duration, toast.destroy)

    def start_scraper(self):
        if self.scraper_process and self.scraper_process.poll() is None:
            self.show_toast("Scraper is already running!", 3000)
            return
            
        query = self.entry_query.get().strip()
        location = self.entry_loc.get().strip()
        max_results = self.entry_max.get().strip()
        min_rating = self.entry_min_rating.get().strip()
        
        if not query:
            return
        
        if min_rating:
            try:
                float(min_rating.replace(',', '.'))
            except ValueError:
                self.show_toast("Min Rating tidak valid.", 3000, error=True)
                return
        
        selected_fields = [var.get() for var in self.field_vars.values() if var.get()]
        if not selected_fields:
            self.show_toast("Please select at least one field to export", 3000, error=True)
            return
            
        full_query = f"{query} di {location}" if location else query
            
        if not max_results.isdigit():
            max_results = "20"
            
        export_format = getattr(self, 'opt_format', None)
        fmt_val = export_format.get() if export_format else "Excel"
        fmt_ext = ".xlsx"
        export_type = "xlsx"
        if "Word" in fmt_val:
            fmt_ext = ".docx"
            export_type = "docx"
        elif "PDF" in fmt_val:
            fmt_ext = ".pdf"
            export_type = "pdf"
        elif "HTML" in fmt_val:
            fmt_ext = ".html"
            export_type = "html"
            
        output_dir = os.path.join(HORIZON_DIR, "output")
        os.makedirs(output_dir, exist_ok=True)
        safe_query = "".join([c if c.isalnum() else "_" for c in full_query])
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(output_dir, f"{safe_query}_{timestamp}{fmt_ext}")
        
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main", "scraper_service.py")
        
        self.show_toast(f"Scraping started for '{full_query}'...", 3000)
        
        cmd = [sys.executable, "-u", script_path, "--query", full_query, "--max", max_results, "--output", output_path, "--format", export_type]
        if selected_fields:
            cmd.extend(["--fields"] + selected_fields)
        if min_rating:
            cmd.extend(["--min-rating", min_rating.replace(',', '.')])
        
        self.scraper_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        
        self.scraper_thread = threading.Thread(target=self._read_scraper_logs, args=(full_query, output_path), daemon=True)
        self.scraper_thread.start()

    def _read_scraper_logs(self, query, output_path):
        if not self.scraper_process:
            return
        
        for line in iter(self.scraper_process.stdout.readline, ''):
            if not line:
                break
            line = line.strip()
            if line:
                logger.info(f"[GMAPS] {line}")
                
        exit_code = self.scraper_process.wait()
        success = exit_code == 0 and os.path.exists(output_path)
        
        def finish_log():
            if success:
                self.show_toast("Scraping finished!", 4000)
            else:
                self.show_toast("Scraping failed. Check Logs for details.", 4000)
            self.record_history(query, output_path, success)
        self.after(0, finish_log)

    def record_history(self, query, output_path, success):
        # Simpan riwayat hasil scraping ke file JSON di folder output
        history_file = os.path.join(HORIZON_DIR, "output", "history.json")
        entries = []
        if os.path.exists(history_file):
            try:
                with open(history_file, 'r', encoding='utf-8') as f:
                    entries = json.load(f)
            except Exception:
                entries = []
        
        entries.insert(0, {
            "query": query,
            "path": output_path,
            "filename": os.path.basename(output_path),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "success": success
        })
        entries = entries[:100] # Batasi maksimal 100 riwayat terakhir
        
        try:
            with open(history_file, 'w', encoding='utf-8') as f:
                json.dump(entries, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save history: {e}")
            
        self.refresh_history()

    def load_history(self):
        history_file = os.path.join(HORIZON_DIR, "output", "history.json")
        if not os.path.exists(history_file):
            return []
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []

    def refresh_history(self):
        # Bersihkan baris lama
        for row in self.history_rows:
            row.destroy()
        self.history_rows = []
        self.history_empty_label.pack_forget()
        
        entries = self.load_history()
        
        if not entries:
            self.history_empty_label.pack(padx=15, pady=20)
            return
        
        for entry in entries:
            row = ctk.CTkFrame(self.history_scroll, fg_color=("#e5e7eb", "#252529"), corner_radius=8)
            row.pack(fill="x", pady=4)
            row.grid_columnconfigure(0, weight=1)
            
            exists = os.path.exists(entry.get("path", ""))
            status_color = "#22c55e" if entry.get("success") and exists else "#ef4444"
            
            text_frame = ctk.CTkFrame(row, fg_color="transparent")
            text_frame.grid(row=0, column=0, padx=15, pady=10, sticky="w")
            
            lbl_query = ctk.CTkLabel(text_frame, text=entry.get("query", "Unknown"), font=self.get_font(size=13, weight="bold"), text_color=("#111827", "#ffffff"), anchor="w")
            lbl_query.pack(anchor="w")
            
            status_text = "Completed" if (entry.get("success") and exists) else ("File missing" if entry.get("success") else "Failed")
            info_text = f"{entry.get('timestamp', '')} • {entry.get('filename', '')} • "
            lbl_info = ctk.CTkLabel(text_frame, text=info_text, font=self.get_font(size=11), text_color=("#4b5563", "#9ca3af"), anchor="w")
            lbl_info.pack(anchor="w")
            
            lbl_status = ctk.CTkLabel(row, text=status_text, font=self.get_font(size=11, weight="bold"), text_color=status_color)
            lbl_status.grid(row=0, column=1, padx=(0, 10), pady=10, sticky="e")
            
            btn_open = ctk.CTkButton(
                row, text="Open", font=self.get_font(size=11, weight="bold"), width=70, height=26,
                fg_color=self.accent_color, hover_color=self.accent_hover_color,
                state="normal" if exists else "disabled",
                command=lambda p=entry.get("path", ""): self.open_file(p)
            )
            btn_open.grid(row=0, column=2, padx=(0, 15), pady=10, sticky="e")
            self._accent_widgets.append(btn_open)
            
            self.history_rows.append(row)

    def open_file(self, path):
        try:
            os.startfile(path)
        except Exception as e:
            logger.error(f"Failed to open file {path}: {e}")
            self.show_toast("Could not open file.", 3000)

    def open_output_folder(self):
        output_dir = os.path.join(HORIZON_DIR, "output")
        os.makedirs(output_dir, exist_ok=True)
        try:
            os.startfile(output_dir)
        except Exception as e:
            logger.error(f"Failed to open output folder: {e}")

    def update_overview(self):
        # Lewati update label saat tersembunyi di tray (uptime tetap dihitung
        # dari app_start_time saat window ditampilkan lagi)
        if self.state() == 'withdrawn':
            self.after(1000, self.update_overview)
            return
        active_count = sum(1 for w in self.workers if w.is_running)
        total_count = len(self.workers)
        
        img_current = self.img_status_green if active_count > 0 else self.img_status_red
        self.lbl_status.configure(image=img_current, text=f" Services: {active_count} / {total_count} Active")
        
        elapsed = int(time.time() - self.app_start_time)
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        self.lbl_uptime.configure(text=f" Uptime: {uptime_str}")
        
        self.after(1000, self.update_overview)

    def select_tab(self, tab_name):
        # Update warna tombol sidebar agar terlihat aktif highlight abu-abu gelap
        for name, btn in self.sidebar_buttons.items():
            btn.configure(fg_color=("#e5e7eb", "#2a2a2d") if name == tab_name else "transparent")
        
        # Tampilkan frame yang sesuai
        self.frame_dash.grid_forget()
        self.frame_discord.grid_forget()
        self.frame_scraper.grid_forget()
        self.frame_logs.grid_forget()
        self.frame_settings.grid_forget()
        self.frame_credits.grid_forget()
        
        if tab_name == "Dashboard":
            self.frame_dash.grid(row=0, column=1, sticky="nsew")
        elif tab_name == "Discord":
            self.frame_discord.grid(row=0, column=1, sticky="nsew")
        elif tab_name == "Scraping":
            self.frame_scraper.grid(row=0, column=1, sticky="nsew")
            self.refresh_history()
        elif tab_name == "Logs":
            self.frame_logs.grid(row=0, column=1, sticky="nsew")
        elif tab_name == "Settings":
            self.frame_settings.grid(row=0, column=1, sticky="nsew")
        elif tab_name == "Credits":
            self.frame_credits.grid(row=0, column=1, sticky="nsew")
    def _create_input_row(self, parent, label_text, default_value, pady=5):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=15, pady=pady)
        lbl = ctk.CTkLabel(row, text=label_text, font=self.get_font(size=12, weight="bold"), text_color=("#111827", "#ffffff"), width=140, anchor="w")
        lbl.pack(side="left")
        entry = ctk.CTkEntry(row, font=self.get_font(size=12), fg_color=("#ffffff", "#131315"), border_color=("#9ca3af", "#3f3f46"), text_color=("#111827", "#ffffff"), height=28)
        entry.pack(side="left", fill="x", expand=True, padx=(5, 0))
        entry.insert(0, default_value)
        return entry

    def _create_platform_checklist(self, parent, selected_csv):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=15, pady=(2, 8))
        lbl = ctk.CTkLabel(row, text="Target Platforms", font=self.get_font(size=12, weight="bold"), text_color=("#111827", "#ffffff"), width=140, anchor="nw")
        lbl.pack(side="left", anchor="n")
        checklist = PlatformChecklist(row, selected_csv=selected_csv, accent_color=self.accent_color, get_font=self.get_font)
        checklist.pack(side="left", fill="x", expand=True, padx=(5, 0))
        for chk in checklist.winfo_children():
            self._accent_widgets.append(chk)
        return checklist

    def _get_field_validator(self, key):
        # Mengembalikan fungsi validasi ringan untuk field konfigurasi kritikal.
        if key == "discord_webhook":
            return lambda v: v.strip() == "" or v.strip().startswith("https://discord.com/api/webhooks/")
        elif key == "discord_client_id":
            return lambda v: v.strip() == "" or v.strip().isdigit()
        elif key == "check_interval_mins":
            return lambda v: v.strip().isdigit() and int(v.strip()) > 0
        elif key == "target_platforms":
            return lambda v: len([p.strip() for p in v.split(",") if p.strip()]) > 0
        return None

    def change_appearance_mode(self, new_appearance_mode: str):
        if "dashboard" not in config:
            config["dashboard"] = {}
        config["dashboard"]["appearance_mode"] = new_appearance_mode
        ConfigManager.save(config)
        ctk.set_appearance_mode(new_appearance_mode)

    def change_scaling_event(self, new_scaling: str):
        if "dashboard" not in config:
            config["dashboard"] = {}
        config["dashboard"]["ui_scaling"] = new_scaling
        ConfigManager.save(config)
        new_scaling_float = int(new_scaling.replace("%", "")) / 100
        ctk.set_widget_scaling(new_scaling_float)

    def change_on_close_action(self, new_action: str):
        if "dashboard" not in config:
            config["dashboard"] = {}
        config["dashboard"]["on_close_action"] = new_action
        ConfigManager.save(config)

    def change_animation_event(self, new_state: str):
        if "dashboard" not in config:
            config["dashboard"] = {}
        config["dashboard"]["ui_animations"] = new_state
        ConfigManager.save(config)

    def change_startup_event(self, new_state: str):
        if "dashboard" not in config:
            config["dashboard"] = {}
        config["dashboard"]["run_on_startup"] = new_state
        ConfigManager.save(config)
        self.apply_startup_registry(new_state)

    def change_color_theme_event(self, new_theme: str):
        if "dashboard" not in config:
            config["dashboard"] = {}
        config["dashboard"]["color_theme"] = new_theme.lower()
        ConfigManager.save(config)
        
        saved_theme = new_theme.lower()
        if saved_theme == "green":
            self.accent_color = "#10b981"
            self.accent_hover_color = "#059669"
        elif saved_theme == "dark-blue":
            self.accent_color = "#3730a3"
            self.accent_hover_color = "#312e81"
        else: # blue
            self.accent_color = "#3b82f6"
            self.accent_hover_color = "#2563eb"
            
        for w in self._accent_widgets:
            if isinstance(w, ctk.CTkButton):
                w.configure(fg_color=self.accent_color, hover_color=self.accent_hover_color)
            elif isinstance(w, ctk.CTkOptionMenu):
                w.configure(button_color=self.accent_color, button_hover_color=self.accent_hover_color, dropdown_hover_color=self.accent_color)
            elif isinstance(w, ctk.CTkSwitch):
                w.configure(progress_color=self.accent_color)
            elif isinstance(w, ctk.CTkCheckBox):
                w.configure(fg_color=self.accent_color, hover_color=self.accent_color)

    def change_notifications_event(self, new_state: str):
        if "dashboard" not in config:
            config["dashboard"] = {}
        config["dashboard"]["notifications"] = new_state
        ConfigManager.save(config)

    def change_hw_accel_event(self, new_state: str):
        if "dashboard" not in config:
            config["dashboard"] = {}
        config["dashboard"]["hw_accel"] = new_state
        ConfigManager.save(config)
        
    def apply_startup_registry(self, state: str):
        key = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "HorizonDashboard"
        
        # Menggunakan pythonw agar berjalan secara Background/Windowless
        python_exe = sys.executable.replace("python.exe", "pythonw.exe")
        script_path = os.path.join(HORIZON_DIR, "horizon.pyw")
        command = f'"{python_exe}" "{script_path}"'
        
        try:
            reg_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE | winreg.KEY_READ)
            if state == "Enabled":
                winreg.SetValueEx(reg_key, app_name, 0, winreg.REG_SZ, command)
                logger.info("Successfully registered application to Windows Startup.")
            else:
                try:
                    winreg.DeleteValue(reg_key, app_name)
                    logger.info("Successfully removed application from Windows Startup.")
                except FileNotFoundError:
                    pass
            winreg.CloseKey(reg_key)
        except Exception as e:
            logger.error(f"Failed to set startup registry: {e}")

    def on_close_window(self):
        action = config.get("dashboard", {}).get("on_close_action", "Minimize to Tray")
        if action == "Quit Application":
            self.shutdown()
        else:
            self.hide_to_tray()

    def poll_logs(self):
        # Polling logs dari queue tanpa me-lagging UI thread.
        # Saat window disembunyikan ke tray, tidak perlu render (hemat CPU).
        if self.state() == 'withdrawn':
            self.after(200, self.poll_logs)
            return
        if gui_handler.counter != getattr(self, '_last_log_counter', -1):
            self._last_log_counter = gui_handler.counter
            self.render_logs()
        self.after(200, self.poll_logs)

    def render_logs(self):
        # Terapkan filter tag terhadap isi log_queue lalu render ke textbox
        filter_val = self.opt_log_filter.get() if hasattr(self, 'opt_log_filter') else "All"
        
        tag_map = {
            "App_Sync": "[APP_SYNC]",
            "Freebie": "[FREEBIE]",
            "Gmaps": "[GMAPS]",
        }
        
        if filter_val == "All":
            lines = list(log_queue)
        elif filter_val == "System":
            known_tags = tuple(tag_map.values())
            lines = [l for l in log_queue if not any(t in l for t in known_tags)]
        else:
            needle = tag_map.get(filter_val, "")
            lines = [l for l in log_queue if needle in l]
        
        log_str = "\n".join(lines)
        self.console_text.configure(state="normal")
        self.console_text.delete("1.0", ctk.END)
        self.console_text.insert(ctk.END, log_str)
        self.console_text.see(ctk.END) # auto scroll to bottom
        self.console_text.configure(state="disabled")

    def export_logs(self):
        export_dir = os.path.join(HORIZON_DIR, "log")
        os.makedirs(export_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        export_path = os.path.join(export_dir, f"export_{timestamp}.log")
        
        try:
            with open(export_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(log_queue))
            self.show_toast(f"Log exported to log/{os.path.basename(export_path)}", 3500)
            logger.info(f"Log exported to {export_path}")
        except Exception as e:
            logger.error(f"Failed to export log: {e}")
            self.show_toast("Failed to export log.", 3000)

    def create_tray_image(self):
        # Gunakan Lucide icon untuk tray
        ic_fac = get_icon_factory(64, self.accent_color)
        return ic_fac.asPil('circle')

    def _make_tray_toggle(self, worker):
        # Start/stop worker langsung dari menu tray tanpa buka window
        def toggle(icon, item):
            self.after(0, worker.stop if worker.is_running else worker.start)
        return toggle

    def init_tray(self):
        image = self.create_tray_image()
        menu_items = [pystray.MenuItem("Show Dashboard", self.on_tray_show, default=True), pystray.Menu.SEPARATOR]
        for worker in self.workers:
            menu_items.append(
                pystray.MenuItem(
                    lambda item, w=worker: f"{'Stop' if w.is_running else 'Start'} {w.name}",
                    self._make_tray_toggle(worker),
                    checked=lambda item, w=worker: w.is_running
                )
            )
        menu_items.append(pystray.Menu.SEPARATOR)
        menu_items.append(pystray.MenuItem("Exit", self.on_tray_exit))
        menu = pystray.Menu(*menu_items)
        self.tray_icon = pystray.Icon("horizon_dashboard", image, "HORIZON Dashboard", menu)

        # Tray loop memblokir, jadi harus di-background
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def on_tray_show(self):
        # Minta main thread buat memunculkan window secara native
        self.after(0, self.deiconify)

    def on_tray_exit(self):
        self.after(0, self.shutdown)

    def hide_to_tray(self):
        logger.info("Application hidden to System Tray.")
        self.withdraw() # 

    def shutdown(self):
        logger.info("Stopping entire system safely...")
        self.withdraw()
        
        # Render pesan terakhir sejenak sebelum program mati
        self.console_text.configure(state="normal")
        self.console_text.insert(ctk.END, "\nFinished closing all services...")
        self.console_text.see(ctk.END)
        self.console_text.configure(state="disabled")
        self.update_idletasks()
        
        for worker in self.workers:
            worker.stop()
            
        if self.tray_icon:
            self.tray_icon.stop()
            
        logger.info("Done.")
        time.sleep(0.3)
        self.quit()
        self.destroy()

if __name__ == "__main__":
    app = DashboardApp()
    logger.info("Dashboard successfully loaded. Ready to use!")
    app.mainloop()
