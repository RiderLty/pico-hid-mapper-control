"""Double-click entry point without a separate console window."""
from tkinter import messagebox
import traceback

from remote_control import main
from app_info import APP_TITLE

try:
    if main():
        messagebox.showerror(APP_TITLE, "启动失败，请重新打开程序。")
except Exception:
    traceback.print_exc()
    messagebox.showerror(APP_TITLE, "启动失败，请重新打开程序。")
