# fireflare

Drives Firefox Nightly to run the [`@cloudflare/speedtest`](https://github.com/cloudflare/speedtest) library (loaded from esm.sh via a local HTML page), records throughput, latency, jitter, plus per-measurement points, and saves structured JSON results. A separate `report.py` renders the JSON files as a self-contained HTML report (summary table + boxplots).

## Report

A rendered comparison report is checked in at [results/report.html](results/report.html): five connection paths side by side, direct HTTP/1.1, direct HTTP/3, and Firefox's in-browser IP-protection (VPN) proxy reached three ways (HTTP/2 CONNECT, HTTP/3 CONNECT, and HTTP/3 MASQUE connect-udp), with throughput, latency, and jitter per path. Generate the data with `uv run main.py --matrix`, then render with `uv run report.py`.

## Status

- **Direct baseline** works (`uv run main.py`).
- **HTTP/3 variant** works (`uv run main.py --h3`, points the library at `bastion.h3.speed.cloudflare.com`).
- **In-browser VPN (IP protection)** works (`uv run main.py --vpn`), routing speedtest traffic through Firefox's IP protection / Fastly proxy. Two layers matter and the report surfaces both: the transport to the proxy (HTTP/2 CONNECT, or HTTP/3 with `--custom-firefox`), and how the origin is reached through it (classic CONNECT, or MASQUE connect-udp). With `--custom-firefox` and Alt-Svc priming, end-to-end MASQUE works: the origin connection itself is HTTP/3 (connect-udp) through the proxy.

## Requirements

- Linux x86_64
- [uv](https://docs.astral.sh/uv/)
- Python 3.11+

Firefox Nightly and geckodriver are downloaded on first run into `.cache/`.

## One-time setup for `--vpn`

The VPN runs need a Firefox profile that's already signed in and has the feature flipped on. Do this once:

```
env LD_LIBRARY_PATH='' ./.cache/firefox/firefox -profile ./profile
```

Then in that Firefox window:
1. Go to `about:config`, set `browser.ipProtection.enabled = true`.
2. Sign in to a Firefox Account.
3. Quit Firefox cleanly (releasing the profile lock).

The persistent profile lives at `./profile/` (gitignored) and is reused across runs. `--vpn` runs toggle the proxy on; runs without `--vpn` turn it off.

## Run

```
uv run main.py                # direct baseline (HTTP/1.1)
uv run main.py --h3           # direct HTTP/3 (bastion.h3.speed.cloudflare.com)
uv run main.py --vpn          # route through IP protection (HTTP/2 CONNECT proxy hop)
uv run main.py --vpn --h3 --custom-firefox   # end-to-end HTTP/3 over MASQUE (try build)
uv run main.py --matrix       # all five comparison paths, one result JSON each
uv run main.py --matrix --profile   # also capture a Firefox Profiler "Networking" profile per run
uv run main.py --debug        # tiny measurement set; for plumbing changes
uv run main.py --headless     # no browser window (combinable with any of the above)
```

Use `--firefox PATH` to run a local build instead of a downloaded one (e.g. `--firefox ../firefox/obj-x86_64-pc-linux-gnu/dist/bin/firefox`), and `--profile-dir DIR` to use a profile other than `./profile`.

Output JSON files land in `results/`, named `<tag>-<utc-timestamp>.json`. The tag records, in order: `debug` (only with `--debug`); on `--vpn` runs `proxy-<v>` for the transport to the proxy (`h3` = QUIC with connect-udp negotiated to the proxy, `h2` = TCP CONNECT); and `origin-<v>` for the HTTP version negotiated with the origin, which reflects the destination tunnel method (`h2` = CONNECT, `h3` = connect-udp end to end).

## Self-hosted server and proxy (`--bench`)

`--bench` swaps speed.cloudflare.com and the IP-protection (Fastly) servers for [proxy-bench](../proxy-bench)'s `speedtest-server` and `connect-proxy`. IP protection still needs the signed-in profile (see the one-time setup above), because it still fetches a proxy pass from Mozilla before it connects. The pass is sent to the proxy, which ignores it.

1. In a separate terminal, build proxy-bench and start both programs (Ctrl-C stops them). This assumes proxy-bench is checked out next to fireflare:

   ```
   cd ../proxy-bench && make && \
     SERVER_ARGS="--alt-svc" \
     PROXY_ARGS="--allow 127.0.0.1:8443 --allow [::1]:8443 --allow 127.0.0.1:8080" \
     scripts/run-all.sh
   ```

   `--alt-svc` lets the h3 configurations discover HTTP/3. The proxy's `--allow` list keeps the default destinations (the speed test server on 8443), and adds the port the test page is served on (8080), because the page is loaded through the proxy too.

2. Run fireflare, here with the full matrix and a local Firefox build:

   ```
   uv run main.py --bench --matrix --firefox ../firefox/obj-x86_64-pc-linux-gnu/dist/bin/firefox --profile-dir profile-local
   ```

   Every configuration works with `--bench`, alone or with `--matrix`, `--debug`, `--headless` and so on. Results go to `results-bench/`. Render them with `uv run report.py results-bench`.

How it works:

- The speed test server is addressed as `localhost4:8443` (`--bench-server`), and the test page is served at `http://localhost4:8080/` (`--bench-page-port`). `localhost4` is in the stock `/etc/hosts` and resolves to 127.0.0.1. IP protection never proxies loopback names or IPs (`localhost`, `*.localhost`, 127/8, `::1`), or any subresource of a page on one, so neither can use those.
- The proxy is `localhost:4433` (`--bench-proxy`). It's configured through `browser.ipProtection.override.serverlist`, as MASQUE with a CONNECT fallback, or just CONNECT when HTTP/3 is disabled.
- In bench mode, Alt-Svc is turned off for the non-h3 configurations, so they stay on TCP even though the server advertises HTTP/3.
- The self-signed certificates are accepted through WebDriver's insecure-certs mode. `network.http.http3.disable_when_third_party_roots_found` is turned off, otherwise Firefox refuses HTTP/3 to them.
- The origin's HTTP version comes from Resource Timing's `nextHopProtocol`, since the server has no `/cdn-cgi/trace`.

## Loaded-latency probe (`probe.py`)

`probe.py` looks at latency under load in more detail than the speed test does. It runs one large transfer at a time against the `--bench` server (start it as above), with a zero-byte ping every ~20 ms alongside, and records each ping's send time and time to first byte. That shows *how* latency behaves during a transfer: every ping a little slower (a standing queue), or one ping that waits for the whole transfer (head-of-line blocking). A run takes ~20 s.

```
uv run probe.py 5 --firefox ../firefox/obj-x86_64-pc-linux-gnu/dist/bin/firefox --profile-dir profile-local
uv run probe.py 5 --mode upload --bytes 50000000      # uploads instead of downloads
uv run probe.py 2 --ping-priority high                # pings with fetch(..., {priority: 'high'})
uv run probe.py 5 --env MOZ_LOG=timestamp,nsHttp:5 --env MOZ_LOG_FILE=/tmp/fx.log
uv run probe.py 5 --pref network.http.http3.enable_qlog=true --env TMPDIR=/tmp/qlog
```

The argument is a `--matrix` configuration, by number or label (`5` or `5-proxy-h3masque`). It takes the same `--firefox`, `--profile-dir`, `--headless` and `--bench-*` options as `main.py`, plus `--pref` and `--env` for Firefox prefs and environment variables (e.g. logging, or switches in a local build). It prints a per-round summary (throughput, ping percentiles, the first pings and the slow ones, each as `sent at +ms: ttfb ms`), and saves the full result to `results-probe/`, which isn't checked in. With `enable_qlog`, Firefox writes qlog files for each QUIC connection under `$TMPDIR/qlog_<pid>/`.

## License

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE))
- MIT License ([LICENSE-MIT](LICENSE-MIT))

at your option.
