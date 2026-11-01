"""Child process that parses one document and prints the result as JSON.

Run by `documents.extract_isolated` with `python -I`, so it is independent of
how the server was started. Input on stdin: one JSON header line, then the
raw document bytes. Output on stdout: the Extraction as JSON.
"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

# -I leaves the project off sys.path; add the folder that contains the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filepilot.config import Settings  # noqa: E402
from filepilot.documents import ERROR, Extraction, run_extractor  # noqa: E402


def main() -> None:
    stream = sys.stdin.buffer
    header = json.loads(stream.readline())
    data = stream.read()
    cfg = Settings(**header["cfg"])
    try:
        ex = run_extractor(header["kind"], data, header["truncated"], cfg)
    except MemoryError:
        ex = Extraction(ERROR, "The document needed too much memory to read.")
    except Exception as exc:
        ex = Extraction(ERROR, f"The document could not be read ({type(exc).__name__}).")
    sys.stdout.write(json.dumps(asdict(ex), default=str))


if __name__ == "__main__":
    main()
