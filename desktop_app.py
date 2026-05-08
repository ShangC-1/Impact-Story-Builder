from __future__ import annotations

import socket
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import BOTH, LEFT, StringVar, Tk, ttk, messagebox

from server import RUNTIME_ROOT, create_server


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4173
MAX_PORT = 4193
LOG_PATH = RUNTIME_ROOT / "impact-story-builder-launcher.log"
HEALTH_TIMEOUT_SECONDS = 12


def log_message(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{message}\n")


def find_available_port(host: str, start_port: int, end_port: int) -> int:
    for port in range(start_port, end_port + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No open port was found between {start_port} and {end_port}.")


@dataclass
class ServerSession:
    host: str
    port: int

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


class ImpactStoryDesktopApp:
    def __init__(self) -> None:
        log_message("Launcher starting.")
        self.root = Tk()
        self.root.title("Impact Story Builder")
        self.root.geometry("540x280")
        self.root.minsize(500, 250)
        self.root.protocol("WM_DELETE_WINDOW", self.close_app)

        self.session = ServerSession(
            host=DEFAULT_HOST,
            port=find_available_port(DEFAULT_HOST, DEFAULT_PORT, MAX_PORT),
        )
        log_message(f"Using local URL {self.session.url}")
        self.server = create_server(self.session.host, self.session.port)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)

        self.status_text = StringVar(value="Starting local server...")
        self.url_text = StringVar(value=self.session.url)

        self._build_ui()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill=BOTH, expand=True)

        ttk.Label(frame, text="Impact Story Builder", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="This Windows app starts the local demo server and opens the prototype in your browser.",
            wraplength=480,
            justify=LEFT,
        ).pack(anchor="w", pady=(8, 10))

        status_box = ttk.LabelFrame(frame, text="Status", padding=12)
        status_box.pack(fill=BOTH, expand=True)

        ttk.Label(status_box, textvariable=self.status_text, wraplength=450, justify=LEFT).pack(anchor="w")
        ttk.Label(status_box, textvariable=self.url_text, foreground="#0e6b73").pack(anchor="w", pady=(8, 0))
        ttk.Label(
            status_box,
            text="API keys are still entered inside the browser UI and are not bundled into this application.",
            wraplength=450,
            justify=LEFT,
        ).pack(anchor="w", pady=(10, 0))

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(14, 0))

        ttk.Button(actions, text="Open In Browser", command=self.open_browser).pack(side=LEFT)
        ttk.Button(actions, text="Copy Link", command=self.copy_link).pack(side=LEFT, padx=(10, 0))
        ttk.Button(actions, text="Close App", command=self.close_app).pack(side=LEFT, padx=(10, 0))

    def start(self) -> None:
        self.server_thread.start()
        log_message("Server thread started.")
        self.status_text.set("Starting local server. The browser will open when the app is ready.")
        threading.Thread(target=self.wait_for_server_ready, daemon=True).start()
        self.root.mainloop()

    def open_browser(self) -> None:
        log_message(f"Opening browser at {self.session.url}")
        webbrowser.open(self.session.url)

    def wait_for_server_ready(self) -> None:
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        health_url = f"{self.session.url}/api/health"
        last_error = "Unknown startup error."

        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(health_url, timeout=2) as response:
                    if response.status == 200:
                        log_message("Health check succeeded.")
                        self.root.after(0, self._on_server_ready)
                        return
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = str(error)
            time.sleep(0.2)

        log_message(f"Health check failed: {last_error}")
        self.root.after(0, lambda: self._on_server_failed(last_error))

    def _on_server_ready(self) -> None:
        self.status_text.set("Local server is running. The browser will open automatically.")
        self.open_browser()

    def _on_server_failed(self, last_error: str) -> None:
        self.status_text.set(
            "The local server did not finish starting. Close this window and try again, or run the PowerShell script."
        )
        messagebox.showerror(
            "Impact Story Builder",
            f"The local server did not start correctly.\n\nLast error: {last_error}",
        )

    def copy_link(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.session.url)
        self.root.update()
        self.status_text.set("Local link copied. Share it only on the same computer while this app is open.")

    def close_app(self) -> None:
        log_message("Closing launcher.")
        try:
            self.server.shutdown()
            self.server.server_close()
        finally:
            self.root.destroy()


def main() -> None:
    try:
        app = ImpactStoryDesktopApp()
        app.start()
    except Exception as error:
        log_message("Launcher failed:")
        log_message(traceback.format_exc())
        messagebox.showerror("Impact Story Builder", f"Unable to start the local app.\n\n{error}")
        raise


if __name__ == "__main__":
    main()
