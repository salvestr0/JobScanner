"""
Job Scanner — single entry point
Run: python run.py
"""
import threading
import time
import webbrowser

from app import app

PORT = 5000
URL  = f"http://localhost:{PORT}"


def _open_browser():
    time.sleep(1.2)          # give Flask a moment to bind the port
    webbrowser.open(URL)


if __name__ == "__main__":
    print(f"Starting Job Scanner at {URL}")
    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(debug=False, threaded=True, port=PORT)
