import logging
from pathlib import Path

# Setup rotating log per process start inside `logs` dir
LOG_DIR = Path("logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
def _next_log_path():
    existing = [p.name for p in LOG_DIR.iterdir() if p.is_file() and p.name.startswith("log_") and p.suffix==".log"]
    nums = []
    for n in existing:
        try:
            nums.append(int(n.split("_")[1].split(".")[0]))
        except Exception:
            continue
    next_idx = max(nums)+1 if nums else 0
    return LOG_DIR / f"log_{next_idx}.log"

LOG_FILE = _next_log_path()
logging.basicConfig(level=logging.INFO, filename=str(LOG_FILE), filemode="a",
                    format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("app")

def log_and_print(msg: str, level: str = "info"):
    # safe wrapper: never log secrets. Use only for general messages.
    if level == "info":
        logger.info(msg)
        print(msg, flush=True)
    elif level == "error":
        logger.error(msg)
        print(msg, flush=True)
    else:
        logger.debug(msg)
        print(msg, flush=True)