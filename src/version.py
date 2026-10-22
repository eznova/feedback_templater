"""Версия исходников: метаданные сборки либо локальный Git."""
import os
import re
import subprocess
from pathlib import Path


def commit_hash():
    value = os.environ.get('APP_COMMIT', '').strip()
    if not value:
        try:
            value = subprocess.run(
                ['git', 'rev-parse', '--short=7', 'HEAD'],
                cwd=Path(__file__).resolve().parent.parent,
                capture_output=True, text=True, check=True, timeout=2,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return 'неизвестен'
    return value[:7].lower() if re.fullmatch(r'[0-9a-fA-F]{7,40}', value) else 'неизвестен'
