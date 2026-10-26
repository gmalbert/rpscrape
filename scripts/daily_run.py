#!/usr/bin/env python3

"""
Daily driver for GB flat result scraping.

Fills every date missing from data/region/gb/flat between the earliest covered
day and yesterday, so a failed or skipped day heals itself on the next run.

Before scraping it picks a working egress: each configured HTTP proxy (the
Gluetun exits published on loopback) is probed against Racing Post, and the
first one that is not throttled wins. Direct access is the last resort.
"""

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import time

from pathlib import Path

from curl_cffi import requests

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'scripts'
DATA = ROOT / 'data' / 'region' / 'gb' / 'flat'
LOG_DIR = ROOT / '.cache' / 'logs'
PROXY_FILE = ROOT / '.cache' / 'vpn' / 'proxies.txt'

PROBE_URL = 'https://www.racingpost.com/results/'
PROBE_RETRIES = 2
MAX_DAYS_PER_RUN = 10
COMPOSE_FILE = Path(
    os.getenv(
        'RPS_VPN_COMPOSE',
        r'C:\Users\Greg\web_apps\horse-racing-predictions\compose.vpn.yaml',
    )
)
DEFAULT_PROXIES = (
    'http://127.0.0.1:18881',
    'http://127.0.0.1:18882',
    'http://127.0.0.1:18883',
)

FILENAME_RE = re.compile(
    r'^(?P<y1>\d{4})_(?P<m1>\d{2})_(?P<d1>\d{2})'
    r'(?:_(?P<y2>\d{4})_(?P<m2>\d{2})_(?P<d2>\d{2}))?\.csv$'
)

_log_handle = None


def log(message: str) -> None:
    line = f'[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {message}'
    print(line, flush=True)
    if _log_handle is not None:
        _log_handle.write(line + '\n')
        _log_handle.flush()


def covered_dates() -> set[dt.date]:
    """Dates covered by existing outputs, derived from filenames.

    Ranges are expanded so that days with no racing (header-only files) still
    count as covered and are not retried forever.
    """
    covered: set[dt.date] = set()

    if not DATA.is_dir():
        return covered

    for path in DATA.glob('*.csv'):
        match = FILENAME_RE.match(path.name)
        if match is None:
            continue

        start = dt.date(int(match['y1']), int(match['m1']), int(match['d1']))
        if match['y2'] is None:
            covered.add(start)
            continue

        end = dt.date(int(match['y2']), int(match['m2']), int(match['d2']))
        if end < start:
            continue

        day = start
        while day <= end:
            covered.add(day)
            day += dt.timedelta(days=1)

    return covered


def missing_dates(target: dt.date | None) -> list[dt.date]:
    if target is not None:
        return [target]

    covered = covered_dates()
    if not covered:
        log('no existing outputs to establish coverage from')
        return []

    yesterday = dt.date.today() - dt.timedelta(days=1)
    day = min(covered)
    missing: list[dt.date] = []

    while day <= yesterday:
        if day not in covered:
            missing.append(day)
        day += dt.timedelta(days=1)

    return missing


def proxy_candidates() -> list[str]:
    from_env = os.getenv('RPS_PROXIES')

    if from_env:
        return [part.strip() for part in from_env.split(',') if part.strip()]

    if PROXY_FILE.exists():
        names = [
            line.strip()
            for line in PROXY_FILE.read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.strip().startswith('#')
        ]
        if names:
            return names

    return list(DEFAULT_PROXIES)


def wsl_path(path: Path) -> str:
    drive = path.drive.rstrip(':').lower()
    relative = path.relative_to(path.anchor).as_posix()
    return f'/mnt/{drive}/{relative}'


