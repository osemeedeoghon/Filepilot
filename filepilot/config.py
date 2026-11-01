"""Runtime configuration for FilePilot.

Values can be tuned with environment variables, but the server host is always
forced to a loopback address so FilePilot is never exposed to the network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    # Hard ceiling on filesystem entries (files + folders + links) per scan.
    max_entries: int = 20_000
    # Maximum folder nesting below the selected folder.
    max_depth: int = 16
    # Wall-clock budget for a single scan, in seconds.
    max_scan_seconds: float = 30.0
    # Longest path string accepted from a request.
    max_path_length: int = 1024
    # Maximum number of per-item errors returned to the client.
    max_reported_errors: int = 200
    # Seconds to wait for the native macOS folder picker.
    picker_timeout_seconds: int = 300
    # Duplicate detection: wall-clock budget, total bytes read, and read size.
    dup_max_seconds: float = 600.0
    dup_max_bytes: int = 100 * 1024**3
    dup_chunk_bytes: int = 1024 * 1024
    # Files larger than this get a quick check of their first bytes before a full hash.
    dup_prefix_threshold: int = 1024 * 1024
    dup_prefix_bytes: int = 64 * 1024
    # Document inspection: PDF/DOCX files above this size are refused.
    doc_max_bytes: int = 25 * 1024 * 1024
    # Text and CSV files: only this many bytes are read (larger files are truncated).
    doc_text_read_bytes: int = 2 * 1024 * 1024
    # A DOCX may not expand to more than this when unzipped (zip-bomb guard).
    doc_max_unzipped_bytes: int = 100 * 1024 * 1024
    doc_max_pages: int = 300
    doc_max_chars: int = 300_000
    doc_preview_chars: int = 20_000
    doc_timeout_seconds: float = 20.0
    # Parse documents in a separate process that is killed at the time limit.
    doc_isolate: bool = True
    # Optional local AI (Ollama). The URL must be a loopback address.
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = ""  # empty = first installed model
    ai_timeout_seconds: float = 120.0
    ai_max_input_chars: int = 8_000


def load_settings() -> Settings:
    return Settings(
        port=_int_env("FILEPILOT_PORT", 8765, 1024, 65535),
        max_entries=_int_env("FILEPILOT_MAX_ENTRIES", 20_000, 1, 200_000),
        max_depth=_int_env("FILEPILOT_MAX_DEPTH", 16, 0, 64),
        dup_max_seconds=float(_int_env("FILEPILOT_DUP_MAX_SECONDS", 600, 10, 7200)),
        dup_max_bytes=_int_env("FILEPILOT_DUP_MAX_GB", 100, 1, 10_000) * 1024**3,
        doc_max_bytes=_int_env("FILEPILOT_DOC_MAX_MB", 25, 1, 500) * 1024 * 1024,
        ollama_url=os.environ.get("FILEPILOT_OLLAMA_URL", "http://127.0.0.1:11434"),
        ollama_model=os.environ.get("FILEPILOT_OLLAMA_MODEL", "")[:200],
    )


settings = load_settings()
