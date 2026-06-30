from __future__ import annotations

from pathlib import Path

SUPPORTED_DB_SUFFIXES = {'.dbc', '.arxml'}


def is_database_file(path: str) -> bool:
    """Return True if *path* has a supported CAN database file extension."""
    return Path(path).suffix.lower() in SUPPORTED_DB_SUFFIXES


def db_format_label(path: str) -> str:
    """Return 'ARXML' for .arxml files, 'DBC' for everything else."""
    return 'ARXML' if Path(path).suffix.lower() == '.arxml' else 'DBC'
