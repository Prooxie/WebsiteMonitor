import smtplib
from email.mime.text import MIMEText
import logging
from config import Config

logger = logging.getLogger(__name__)

class NotificationManager:
    def __init__(self, config: Config):
        self.config = config

    def send_email(self, subject, message):
        try:
            msg = MIMEText(message)
            msg['Subject'] = subject
            msg['From'] = self.config.config["email_sender"]
            msg['To'] = self.config.config["email_receiver"]

            with smtplib.SMTP(self.config.config["smtp_server"], self.config.config["smtp_port"]) as server:
                server.starttls()
                server.login(self.config.config["email_sender"], self.config.decode_password(self.config.config["email_password"]))
                server.send_message(msg)
            logger.info("Email sent successfully")
        except Exception as e:
            logger.error(f"Failed to send email: {e}", exc_info=True)

    # Add SMS or other notification methods here
