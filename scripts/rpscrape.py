#!/usr/bin/env python3

import gzip
import os
import sys

from collections.abc import Callable
from datetime import date
from dotenv import load_dotenv
from orjson import loads
from pathlib import Path
from time import sleep
from typing import TextIO, TYPE_CHECKING
from urllib.parse import urlencode

from utils.argparser import ArgParser
from utils.betfair import Betfair
from utils.network import NetworkClient, Persistent406Error
from utils.next_data import page_props, race_result
from utils.paths import Paths, build_paths
from utils.settings import Settings
from utils.update import Update

_ = load_dotenv()

settings = Settings()

if TYPE_CHECKING:
    from utils.betfair import Betfair

RACE_TYPES: dict[str, set[str]] = {
    'flat': {'Flat'},
    'jumps': {'Chase', 'Hurdle', 'NH Flat'},
}

THROTTLE_ROUNDS = 5
THROTTLE_DELAY = 60.0
RACE_DELAY = 2.0
CHUNK_SIZE = 250
CHUNK_PAUSE = 120.0
DISCOVERY_DELAY = 1.0


def check_for_update() -> bool:
    update = Update()

    if not update.available():
        return False

    choice = input('Update available. Do you want to update? [y/N] ').strip().lower()
    if choice != 'y':
        return False

    success = update.pull_latest()
    print('Updated successfully.' if success else 'Failed to update.')
    return success


def sort_key(url: str) -> tuple[str, str]:
    parts = url.split('/')
    race_course = parts[5]
    race_date = parts[6]
    return race_date, race_course


def get_race_urls(
    years: list[str], tracks: list[tuple[str, str]], race_type: str, client: NetworkClient
) -> list[str]:
    url_result_base = 'https://www.racingpost.com/results'
    url_api_base = 'https://www.racingpost.com/api/course-profile/results'

    urls: set[str] = set()

    for course_id, course in tracks:
        course_slug = course.lower().replace(' ', '-').replace("'", '')

        for year in years:
            query = urlencode(
                {
                    'courseId': course_id,
                    'season': year,
                    'raceType': race_type,
                    'raceCategory': 'all-races',
                }
            )
            url = f'{url_api_base}?{query}'

            sleep(DISCOVERY_DELAY)
            status, response = client.get(url)

            if status != 200:
                print(f'Failed to get race urls.\nStatus: {status}, URL: {url}')
                sys.exit(1)

            rows = (
                loads(response.content)
                .get('results', {})
                .get('data', {})
                .get('principle_race_results')
                or []
            )

            for row in rows:
                race_date = row['race_datetime'][:10]
                race_id = row['race_instance_uid']
                urls.add(f'{url_result_base}/{course_id}/{course_slug}/{race_date}/{race_id}')

    return sorted(urls, key=sort_key)


def get_race_urls_date(
    dates: list[date], tracks: list[tuple[str, str]], client: NetworkClient
) -> list[str]:
    urls: set[str] = set()
    course_ids: set[str] = {t[0] for t in tracks}

    for race_date in dates:
        sleep(DISCOVERY_DELAY)
        url = f'https://www.racingpost.com/results/{race_date}'

        _, response = client.get(url)

        meetings = (
            page_props(response.text).get('initialState', {}).get('results', {}).get('data') or []
        )

        for meeting in meetings:
            if str(meeting.get('courseId')) not in course_ids:
                continue

            for race in meeting.get('races') or []:
                link = race.get('fullResultLink')
                if link:
                    urls.add(f'https://www.racingpost.com{link}')

    return sorted(urls, key=sort_key)


def load_or_save_urls(path: Path, builder: Callable[[], list[str]]) -> list[str]:
    if path.exists():
        return [line.strip() for line in path.read_text().splitlines() if line.strip()]

    urls = builder()
    _ = path.write_text('\n'.join(urls))

    return urls


