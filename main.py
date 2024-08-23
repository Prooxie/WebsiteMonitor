import sys
import customtkinter as ctk
from gui import WebsiteMonitorApp
from monitor import Monitor
from logging_config import setup_logging
import logging

if __name__ == "__main__":
    setup_logging()
    logger = logging.getLogger(__name__)

    try:
        root = ctk.CTk()
        monitor = Monitor()
        app = WebsiteMonitorApp(root, monitor)
        root.mainloop()
    except Exception as e:
        logger.error(f"Application failed: {e}", exc_info=True)
