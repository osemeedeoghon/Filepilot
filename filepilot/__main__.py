"""Start FilePilot with `python -m filepilot`. Always binds to 127.0.0.1."""

import uvicorn

from .config import settings


def main() -> None:
    print(f"FilePilot running at http://{settings.host}:{settings.port}  (Ctrl+C to stop)")
    uvicorn.run("filepilot.app:app", host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
