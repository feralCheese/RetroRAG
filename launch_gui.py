# Entry point for the frozen (PyInstaller) GUI executable RetroRAG.exe
import sys

from retrorag.gui.app import run

if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else None))
