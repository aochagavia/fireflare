"""Loaded-latency probe: one large transfer at a time with a ping alongside.

Runs probe.html against the self-hosted bench (see `--bench` in the README):
each round downloads (or uploads) BYTES while a zero-byte ping is sent every
INTERVAL ms, and records each ping's send time and time to first byte. That
shows how latency under load behaves over the course of a transfer, which the
speed test's summary numbers hide: e.g. whether every ping is a little slower
(a standing queue) or one ping waits for the whole transfer (head-of-line
blocking). A run takes ~20 s, against minutes for the full speed test.

    uv run probe.py 5                    # MASQUE, 3 x 250 MB download
    uv run probe.py 5 --mode upload --bytes 50000000
    uv run probe.py 2 --ping-priority high
    uv run probe.py 5 --env MOZ_LOG=timestamp,nsHttp:5 --env MOZ_LOG_FILE=/tmp/log

CONFIG is one of the --matrix configurations, by number or label (1-direct-h1h2,
2-direct-h3, 3-proxy-h2connect, 4-proxy-h3connect, 5-proxy-h3masque). The full
result is saved as JSON under results-probe/ (not checked in); a summary is
printed.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
from pathlib import Path

import main as fireflare

PROBE_HTML = (fireflare.ROOT / "probe.html").read_text()
RESULTS_PROBE = fireflare.ROOT / "results-probe"


def pref_value(v: str) -> bool | int | str:
    """`true`/`false` and integers as such, anything else as a string."""
    if v in ("true", "false"):
        return v == "true"
    try:
        return int(v)
    except ValueError:
        return v


def key_value(arg: str) -> tuple[str, str]:
    key, sep, value = arg.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {arg!r}")
    return key, value


def quantiles(xs: list[float]) -> str:
    if not xs:
        return "-"
    xs = sorted(xs)
    return (f"p50={statistics.median(xs):.0f} p90={xs[int(len(xs) * .9)]:.0f} "
            f"max={xs[-1]:.0f} n={len(xs)}")


def ttfb(t: dict) -> float:
    return t["fb"] - t["rs"]


def summarize(result: dict, slow_ms: float) -> None:
    print("idle ping ttfb (ms):", quantiles([ttfb(x) for x in result["idle"]]))
    for i, r in enumerate(result["rounds"]):
        end, nbytes = r["progress"][-1]
        dur = end - r["t0"]
        pings = r["pings"]
        print(f"round {i} ({r['proto']}): {dur:.0f} ms, {nbytes * 8 / dur / 1e3:.0f} Mbps; "
              f"ping ttfb (ms) {quantiles([ttfb(x) for x in pings])}")
        print("   first pings (sent at +ms: ttfb ms):",
              " ".join(f"{x['t0'] - r['t0']:.0f}:{ttfb(x):.0f}" for x in pings[:12]))
        slow = [x for x in pings if ttfb(x) > slow_ms]
        if slow:
            print(f"   pings over {slow_ms:g} ms (sent at +ms: ttfb ms):",
                  " ".join(f"{x['t0'] - r['t0']:.0f}:{ttfb(x):.0f}" for x in slow[:12]))


def main() -> None:
    labels = [label for label, _, _ in fireflare.MATRIX]
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=f"CONFIG: {', '.join(labels)} (or just the number)")
    parser.add_argument("config", help="--matrix configuration to run")
    parser.add_argument("--mode", choices=["download", "upload"], default="download")
    parser.add_argument("--bytes", type=int, default=250_000_000,
                        help="size of each transfer (default: %(default)s)")
    parser.add_argument("--rounds", type=int, default=3,
                        help="transfers per run (default: %(default)s)")
    parser.add_argument("--interval", type=int, default=20, metavar="MS",
                        help="pause between a ping's response and the next "
                             "ping (default: %(default)s)")
    parser.add_argument("--delay", type=int, default=50, metavar="MS",
                        help="wait before a round's first ping (default: %(default)s)")
    parser.add_argument("--ping-priority", choices=["high", "low", "auto"],
                        help="fetch() priority for the pings (default: none given)")
    parser.add_argument("--pref", type=key_value, action="append", default=[],
                        metavar="NAME=VALUE",
                        help="set a Firefox pref at launch (repeatable)")
    parser.add_argument("--env", type=key_value, action="append", default=[],
                        metavar="NAME=VALUE",
                        help="set an environment variable for Firefox, e.g. "
                             "MOZ_LOG (repeatable)")
    parser.add_argument("--tag", help="appended to the result's label")
    parser.add_argument("--slow-ms", type=float, default=20,
                        help="list pings slower than this (default: %(default)s)")
    parser.add_argument("--results-dir", type=Path, metavar="DIR",
                        default=RESULTS_PROBE,
                        help="where to save the result (default: ./results-probe)")
    fireflare.add_browser_args(parser)
    args = parser.parse_args()

    matches = [m for m in fireflare.MATRIX
               if args.config in (m[0], m[0].split("-", 1)[0])]
    if not matches:
        parser.error(f"unknown config {args.config!r}, expected one of {', '.join(labels)}")
    label, kwargs, _ = matches[0]
    if args.tag:
        label = f"{label}-{args.tag}"

    fireflare.require_linux_x86_64()
    fireflare.PROFILE = args.profile_dir.resolve()
    fireflare.RESULTS = args.results_dir.resolve()
    for d in (fireflare.CACHE, fireflare.RESULTS, fireflare.PROFILE):
        d.mkdir(exist_ok=True)
    # Inherited by geckodriver, and from it by Firefox.
    os.environ.update(dict(args.env))

    cfg = {"mode": args.mode, "bytes": args.bytes, "rounds": args.rounds,
           "interval": args.interval, "delay": args.delay,
           "pingPriority": args.ping_priority or ""}
    html = PROBE_HTML.replace("__CFG__", json.dumps(cfg)).encode()
    bench = fireflare.bench_from_args(args)
    # Configs 4 and 5 need the custom build, as in --matrix.
    firefox = fireflare.resolve_firefox(args.firefox, custom=True)
    geckodriver = fireflare.ensure_geckodriver()
    print(f"Using {fireflare.firefox_version(firefox)}")

    server, base_url = fireflare.serve_page(bench, html=html)
    try:
        result = fireflare.run_once(
            firefox, geckodriver, base_url, debug=False, label=label,
            profile=args.profile, headless=args.headless, bench=bench,
            extra_prefs={k: pref_value(v) for k, v in args.pref}, **kwargs)
    finally:
        server.shutdown()
    summarize(result, args.slow_ms)


if __name__ == "__main__":
    main()
