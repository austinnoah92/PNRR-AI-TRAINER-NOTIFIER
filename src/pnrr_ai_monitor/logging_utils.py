from datetime import datetime
from pathlib import Path


def log_message(message: str, log_file: Path) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line)
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