def prepare_betfair(
    race_urls: list[str],
    paths: Paths,
) -> 'Betfair | None':
    if not settings.toml or not settings.toml.get('betfair_data', False):
        return None

    paths.betfair.parent.mkdir(parents=True, exist_ok=True)

    from utils.betfair import Betfair

    print('Getting Betfair data...')

    if paths.betfair.exists():
        print('Using cached Betfair data')
        return Betfair.from_csv(paths.betfair)

    betfair = Betfair(race_urls)

    with open(str(paths.betfair), 'w') as f:
        fields = settings.toml.get('fields', {}).get('betfair', {})
        header = ','.join(['date', 'region', 'off', 'horse'] + list(fields.keys()))
        _ = f.write(header + '\n')

        for row in betfair.rows:
            values = ['' if v is None else str(v) for v in row.to_dict().values()]
            _ = f.write(','.join(values) + '\n')

    return betfair


def get_race_data(url: str, client: NetworkClient, retries: int = THROTTLE_ROUNDS) -> dict | None:
    for attempt in range(retries):
        try:
            _, response = client.get(url)
        except Persistent406Error:
            if attempt == retries - 1:
                raise

            print(f'Throttled (406) on {url}, pausing {THROTTLE_DELAY:.0f}s')
            sleep(THROTTLE_DELAY)
            continue

        data = race_result(response.text)

        if data:
            return data

    return None


def scrape_races(
    race_urls: list[str],
    paths: Paths,
    race_type: str,
    client: NetworkClient,
    file_writer: Callable[[str, bool], TextIO],
    limit: int | None = None,
):
    from utils.race import Race, VoidRaceError

    betfair = prepare_betfair(
        race_urls=race_urls,
        paths=paths,
    )

    last_url = paths.progress.read_text().strip() if paths.progress.exists() else None

    if last_url:
        try:
            race_urls = race_urls[race_urls.index(last_url) + 1 :]
            print(f'Resuming after {last_url}')
        except ValueError:
            pass
    else:
        print('Scraping races')

    append = last_url is not None and paths.output.exists()

    with file_writer(str(paths.output), append=append) as f:
        if not append:
            _ = f.write(settings.csv_header + '\n')

        for index, url in enumerate(race_urls, start=1):
            if limit is not None and index > limit:
                print(f'Reached limit of {limit} races; stopping (rerun to resume)')
                break

            sleep(RACE_DELAY)

            if index % CHUNK_SIZE == 0:
                print(f'Pausing {CHUNK_PAUSE:.0f}s after {index} races to avoid throttling')
                sleep(CHUNK_PAUSE)

            data = get_race_data(url, client)

            try:
                race = (
                    Race(data, settings.fields, betfair.data)
                    if betfair
                    else Race(data, settings.fields)
                )
            except VoidRaceError:
                continue

            allowed = RACE_TYPES.get(race_type)
            if allowed is not None and race.race_info.race_type not in allowed:
                continue

            for row in race.csv_data:
                _ = f.write(row + '\n')

            _ = paths.progress.write_text(url)

    print('Finished scraping.')
    print(f'OUTPUT_CSV={paths.output.resolve()}')


def writer_csv(file_path: str, append: bool = False) -> TextIO:
    return open(file_path, 'a' if append else 'w', encoding='utf-8')


def writer_gzip(file_path: str, append: bool = False) -> TextIO:
    mode = 'at' if append else 'wt'
    return gzip.open(file_path, mode, encoding='utf-8')


def main():
    if settings.toml is None:
        sys.exit()

    if settings.toml['auto_update']:
        _ = check_for_update()

    gzip_output = settings.toml.get('gzip_output', False)
    file_writer = writer_gzip if gzip_output else writer_csv

    parser = ArgParser()

    if len(sys.argv) <= 1:
        parser.parser.print_help()
        sys.exit(2)

    args = parser.parse(sys.argv[1:])
    paths = build_paths(args.request, gzip_output)

    client = NetworkClient(
        email=os.getenv('EMAIL'),
        auth_state=os.getenv('AUTH_STATE'),
        access_token=os.getenv('ACCESS_TOKEN'),
    )

    if args.dates != []:
        race_urls = load_or_save_urls(
            paths.urls,
            lambda: get_race_urls_date(args.dates, args.tracks, client),
        )

    else:
        race_urls = load_or_save_urls(
            paths.urls,
            lambda: get_race_urls(args.years, args.tracks, args.race_type, client),
        )

    scrape_races(race_urls, paths, args.race_type, client, file_writer, limit=args.limit)


if __name__ == '__main__':
    main()
