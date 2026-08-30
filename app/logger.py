import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "runtime" / "logs"

MANAGER_LOG_MAX_BYTES = 2 * 1024 * 1024
MANAGER_LOG_BACKUPS = 3


def get_manager_logger() -> logging.Logger:
    logger = logging.getLogger("mcp_manager")
    if not logger.handlers:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        fh = RotatingFileHandler(
            LOG_DIR / "manager.log",
            maxBytes=MANAGER_LOG_MAX_BYTES,
            backupCount=MANAGER_LOG_BACKUPS,
        )
        fh.setFormatter(fmt)
        logger.addHandler(sh)
        logger.addHandler(fh)
        logger.setLevel(logging.INFO)
        # This logger carries its own stdout handler. In a runner the root
        # logger has one too (basicConfig in mcp_runner), so propagating would
        # print every manager-logger line into the runtime log twice — visible
        # since tool_loader started logging the content-folder autofill.
        logger.propagate = False
    return logger


def get_install_log_path(instance_id: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{instance_id}.install.log"


def get_runtime_log_path(instance_id: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{instance_id}.runtime.log"