def ensure_containers() -> None:
    """Best effort: bring the Gluetun exits up so the proxies exist.

    Containers use restart: unless-stopped, so this is normally a no-op; it
    matters after a reboot or when WSL has shut down.
    """
    if not COMPOSE_FILE.exists():
        log(f'no VPN compose file at {COMPOSE_FILE}')
        return

    try:
        result = subprocess.run(
            ['wsl', '-e', 'docker', 'compose', '-f', wsl_path(COMPOSE_FILE), 'up', '-d'],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as error:
        log(f'could not start VPN containers: {error!r}')
        return

    detail = ((result.stdout or '') + (result.stderr or '')).strip()

    if result.returncode == 0:
        log('VPN containers up')
        return

    log(f'VPN containers did not start (exit {result.returncode}): {detail[-400:]}')


def probe(proxy: str | None) -> int | str:
    proxies = {'http': proxy, 'https': proxy} if proxy else None
    last: Exception | None = None

    for _ in range(PROBE_RETRIES):
        try:
            response = requests.get(
                PROBE_URL, impersonate='chrome', timeout=20, proxies=proxies
            )
            return response.status_code
        except Exception as error:
            last = error
            time.sleep(2)

    return f'ERR {last!r}'


def workflow_env(proxy: str | None) -> dict[str, str]:
    env = os.environ.copy()

    if proxy is None:
        for key in ('RPS_PROXY', 'HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
            env.pop(key, None)
        return env

    env['RPS_PROXY'] = proxy
    env['HTTP_PROXY'] = proxy
    env['HTTPS_PROXY'] = proxy
    env['http_proxy'] = proxy
    env['https_proxy'] = proxy

    return env


def run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=SCRIPTS,
        stdout=_log_handle,
        stderr=subprocess.STDOUT,
        input=b'\n',
        env=env,
    )


def ensure_egress() -> str | None:
    """Returns the proxy to use, None for direct, or raises SystemExit on failure."""
    for proxy in proxy_candidates():
        status = probe(proxy)
        log(f'probe via {proxy}: {status}')

        if status == 200:
            return proxy

    status = probe(None)
    log(f'probe direct: {status}')

    if status == 200:
        return None

    raise SystemExit('no usable egress: every proxy and direct access were rejected')


def scrape(day: dt.date, proxy: str | None) -> int:
    log(f'scraping {day.isoformat()} via {proxy or "direct"}')
    result = run(
        ['-u', 'rpscrape.py', '-d', day.strftime('%Y/%m/%d'), '-r', 'gb', '-t', 'flat'],
        env=workflow_env(proxy),
    )
    log(f'{day.isoformat()} finished with exit {result.returncode}')
    return result.returncode


def main() -> int:
    global _log_handle

    parser = argparse.ArgumentParser(description='Scrape missing GB flat result days.')
    _ = parser.add_argument('--date', help='Only this date (YYYY-MM-DD), instead of catch-up')
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log_handle = open(LOG_DIR / f'daily_{dt.date.today():%Y_%m_%d}.log', 'a', encoding='utf-8')

    try:
        target = dt.date.fromisoformat(args.date) if args.date else None
        missing = missing_dates(target)

        if not missing:
            log('nothing to do; all days up to yesterday are covered')
            return 0

        if len(missing) > MAX_DAYS_PER_RUN:
            log(f'{len(missing)} days missing; handling the first {MAX_DAYS_PER_RUN}: '
                f'{", ".join(d.isoformat() for d in missing[:MAX_DAYS_PER_RUN])}')
            missing = missing[:MAX_DAYS_PER_RUN]
        else:
            log(f'{len(missing)} day(s) to fetch: {", ".join(d.isoformat() for d in missing)}')

        ensure_containers()

        try:
            proxy = ensure_egress()
        except SystemExit as error:
            log(str(error))
            return 1

        failures = [day for day in missing if scrape(day, proxy) != 0]

        if failures:
            log(f'failed: {", ".join(d.isoformat() for d in failures)}')
            return 1

        log('done')
        return 0
    finally:
        if _log_handle is not None:
            _log_handle.close()


if __name__ == '__main__':
    sys.exit(main())
