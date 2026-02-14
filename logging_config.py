#!/usr/bin/env python3
"""
HighTrade Unified Logging Configuration
Centralized logging setup for all components with rotation and formatting
"""

import logging
import logging.handlers
from pathlib import Path
from datetime import datetime

# Unified log directory
LOG_DIR = Path.home() / 'trading_data' / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Log levels for different components
LOG_LEVELS = {
    'orchestrator': logging.INFO,
    'slack_bot': logging.INFO,
    'mcp_server': logging.INFO,
    'monitoring': logging.INFO,
    'news': logging.WARNING,  # Less verbose for news modules
    'alerts': logging.INFO,
    'paper_trading': logging.INFO,
    'broker': logging.INFO,
    'default': logging.INFO
}

# Unified formatter
FORMATTER = logging.Formatter(
    '%(asctime)s | %(name)-20s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def setup_logger(name, log_file=None, level=None):
    """
    Setup a logger with unified configuration

    Args:
        name: Logger name (usually __name__)
        log_file: Optional specific log file (defaults to component name)
        level: Optional log level (defaults to LOG_LEVELS setting)

    Returns:
        configured logger instance
    """
    logger = logging.getLogger(name)

    # Prevent duplicate handlers
    if logger.handlers:
        return logger

    # Determine log level
    component = name.split('.')[0]
    if level is None:
        level = LOG_LEVELS.get(component, LOG_LEVELS['default'])

    logger.setLevel(level)

    # Console handler (for immediate feedback)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(FORMATTER)
    logger.addHandler(console_handler)

    # File handler with rotation (10MB max, keep 5 backups)
    if log_file is None:
        log_file = f"{component}.log"

    log_path = LOG_DIR / log_file
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(FORMATTER)
    logger.addHandler(file_handler)

    # Master log - everything goes here too (for debugging)
    master_log = LOG_DIR / 'hightrade_master.log'
    master_handler = logging.handlers.RotatingFileHandler(
        master_log,
        maxBytes=50 * 1024 * 1024,  # 50MB
        backupCount=3,
        encoding='utf-8'
    )
    master_handler.setLevel(logging.DEBUG)
    master_handler.setFormatter(FORMATTER)
    logger.addHandler(master_handler)

    return logger


def setup_error_alerts_logger():
    """
    Special logger for critical errors that need immediate attention
    Writes to a separate error.log for easy monitoring
    """
    error_logger = logging.getLogger('error_alerts')

    if error_logger.handlers:
        return error_logger

    error_logger.setLevel(logging.ERROR)

    error_log = LOG_DIR / 'errors.log'
    error_handler = logging.handlers.RotatingFileHandler(
        error_log,
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=10,
        encoding='utf-8'
    )

    error_formatter = logging.Formatter(
        '%(asctime)s | %(levelname)s | %(name)s | %(funcName)s:%(lineno)d\n'
        '%(message)s\n' + '-' * 80,
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    error_handler.setFormatter(error_formatter)
    error_logger.addHandler(error_handler)

    return error_logger


def log_startup(component_name, version=None):
    """Log system startup with environment info"""
    logger = logging.getLogger(component_name)
    logger.info("=" * 70)
    logger.info(f"{component_name} Starting")
    if version:
        logger.info(f"Version: {version}")
    logger.info(f"Started at: {datetime.now().isoformat()}")
    logger.info(f"Log directory: {LOG_DIR}")
    logger.info("=" * 70)


def log_shutdown(component_name):
    """Log clean shutdown"""
    logger = logging.getLogger(component_name)
    logger.info("=" * 70)
    logger.info(f"{component_name} Shutting Down")
    logger.info(f"Stopped at: {datetime.now().isoformat()}")
    logger.info("=" * 70)


# Example usage patterns for reference
if __name__ == '__main__':
    # Example 1: Basic logger
    logger = setup_logger(__name__)
    logger.info("This is a test message")
    logger.warning("This is a warning")
    logger.error("This is an error")

    # Example 2: Component-specific logger
    orchestrator_logger = setup_logger('orchestrator')
    log_startup('orchestrator', version='1.0.0')
    orchestrator_logger.info("Monitoring cycle starting")
    log_shutdown('orchestrator')

    # Example 3: Error alerts
    error_logger = setup_error_alerts_logger()
    try:
        raise ValueError("Test error for alert system")
    except Exception as e:
        error_logger.exception(f"Critical error occurred: {e}")

    print(f"\n✅ Logs written to: {LOG_DIR}")
    print(f"   - Component logs: {LOG_DIR}/<component>.log")
    print(f"   - Master log: {LOG_DIR}/hightrade_master.log")
    print(f"   - Error log: {LOG_DIR}/errors.log")
