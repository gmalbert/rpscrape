from orjson import loads
from re import DOTALL, search

NEXT_DATA_PATTERN = r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>'


def extract_next_data(text: str) -> dict | None:
    match = search(NEXT_DATA_PATTERN, text, DOTALL)

    if match is None:
        return None

    try:
        return loads(match.group(1))
    except ValueError:
        return None


def page_props(text: str) -> dict:
    data = extract_next_data(text)

    if not data:
        return {}

    return data.get('props', {}).get('pageProps', {})


def race_result(text: str) -> dict | None:
    return page_props(text).get('initialState', {}).get('raceResult', {}).get('data')
