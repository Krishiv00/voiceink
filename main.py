import os
import socket
import sys
import threading

os.environ.setdefault("DESKTOP_MODE", "1")

import webview

webview.settings['OPEN_DEVTOOLS_IN_DEBUG'] = False

APP_NAME = "VoiceInk"

_lock_socket: socket.socket | None = None


def _acquire_single_instance_lock() -> bool:
    global _lock_socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        sock.bind(("127.0.0.1", 51715))
        sock.listen(1)
    except OSError:
        sock.close()
        return False

    _lock_socket = sock

    return True


def _focus_existing_instance() -> None:
    try:
        if sys.platform.startswith("win"):
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(None, APP_NAME)
            if hwnd:
                if ctypes.windll.user32.IsIconic(hwnd):
                    ctypes.windll.user32.ShowWindow(hwnd, 9)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        elif sys.platform == "darwin":
            import subprocess
            subprocess.run(
                ["osascript", "-e", f'tell application "{APP_NAME}" to activate'],
                check=False, capture_output=True,
            )
    except Exception:
        pass


if not _acquire_single_instance_lock():
    _focus_existing_instance()
    sys.exit(0)


from app import main

_startup_file_path = sys.argv[1] if len(sys.argv) > 1 else None
if _startup_file_path and not os.path.isfile(_startup_file_path):
    _startup_file_path = None


def _get_primary_screen() -> "webview.Screen | None":
    screens = list(webview.screens) or []

    if not screens:
        return None
    for screen in screens:
        if screen.x <= 0 < screen.x + screen.width and screen.y <= 0 < screen.y + screen.height:
            return screen
    return screens[0]


target_screen = _get_primary_screen()

if target_screen is not None:
    screen_x, screen_y = target_screen.x, target_screen.y
    screen_width = target_screen.width
    screen_height = target_screen.height
else:
    screen_x = screen_y = 0
    screen_width, screen_height = 1280, 800

WINDOW_SCALE = 0.75

window_width = round(min(screen_width * WINDOW_SCALE, screen_width))
window_height = round(min(screen_height * WINDOW_SCALE, screen_height))
x = round(screen_x + (screen_width - window_width) / 2)
y = round(screen_y + (screen_height - window_height) / 2)


class Api:
    def toggle_fullscreen(self) -> None:
        window.toggle_fullscreen()


backend_thread = threading.Thread(target=main, args=(_startup_file_path,), daemon=True)
backend_thread.start()

window = webview.create_window(
    title=APP_NAME,
    url="http://127.0.0.1:5151",
    width=window_width,
    height=window_height,
    x=x,
    y=y,
    resizable=True,
    js_api=Api()
)


def on_loaded() -> None:
    window.maximize()

    window.evaluate_js("""
document.addEventListener('keydown', function(e) {
  if (e.key === 'F11') {
    window.pywebview.api.toggle_fullscreen();
  }
});
    """)


window.events.loaded += on_loaded

webview.start(debug=False)
