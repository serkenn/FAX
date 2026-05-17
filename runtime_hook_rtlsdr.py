import sys
import os

# PyInstaller --onefile extracts to sys._MEIPASS at runtime.
# On Windows, ctypes DLL search does not automatically include that directory,
# so we register it explicitly to let pyrtlsdr find rtlsdr.dll.
if sys.platform == "win32" and hasattr(sys, "_MEIPASS"):
    os.add_dll_directory(sys._MEIPASS)
