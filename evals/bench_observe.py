"""
Offline micro-benchmark for BrowserSession.observe() -- the page-reading
half of every browser step. No LLM, no API key, no internet: it generates a
local page with N interactive elements (links, buttons, inputs including a
password field, checkboxes, selects, a few hidden ones), serves it from a
temp dir, and times observe() on it.

    python evals/bench_observe.py                 # 50, 200 and 500 elements
    python evals/bench_observe.py --sizes 1000 --repeats 20

Unlike evals/run_evals.py this costs nothing, so it's safe to run any time a
change touches observe(). See docs/JEV_VOICE_PLAN.md Phase 0 for why this
number matters: it's paid on every browser step, before the model is even
asked anything.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser import BrowserSession  # noqa: E402
from config import load_config  # noqa: E402


def build_page(n: int) -> str:
    """Roughly n interactive elements, cycling through the kinds observe() handles."""
    parts = []
    for i in range(n):
        kind = i % 8
        if kind == 0:
            parts.append(f'<a href="#a{i}">Link number {i}</a>')
        elif kind == 1:
            parts.append(f"<button>Button {i}</button>")
        elif kind == 2:
            parts.append(f'<input type="text" name="field{i}" placeholder="Field {i}" value="v{i}">')
        elif kind == 3:
            parts.append(f'<input type="checkbox" aria-label="Option {i}"{" checked" if i % 16 == 3 else ""}>')
        elif kind == 4:
            parts.append(f'<select aria-label="Choice {i}"><option>One</option><option>Two</option></select>')
        elif kind == 5:
            parts.append(f'<textarea aria-label="Notes {i}">text {i}</textarea>')
        elif kind == 6:
            parts.append(f'<input type="password" aria-label="Password {i}" value="secret{i}">')
        else:
            parts.append(f'<div role="button" style="display:none">Hidden {i}</div>')
    body = "\n".join(f"<p>Row {i}: {p}</p>" for i, p in enumerate(parts))
    return f"<!DOCTYPE html><html><head><title>Bench {n}</title></head><body>{body}</body></html>"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--sizes", type=int, nargs="+", default=[50, 200, 500])
    parser.add_argument("--repeats", type=int, default=10)
    args = parser.parse_args(argv)

    import dataclasses

    config = dataclasses.replace(load_config(), headless=True, use_persistent_profile=False)

    with tempfile.TemporaryDirectory() as tmp:
        for n in args.sizes:
            (Path(tmp) / f"bench_{n}.html").write_text(build_page(n), encoding="utf-8")
        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):  # keep the timing table readable
                pass

        handler = functools.partial(QuietHandler, directory=tmp)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        session = BrowserSession(config)
        session.start()
        try:
            print(f"{'elements on page':>16} {'kept':>6} {'median ms':>10} {'min ms':>8}")
            for n in args.sizes:
                session.goto(f"{base}/bench_{n}.html")
                session.observe()  # warm-up
                times, kept = [], 0
                for _ in range(args.repeats):
                    started = time.perf_counter()
                    obs = session.observe()
                    times.append((time.perf_counter() - started) * 1000)
                    kept = len(obs.elements)
                print(f"{n:>16} {kept:>6} {statistics.median(times):>10.1f} {min(times):>8.1f}")
        finally:
            session.stop()
            server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
