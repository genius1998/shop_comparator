# -*- coding: utf-8 -*-

import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)


def _configure_playwright_browser_path():
    if not getattr(sys, "frozen", False):
        return

    candidates = []

    if hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "browsers"))

    exe_dir = os.path.dirname(sys.executable)
    candidates.extend(
        [
            os.path.join(exe_dir, "_internal", "browsers"),
            os.path.join(exe_dir, "browsers"),
        ]
    )

    for candidate in candidates:
        if os.path.isdir(candidate):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = candidate
            break


_configure_playwright_browser_path()

def main():
    if "--desktop" not in sys.argv:
        from shop_comparator.web.web_app import main as web_main
        return web_main()
    import tkinter as tk
    from shop_comparator.desktop.gui import App
    root = tk.Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    main()
