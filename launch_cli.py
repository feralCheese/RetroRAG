# Entry point for the frozen (PyInstaller) console executable retrorag-cli.exe (MCP stdio, API, CLI)
import sys

from retrorag.__main__ import main

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()   # OCR/import jobs run in worker processes
    sys.exit(main())
