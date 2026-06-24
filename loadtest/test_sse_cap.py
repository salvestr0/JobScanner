"""Verify the SSE concurrent-stream cap.

Opens more concurrent /api/scan/stream connections than MAX_SSE_STREAMS and
checks that the excess get a fast 503 'busy' instead of each grabbing a worker
thread. Run against a live gunicorn started by the caller.
"""
import concurrent.futures as cf
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOST = os.getenv("HOST", "http://127.0.0.1:10000")
N = int(os.getenv("N_STREAMS", "10"))

with open(os.path.join(os.path.dirname(__file__), "users.json")) as f:
    EMAILS = json.load(f)["emails"]


def login():
    s = requests.Session()
    r = s.post(f"{HOST}/api/auth/login",
               json={"email": EMAILS[0], "password": "loadtest123"}, timeout=10)
    r.raise_for_status()
    return s


def open_stream(i):
    s = login()
    try:
        # stream=True returns as soon as headers arrive; we only need the status.
        r = s.get(f"{HOST}/api/scan/stream", stream=True, timeout=20)
        code = r.status_code
        r.close()
        return code
    except Exception as e:  # noqa: BLE001
        return f"ERR:{e}"


def main():
    with cf.ThreadPoolExecutor(max_workers=N) as ex:
        codes = list(ex.map(open_stream, range(N)))
    ok = sum(1 for c in codes if c == 200)
    busy = sum(1 for c in codes if c == 503)
    print(f"Opened {N} concurrent streams -> {ok} x 200, {busy} x 503-busy, codes={codes}")
    cap = int(os.getenv("EXPECT_CAP", "6"))
    assert ok <= cap, f"more 200s ({ok}) than the cap ({cap}) — cap not enforced!"
    assert busy >= 1, "expected at least one 503-busy when over the cap"
    print("PASS: stream cap enforced — excess streams shed instead of starving threads")


if __name__ == "__main__":
    main()
