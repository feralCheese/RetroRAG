# PyInstaller spec: builds dist\RetroRAG\ with
#   RetroRAG.exe       - the Windows app (no console)
#   retrorag-cli.exe   - console version, used by Claude Desktop / Claude Code (MCP stdio) and for the CLI
# Both share one set of libraries. Run build_exe.bat rather than calling this directly.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hidden = (collect_submodules("retrorag") + collect_submodules("uvicorn") + collect_submodules("mcp", filter=lambda n: not n.startswith("mcp.cli"))
          + collect_submodules("pymupdf") + ["pytesseract"])
datas = collect_data_files("mcp") + collect_data_files("pymupdf") + collect_data_files("retrorag")   # incl. pixel font
excludes = ["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQuick", "PySide6.QtQml",
            "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization"]

a = Analysis(["launch_gui.py", "launch_cli.py"], hiddenimports=hidden, datas=datas, excludes=excludes)
pyz = PYZ(a.pure)

gui_scripts = [s for s in a.scripts if s[0] != "launch_cli"]
cli_scripts = [s for s in a.scripts if s[0] != "launch_gui"]

gui = EXE(pyz, gui_scripts, [], exclude_binaries=True, name="RetroRAG", console=False, upx=False)
cli = EXE(pyz, cli_scripts, [], exclude_binaries=True, name="retrorag-cli", console=True, upx=False)

coll = COLLECT(gui, cli, a.binaries, a.datas, name="RetroRAG", upx=False)
