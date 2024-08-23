import logging
import logging.config
import os

def setup_logging(default_path='logging.json', default_level=logging.INFO):
    """Setup logging configuration"""
    if os.path.exists(default_path):
        import json
        with open(default_path, 'r') as file:
            config = json.load(file)
        logging.config.dictConfig(config)
    else:
        logging.basicConfig(level=default_level, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Example `logging.json` config (optional)
# {
#     "version": 1,
#     "disable_existing_loggers": false,
#     "formatters": {
#         "standard": {
#             "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
#         }
#     },
#     "handlers": {
#         "console": {
#             "class": "logging.StreamHandler",
#             "formatter": "standard",
#             "level": "INFO"
#         },
#         "file": {
#             "class": "logging.FileHandler",
#             "formatter": "standard",
#             "level": "INFO",
#             "filename": "app.log",
#             "mode": "a"
#         }
#     },
#     "root": {
#         "handlers": ["console", "file"],
#         "level": "INFO"
#     },
#     "loggers": {
#         "__main__": {
#             "level": "INFO",
#             "propagate": true
#         }
#     }
# }
