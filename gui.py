import customtkinter as ctk
import logging
from config import Config
from monitor import Monitor

logger = logging.getLogger(__name__)

class WebsiteMonitorApp:
    def __init__(self, root, monitor):
        self.root = root
        self.monitor = monitor
        self.config = Config()
        self.setup_gui()
        logger.info("Initialized WebsiteMonitorApp")

    def setup_gui(self):
        self.root.title("Website Monitor")
        self.root.geometry("400x300")

        self.url_label = ctk.CTkLabel(self.root, text="Website URL:")
        self.url_label.pack(pady=10)

        self.url_entry = ctk.CTkEntry(self.root, width=300)
        self.url_entry.insert(0, self.config.config.get("url", ""))
        self.url_entry.pack(pady=10)

        self.check_button = ctk.CTkButton(self.root, text="Start Monitoring", command=self.start_monitoring)
        self.check_button.pack(pady=20)

    def start_monitoring(self):
        url = self.url_entry.get()
        self.config.config["url"] = url
        self.config.save_config()
        self.monitor.start()
        logger.info(f"Started monitoring {url}")

    def stop_monitoring(self):
        self.monitor.stop()
        logger.info("Stopped monitoring")

# The rest of your GUI-related code...
