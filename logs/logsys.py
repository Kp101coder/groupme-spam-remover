from typing import List
import logging
from pathlib import Path

# Setup rotating log per process start inside `logs` dir
LOG_DIR = Path("logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
MAX_LOG_FILES = 50  # Maximum number of log files to retain

def _next_log_path():
    # Get all log files and their numeric indices
    log_files = []
    for p in LOG_DIR.iterdir():
        if p.is_file() and p.name.startswith("log_") and p.suffix == ".log":
            try:
                num = int(p.stem.split("_")[1])  # Extract number from log_X.log
                log_files.append((num, p))
            except (ValueError, IndexError):
                continue
    
    # Determine next index
    next_idx = max(num for num, _ in log_files) + 1 if log_files else 0
    
    # Delete logs more than MAX_LOG_FILES behind current index
    threshold = next_idx - MAX_LOG_FILES
    path : Path
    for num, path in log_files:
        if num < threshold:
            try:
                path.unlink()
            except Exception:
                pass  # Ignore deletion errors
    
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
        
def tail_lines(max_lines: int = 200) -> List[str]:
    if not LOG_FILE.exists():
        return []
    try:
        content = LOG_FILE.read_text().splitlines()
    except Exception:
        return []
    if len(content) <= max_lines:
        return content
    return content[-max_lines:]