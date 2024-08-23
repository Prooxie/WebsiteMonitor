import time
import requests
from bs4 import BeautifulSoup
import logging
from config import Config
from notification import NotificationManager

logger = logging.getLogger(__name__)

class Monitor:
    def __init__(self):
        self.config = Config()
        self.notification_manager = NotificationManager(self.config)
        self.running = False
        logger.info("Monitor initialized")

    def run(self):
        logger.info("Monitoring started")
        while self.running:
            self.check_website()
            time.sleep(self.config.base_check_interval)

    def check_website(self):
        try:
            response = requests.get(self.config.url, proxies={"http": self.config.proxy, "https": self.config.proxy})
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                content = soup.select(self.config.base_div_selector)
                logger.info(f"Website checked successfully at {time.ctime()}")
                # Compare content and trigger notifications if needed
            else:
                logger.error(f"Error: {response.status_code} when checking website")
        except Exception as e:
            logger.error(f"Error checking website: {e}", exc_info=True)

    def start(self):
        self.running = True
        self.run()

    def stop(self):
        self.running = False
        logger.info("Monitoring stopped")
