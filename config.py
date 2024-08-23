import json
import bcrypt
import logging

logger = logging.getLogger(__name__)

class Config:
    def __init__(self, config_file='config.json'):
        self.config_file = config_file
        self.load_config()

    def load_config(self):
        default_config = {
            "url": "https://example.com",
            "base_check_interval": 1800,
            "email_sender": "you@example.com",
            "email_password": self.hash_password("password"),
            "email_receiver": "receiver@example.com",
            "smtp_server": "smtp.example.com",
            "smtp_port": 587,
            "saved_content_file": "saved_page.html",
            "internet_check_host": "8.8.8.8",
            "base_div_selector": "div.isotope-wrapper.grid-wrapper.half-gutter",
            "proxy": "",
            "api_urls": []
        }

        try:
            with open(self.config_file, 'r') as file:
                self.config = json.load(file)
            logger.info("Configuration loaded successfully")
        except FileNotFoundError:
            self.config = default_config
            self.save_config()
            logger.warning(f"Config file not found. Using default settings and creating {self.config_file}")

    def save_config(self):
        with open(self.config_file, 'w') as file:
            json.dump(self.config, file, indent=4)
        logger.info(f"Configuration saved to {self.config_file}")

    def hash_password(self, password):
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

    def check_password(self, plain_password, hashed_password):
        return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

    # Add getters and setters for each config attribute here
