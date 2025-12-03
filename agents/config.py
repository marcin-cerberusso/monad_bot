"""
⚙️ STRATEGY CONFIG - Parametry strategii
"""
import os
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# === LOGGING SETUP ===
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def setup_logging(name: str = "monad_bot") -> logging.Logger:
    """
    Configure and return a logger with console and file handlers.
    
    Args:
        name: Logger name (default: "monad_bot")
        
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    
    # Prevent adding handlers multiple times
    if logger.handlers:
        return logger
    
    # Formatter
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s'
    )
    
    # Console handler (INFO and above)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # Main file handler with rotation (10MB max, 5 backups)
    main_log_path = LOG_DIR / f"{name}.log"
    file_handler = RotatingFileHandler(
        main_log_path,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # Error file handler (ERROR and above only)
    error_log_path = LOG_DIR / f"{name}_errors.log"
    error_handler = RotatingFileHandler(
        error_log_path,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)
    
    return logger


# === WHALE DETECTION ===
MIN_WHALE_BUY_MON = float(os.getenv("MIN_WHALE_BUY_MON", "200"))  # Only follow whales > 200 MON

# === POSITION SIZING ===
FOLLOW_AMOUNT_MON = float(os.getenv("FOLLOW_AMOUNT_MON", "8"))   # 8 MON per trade
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))              # Max 5 open positions

# === TAKE PROFIT ===
TP1_PERCENT = 50    # Take 30% of position at +50%
TP1_SELL_PERCENT = 30

TP2_PERCENT = 100   # Take 40% more at +100% (2x)
TP2_SELL_PERCENT = 40

# === STOP LOSS ===
STOP_LOSS_PERCENT = -15   # Cut losses at -15%

# === TRAILING STOP ===
TRAILING_ACTIVATE = 60    # Activate trailing at +60%
TRAILING_STOP = 20        # Trail by 20% from ATH

# === RISK FILTERS ===
MAX_TAX_PERCENT = 15      # Max acceptable tax
MIN_LIQUIDITY_USD = 2000  # Min liquidity
MAX_FOMO_PUMP_1H = 150    # Max +150% in 1h (skip FOMO)

# === AI CONFIDENCE ===
MIN_AI_CONFIDENCE = 60    # Min AI confidence to trade

print(f"Config loaded: {FOLLOW_AMOUNT_MON} MON/trade, whale>{MIN_WHALE_BUY_MON} MON")

# === NOTIFICATIONS ===
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

