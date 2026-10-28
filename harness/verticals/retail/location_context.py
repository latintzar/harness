"""Weather and the neighbourhood map of the session's location.

Peter approved two outside services on 10 October 2026: Open-Meteo for the forecast and the
OpenStreetMap tile server for the map. This server calls both and sends the shop's coordinates
and nothing else. The page keeps its closed content policy and reads two routes of ours:

    GET /api/control-room/weather   the place, its city, the current hour, 24 hours, 7 days
    GET /api/control-room/map       one PNG of the streets around the shop, credit drawn in

Bounds, recorded in docs/COST-SAFETY.md. Weather: one answer kept 15 minutes per location, at
most 200 upstream calls per UTC day for the whole service, five seconds per call, the saved
answer served for up to six hours when the upstream fails. Map: zoom 16, a 3x3 tile block, the
picture kept 30 days per location, at most 40 tile fetches per UTC day, two at a time. The day's
counts live in one ledger shared by every worker process and kept across restarts; a ledger that
cannot be read admits no call. ROCOTO_RETAIL_LOCATION_CONTEXT=off stops every upstream call and
leaves saved answers in service.

A commissioned map provider draws the picture in place of the tiles: Mapbox when
ROCOTO_RETAIL_MAPBOX_TOKEN is set, Google when ROCOTO_RETAIL_GOOGLE_MAPS_API_KEY is set (Peter,
11 October 2026). One call returns one picture of one shape, kept for as long as the provider's own
answer says it is valid (12 hours where it says nothing, one hour at least, 30 days at most). Every
server process of this host's user counts those calls in one commissioned ledger: at most 60 per UTC
day and 1,000 per UTC month. A full, missing or damaged ledger, a clock behind the ledger, or a
provider that is away hands the map back to the tiles. The key is read once at start, leaves this
process's environment, travels in the request to the provider and reaches no page, log line, child
process or answer of this service.

    python location_context.py commission-map-ledger    make this host's ledger (once per host user)
"""
from __future__ import annotations
from retail_settings import value as setting

import layout

import asyncio
from bisect import bisect_left
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

HERE = Path(__file__).parent
SCHEMA = 'rocoto.location-weather.v1'
WEATHER_URL = 'https://api.open-meteo.com/v1/forecast'
TILE_URL = 'https://tile.openstreetmap.org/{zoom}/{x}/{y}.png'
# OpenStreetMap's tile policy asks every client to name itself and a way to reach its operator.
USER_AGENT = 'Rocoto/1.0 (+https://soyrocoto.com)'
ATTRIBUTION = '© OpenStreetMap contributors'
ATTRIBUTION_SHORT = '© OpenStreetMap'
ATTRIBUTION_FONT = HERE / 'web/control-room/kit/inter-latin-500-normal.woff2'

TIMEOUT_S = 5
WEATHER_TTL_S = 15 * 60
WEATHER_STALE_S = 6 * 3600
WEATHER_DAILY_CALLS = 200
WEATHER_BYTES = 256 * 1024
FORECAST_DAYS = 8  # seven whole days stay ahead of a saved answer that crosses midnight
MAP_TTL_S = 30 * 24 * 3600
TILE_DAILY_FETCHES = 40
TILE_BYTES = 512 * 1024
RETRY_S = 5 * 60  # a failed upstream rests this long before the next call
ZOOM, GRID, TILE, MAP_SIZE = 16, 3, 256, 512

# The commissioned provider's picture: one call per shape, drawn at two pixels per point.
MAPBOX_URL = ('https://api.mapbox.com/styles/v1/mapbox/light-v11/static/'
              '{longitude:.5f},{latitude:.5f},{zoom},0/{width}x{height}@2x')
GOOGLE_URL = 'https://maps.googleapis.com/maps/api/staticmap'
GOOGLE_STYLE = ('feature:all|saturation:-70', 'feature:poi|visibility:off', 'feature:transit|visibility:off')
PROVIDERS = (('mapbox', 'ROCOTO_RETAIL_MAPBOX_TOKEN'), ('google', 'ROCOTO_RETAIL_GOOGLE_MAPS_API_KEY'))
PICTURE_REVISION = {'mapbox': 'light-v11@2x', 'google': 'roadmap-calm@2x'}
PICTURE_DAILY_CALLS = 60
PICTURE_MONTHLY_CALLS = 1000
PICTURE_BYTES = 3 * 1024 * 1024
PICTURE_VALID_S = 12 * 3600       # where the provider's answer names no validity
PICTURE_VALID_RANGE_S = (3600, 30 * 24 * 3600)
PICTURE_STALE_S = 7 * 24 * 3600   # an older picture still serves this long while the provider is away
REFUSED_REST_S = {401: 6 * 3600, 403: 6 * 3600, 429: 3600}  # a refused key rests the provider as a whole

# httpx names each request line at INFO; that line would carry the provider key.
logging.getLogger('httpx').setLevel(logging.WARNING)
MAP_SIZES = frozenset(range(128, MAP_SIZE + 1, 32))
MERCATOR_LATITUDE = 85.05112878
LOCATIONS = 256

CURRENT = 'temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day'
HOURLY = 'temperature_2m,precipitation_probability,weather_code,is_day'
DAILY = 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset'

# WMO weather interpretation codes as Open-Meteo reports them: words, day icon, night icon.
CONDITIONS = {
    0: ('Clear', 'sun', 'moon'), 1: ('Mostly clear', 'sun', 'moon'),
    2: ('Partly cloudy', 'cloud-sun', 'cloud-moon'), 3: ('Cloudy', 'cloudy', 'cloudy'),
    45: ('Fog', 'cloud-fog', 'cloud-fog'), 48: ('Fog', 'cloud-fog', 'cloud-fog'),
    51: ('Drizzle', 'cloud-drizzle', 'cloud-drizzle'), 53: ('Drizzle', 'cloud-drizzle', 'cloud-drizzle'),
    55: ('Drizzle', 'cloud-drizzle', 'cloud-drizzle'), 56: ('Freezing drizzle', 'cloud-drizzle', 'cloud-drizzle'),
    57: ('Freezing drizzle', 'cloud-drizzle', 'cloud-drizzle'),
    61: ('Light rain', 'cloud-rain', 'cloud-rain'), 63: ('Rain', 'cloud-rain', 'cloud-rain'),
    65: ('Heavy rain', 'cloud-rain', 'cloud-rain'), 66: ('Freezing rain', 'cloud-rain', 'cloud-rain'),
    67: ('Freezing rain', 'cloud-rain', 'cloud-rain'),
    71: ('Light snow', 'cloud-snow', 'cloud-snow'), 73: ('Snow', 'cloud-snow', 'cloud-snow'),
    75: ('Heavy snow', 'cloud-snow', 'cloud-snow'), 77: ('Snow', 'cloud-snow', 'cloud-snow'),
    80: ('Light showers', 'cloud-rain', 'cloud-rain'), 81: ('Showers', 'cloud-rain', 'cloud-rain'),
    82: ('Heavy showers', 'cloud-rain', 'cloud-rain'),
    85: ('Snow showers', 'cloud-snow', 'cloud-snow'), 86: ('Snow showers', 'cloud-snow', 'cloud-snow'),
    95: ('Thunderstorm', 'cloud-lightning', 'cloud-lightning'), 96: ('Thunderstorm', 'cloud-lightning', 'cloud-lightning'),
    99: ('Thunderstorm', 'cloud-lightning', 'cloud-lightning'),
}


class Unavailable(Exception):
    """The upstream gave no usable answer and this location holds no saved one."""


class Refused(ValueError):
    """The upstream answered with a status other than 200."""

    def __init__(self, status):
        super().__init__('The upstream answered outside its contract.')
        self.status = status


def context_enabled(environ=None):
    value = setting('ROCOTO_RETAIL_LOCATION_CONTEXT', 'on', environ=environ).strip().lower()
    if value not in {'on', 'off'}:
        raise ValueError('Set ROCOTO_RETAIL_LOCATION_CONTEXT to on or off.')
    return value == 'on'


def map_provider(environ=None):
    """The commissioned map provider and its key: Mapbox first, then Google; None keeps the tiles."""
    environ = os.environ if environ is None else environ
    for name, variable in PROVIDERS:
        secret = setting(variable, '', environ=environ).strip()
        if secret:
            if not re.fullmatch(r'[A-Za-z0-9._-]{20,400}', secret):
                raise ValueError(f'Set {variable} to the provider key.')
            return name, secret
    return None


def claim_provider(environ=None):
    """Read the provider key once and take both names out of the environment, so no child process inherits it."""
    environ = os.environ if environ is None else environ
    from retail_settings import remove
    try:
        return map_provider(environ)
    finally:
        for _, variable in PROVIDERS:
            remove(variable, environ=environ)


def host_ledger(environ=None):
    """The provider-call ledger every server process of this host's user shares; None where the user has no home."""
    environ = os.environ if environ is None else environ
    state = environ.get('XDG_STATE_HOME', '').strip()
    if not os.path.isabs(state):  # the XDG rule: an empty or relative value is set aside
        try:
            state = str(Path.home() / '.local' / 'state')
        except (RuntimeError, KeyError):
            return None
    return Path(state) / 'rocoto' / 'location-context' / 'map-pictures.json'


def picture_request(provider, secret, place, width, height):
    """The one outside request for a picture: the shop's coordinates, the shape, the key."""
    latitude, longitude = float(place['latitude']), float(place['longitude'])
    if provider == 'mapbox':
        # Mapbox counts zoom on 512-pixel tiles: one level less shows the same streets.
        return (MAPBOX_URL.format(longitude=longitude, latitude=latitude, zoom=ZOOM - 1, width=width, height=height),
                {'access_token': secret})
    if provider == 'google':
        return GOOGLE_URL, {'center': f'{latitude:.5f},{longitude:.5f}', 'zoom': ZOOM, 'size': f'{width}x{height}',
                            'scale': 2, 'maptype': 'roadmap', 'style': list(GOOGLE_STYLE), 'key': secret}
    raise ValueError('Choose a commissioned map provider.')


def provider_png(body, width, height):
    """The provider's answer as one plain PNG of the asked shape at two pixels per point."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(body)) as source:
            # The declared format and size are read before anything is decoded.
            if source.format not in {'PNG', 'JPEG'} or source.size != (2 * width, 2 * height):
                raise ValueError('Use the provider picture of the asked shape.')
            clean = source.convert('RGB')
    except ValueError:
        raise
    except Exception as fault:  # a header that declares an enormous picture, or a decoder fault
        raise ValueError('Use the provider picture of the asked shape.') from fault
    clean.info.clear()  # the provider's colour profile and colour key stay out of the kept file
    output = io.BytesIO()
    clean.save(output, 'PNG', optimize=True)
    return output.getvalue()


def kept_provider_png(picture, width, height):
    """True for a kept file that is still the PNG this service saved for that shape."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(picture)) as source:
            return source.format == 'PNG' and source.size == (2 * width, 2 * height)
    except Exception:
        return False


def valid_for(headers):
    """How long the provider says its picture stays valid, inside this service's bounds."""
    named = re.search(r'(?:^|[,\s])max-age=(\d{1,9})', headers.get('cache-control', ''))
    low, high = PICTURE_VALID_RANGE_S
    return min(max(int(named.group(1)), low), high) if named else PICTURE_VALID_S


class ProviderLedger:
    """Provider calls of this host's user, counted before they leave: per UTC day and per UTC month.

    One private file for every server process. Counts are kept per period, so a clock that moves away
    and returns finds its count. The commissioning command makes the file; serving never does. A
    missing, unreadable or damaged ledger, or a clock that reads earlier than a period already
    counted, admits no call.
    """
    DAYS, MONTHS = 62, 14  # periods kept in the file

    def __init__(self, path, daily, monthly, *, clock=time.time):
        self.path = Path(path).absolute() if path else None
        self.daily, self.monthly, self.clock = daily, monthly, clock

    def _now(self):
        day = datetime.fromtimestamp(self.clock(), timezone.utc).strftime('%Y-%m-%d')
        return day, day[:7]

    @staticmethod
    def _counts(table, stamp):
        if not isinstance(table, dict) or any(
                not isinstance(period, str) or not re.fullmatch(stamp, period) or type(count) is not int or count < 0
                for period, count in table.items()):
            raise ValueError('Restore the provider-call ledger.')
        return table

    def _write(self, ledger):
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.ledger-')
        with os.fdopen(handle, 'w') as stream:
            json.dump(ledger, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @contextmanager
    def _ledger(self, write):
        if self.path is None or self.path.is_symlink() or not self.path.is_file():
            raise ValueError('Commission the provider-call ledger.')
        with open(os.open(self.path.with_name(self.path.name + '.lock'), os.O_CREAT | os.O_RDWR, 0o600), 'r+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = json.loads(self.path.read_text())
            if not isinstance(ledger, dict) or set(ledger) != {'days', 'months'}:
                raise ValueError('Restore the provider-call ledger.')
            days = self._counts(ledger['days'], r'\d{4}-\d{2}-\d{2}')
            months = self._counts(ledger['months'], r'\d{4}-\d{2}')
            before = json.dumps(ledger, sort_keys=True)
            yield days, months
            if write and json.dumps(ledger, sort_keys=True) != before:
                for table, kept in ((days, self.DAYS), (months, self.MONTHS)):
                    for period in sorted(table)[:-kept]:
                        del table[period]
                self._write(ledger)

    def commission(self):
        """Make the ledger where none stands. True when this call made it; a standing ledger is left as it is."""
        if self.path is None:
            raise ValueError('The provider-call ledger has no place.')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(os.open(self.path.with_name(self.path.name + '.lock'), os.O_CREAT | os.O_RDWR, 0o600), 'r+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.exists() or self.path.is_symlink():
                return False
            self._write({'days': {}, 'months': {}})
            return True

    def reserve(self, count=1):
        """Count these calls against the day and the month before they leave. False admits none of them."""
        try:
            with self._ledger(True) as (days, months):
                day, month = self._now()
                if any(period > day for period in days) or any(period > month for period in months):
                    return False  # the clock reads earlier than a period already counted
                if days.get(day, 0) + count > self.daily or months.get(month, 0) + count > self.monthly:
                    return False
                days[day], months[month] = days.get(day, 0) + count, months.get(month, 0) + count
                return True
        except (OSError, ValueError):
            return False

    def used(self):
        """Today's and this month's counts; a ledger that cannot be read reads as full."""
        try:
            with self._ledger(False) as (days, months):
                day, month = self._now()
                return days.get(day, 0), months.get(month, 0)
        except (OSError, ValueError):
            return self.daily, self.monthly


class DailyBudget:
    """Upstream calls per UTC day. One private ledger for every worker process, kept across restarts."""

    def __init__(self, path, caps, *, clock=time.time):
        self.path = Path(path).absolute() if path else None
        self.caps = dict(caps)
        self.clock = clock
        self._memory = {}

    def _day(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime('%Y-%m-%d')

    def _today(self, ledger):
        if ledger.get('day') != self._day():
            ledger.clear()
            ledger.update(day=self._day(), used={})
        used = ledger.get('used')
        if not isinstance(used, dict) or any(type(count) is not int or count < 0 for count in used.values()):
            raise ValueError('Restore the outside-call ledger.')
        return used

    @contextmanager
    def _ledger(self, write):
        if self.path is None:
            yield self._memory
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(os.open(self.path.with_name(self.path.name + '.lock'), os.O_CREAT | os.O_RDWR, 0o600), 'r+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = json.loads(self.path.read_text()) if self.path.exists() else {}
            if not isinstance(ledger, dict):
                raise ValueError('Restore the outside-call ledger.')
            before = json.dumps(ledger, sort_keys=True)
            yield ledger
            if write and json.dumps(ledger, sort_keys=True) != before:
                handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.budget-')
                with os.fdopen(handle, 'w') as stream:
                    json.dump(ledger, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)

    def reserve(self, kind, count=1):
        """Count these calls against today's cap before they leave. False admits none of them."""
        try:
            with self._ledger(True) as ledger:
                used = self._today(ledger)
                if used.get(kind, 0) + count > self.caps[kind]:
                    return False
                used[kind] = used.get(kind, 0) + count
                return True
        except (OSError, ValueError):
            return False

    def used(self, kind):
        """Today's count; an unreadable ledger reads as a full day."""
        try:
            with self._ledger(False) as ledger:
                return self._today(ledger).get(kind, 0)
        except (OSError, ValueError):
            return self.caps[kind]


def _number(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('Use a measured weather value.')
    return value


def _optional(value, low, high):
    return None if value is None else _number(value, low, high)


def _stamp(value, pattern):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError('Use the upstream local time.')
    return value


HOUR = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}'
DAY = r'\d{4}-\d{2}-\d{2}'


def checked(raw):
    """The upstream answer reduced to the measured values this product shows, each one in range."""
    if not isinstance(raw, dict):
        raise ValueError('Use the upstream forecast.')
    current, hourly, daily = raw.get('current'), raw.get('hourly'), raw.get('daily')
    if not all(isinstance(part, dict) for part in (current, hourly, daily)):
        raise ValueError('Use the upstream forecast.')
    offset = raw.get('utc_offset_seconds')
    if type(offset) is not int or abs(offset) > 16 * 3600:
        raise ValueError('Use the upstream local clock.')
    hours, days = hourly.get('time'), daily.get('time')
    if not isinstance(hours, list) or not isinstance(days, list) or not 24 <= len(hours) <= 400 or not 1 <= len(days) <= 16:
        raise ValueError('Use the upstream forecast.')

    def column(table, name, count, convert):
        values = table.get(name)
        if not isinstance(values, list) or len(values) != count:
            raise ValueError('Use the upstream forecast.')
        return [convert(value) for value in values]

    def code(value):
        if type(value) is not int or value not in CONDITIONS:
            raise ValueError('Use a known weather code.')
        return value

    def flag(value):
        if value not in (0, 1):
            raise ValueError('Use the upstream day flag.')
        return int(value)

    degrees = lambda value: _number(value, -90, 65)
    chance = lambda value: _optional(value, 0, 100)
    return {
        'utc_offset_seconds': offset,
        'timezone': raw.get('timezone') if isinstance(raw.get('timezone'), str) else None,
        'current': {
            'time': _stamp(current.get('time'), HOUR), 'temperature': degrees(current.get('temperature_2m')),
            'feels_like': _optional(current.get('apparent_temperature'), -110, 80),
            'humidity': _optional(current.get('relative_humidity_2m'), 0, 100),
            'wind': _optional(current.get('wind_speed_10m'), 0, 500),
            'code': code(current.get('weather_code')), 'is_day': flag(current.get('is_day', 1))},
        'hourly': {
            'time': [_stamp(value, HOUR) for value in hours],
            'temperature': column(hourly, 'temperature_2m', len(hours), degrees),
            'rain': column(hourly, 'precipitation_probability', len(hours), chance),
            'code': column(hourly, 'weather_code', len(hours), code),
            'is_day': column(hourly, 'is_day', len(hours), flag)},
        'daily': {
            'time': [_stamp(value, DAY) for value in days],
            'code': column(daily, 'weather_code', len(days), code),
            'high': column(daily, 'temperature_2m_max', len(days), degrees),
            'low': column(daily, 'temperature_2m_min', len(days), degrees),
            'rain': column(daily, 'precipitation_probability_max', len(days), chance),
            'sunrise': column(daily, 'sunrise', len(days), lambda value: _stamp(value, HOUR)),
            'sunset': column(daily, 'sunset', len(days), lambda value: _stamp(value, HOUR))},
    }


def condition(code, is_day=True):
    words, day, night = CONDITIONS[code]
    return words, day if is_day else night


def city_of(address, zone=None):
    """The city a person would say for this address. Stored text only: no lookup."""
    words = lambda part: ' '.join(word for word in part.split() if not any(character.isdigit() for character in word))
    parts = [part for part in map(words, str(address or '').split(',')[1:]) if part]
    named = str(zone).rsplit('/', 1)[-1].replace('_', ' ') if zone and '/' in str(zone) else ''
    for part in parts:
        if named and part.casefold() == named.casefold():
            return part
    return parts[0] if parts else named


def forecast(saved, place, now, fetched, *, stale=False):
    """The answer the page reads, cut to the hours and days ahead of this moment at the shop."""
    local = datetime.fromtimestamp(now, timezone.utc) + timedelta(seconds=saved['utc_offset_seconds'])
    hourly, daily = saved['hourly'], saved['daily']
    first = bisect_left(hourly['time'], local.strftime('%Y-%m-%dT%H:00'))
    today = bisect_left(daily['time'], local.strftime('%Y-%m-%d'))
    if first + 24 > len(hourly['time']) or today >= len(daily['time']):
        raise ValueError('The saved forecast ends before this hour.')

    def hour(index):
        words, icon = condition(hourly['code'][index], hourly['is_day'][index])
        return {'time': hourly['time'][index], 'temperature_c': hourly['temperature'][index], 'condition': words,
                'icon': icon, 'rain_chance_percent': hourly['rain'][index], 'is_day': bool(hourly['is_day'][index])}

    def day(index):
        words, icon = condition(daily['code'][index])
        date = daily['time'][index]
        return {'date': date, 'weekday': datetime.strptime(date, '%Y-%m-%d').strftime('%A'),
                'high_c': daily['high'][index], 'low_c': daily['low'][index], 'condition': words, 'icon': icon,
                'rain_chance_percent': daily['rain'][index],
                'sunrise': daily['sunrise'][index][11:], 'sunset': daily['sunset'][index][11:]}

    hours = [hour(index) for index in range(first, first + 24)]
    measured = saved['current']
    if now - fetched <= 3600:
        words, icon = condition(measured['code'], measured['is_day'])
        current = {'temperature_c': measured['temperature'], 'condition': words, 'icon': icon,
                   'is_day': bool(measured['is_day']), 'feels_like_c': measured['feels_like'],
                   'wind_kmh': measured['wind'], 'humidity_percent': measured['humidity']}
        observed = measured['time']
    else:
        # An older saved answer speaks through its own forecast for this hour.
        current = {key: hours[0][key] for key in ('temperature_c', 'condition', 'icon', 'is_day')}
        current.update(feels_like_c=None, wind_kmh=None, humidity_percent=None)
        observed = hours[0]['time']
    name, address = place.get('name') or '', place.get('address') or ''
    return {'schema': SCHEMA, 'location_id': place.get('location_id'), 'place': name, 'address': address,
            'city': city_of(address, saved['timezone'] or place.get('timezone')) or name,
            'timezone': saved['timezone'] or place.get('timezone'), 'observed_at': observed, 'stale': stale,
            'current': current, 'hourly': hours, 'daily': [day(index) for index in range(today, min(today + 7, len(daily['time'])))]}


def tile_position(latitude, longitude, zoom=ZOOM):
    """Fractional slippy-map tile coordinates of a point (Web Mercator)."""
    latitude = max(-MERCATOR_LATITUDE, min(MERCATOR_LATITUDE, latitude))
    scale = 2 ** zoom
    x = (longitude + 180) / 360 * scale
    y = (1 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2 * scale
    return min(max(x, 0), scale - 1e-9), min(max(y, 0), scale - 1e-9)


def tile_block(latitude, longitude, zoom=ZOOM):
    """The 3x3 tiles around the shop and the shop's pixel inside that block."""
    x, y = tile_position(latitude, longitude, zoom)
    scale, column, row = 2 ** zoom, int(x), int(y)
    row = min(max(row, 1), scale - 2)  # the block stays on the map at its top and bottom edge
    tiles = {(dx, dy): ((column + dx - 1) % scale, row + dy - 1) for dx in range(GRID) for dy in range(GRID)}
    return tiles, ((x - column + 1) * TILE, (y - row + 1) * TILE)


def compose(tiles, point):
    """One square picture centred on the shop, cut from the 3x3 block."""
    from PIL import Image
    sheet = Image.new('RGB', (GRID * TILE, GRID * TILE))
    for (column, row), content in tiles.items():
        with Image.open(io.BytesIO(content)) as tile:
            if tile.format != 'PNG' or tile.size != (TILE, TILE):
                raise ValueError('Use a standard map tile.')
            sheet.paste(tile.convert('RGB'), (column * TILE, row * TILE))
    left = min(max(round(point[0]) - MAP_SIZE // 2, 0), GRID * TILE - MAP_SIZE)
    top = min(max(round(point[1]) - MAP_SIZE // 2, 0), GRID * TILE - MAP_SIZE)
    output = io.BytesIO()
    sheet.crop((left, top, left + MAP_SIZE, top + MAP_SIZE)).save(output, 'PNG', optimize=True)
    return output.getvalue()


def theme_tone(path=None):
    """The map tone the theme names (saturate, contrast, brightness), for this service to bake into the tiles' picture."""
    try:
        theme = json.loads(Path(path or layout.ROOT / 'theme.json').read_text())
    except (OSError, ValueError):
        return None

    def find(node):
        if isinstance(node, dict):
            if isinstance(node.get('weather-map-tone'), str):
                return node['weather-map-tone']
            for value in node.values():
                found = find(value)
                if found:
                    return found
        return None
    named = re.fullmatch(r'saturate\(([\d.]+)\) contrast\(([\d.]+)\) brightness\(([\d.]+)\)', find(theme) or '')
    return tuple(float(value) for value in named.groups()) if named else None


MAP_TONE = theme_tone()


def toned(image):
    """The tiles' picture in the theme's quiet tone; the page shows every map picture as it arrives."""
    if not MAP_TONE:
        return image
    from PIL import ImageEnhance
    saturation, contrast, brightness = MAP_TONE
    return ImageEnhance.Brightness(ImageEnhance.Contrast(ImageEnhance.Color(image).enhance(saturation)).enhance(contrast)).enhance(brightness)


def credited(picture, width=MAP_SIZE, height=MAP_SIZE):
    """The picture at the asked shape, the shop still at its centre, the map's credit in its corner."""
    from PIL import Image, ImageDraw, ImageFont
    with Image.open(io.BytesIO(picture)) as source:
        base = source.convert('RGB')
    left, top = (base.width - width) // 2, (base.height - height) // 2
    image = toned(base.crop((left, top, left + width, top + height))).convert('RGBA')
    pad_x, pad_y, inset = 10, 6, 16  # the credit stays whole when a frame trims the picture's edge

    def lettering(size):
        try:
            return ImageFont.truetype(str(ATTRIBUTION_FONT), size)
        except OSError:
            return ImageFont.load_default(size)
    layer = Image.new('RGBA', image.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(layer)
    font, credit = lettering(19), ATTRIBUTION
    box = draw.textbbox((0, 0), credit, font=font)
    room = width - 2 * inset - 2 * pad_x
    if box[2] - box[0] > room:
        # A narrow picture takes the credit whole in smaller lettering, in its short form where that reads better.
        if 19 * room / (box[2] - box[0]) < 12:
            credit = ATTRIBUTION_SHORT
            box = draw.textbbox((0, 0), credit, font=font)
        font = lettering(max(8, int(19 * room / (box[2] - box[0]))))
        box = draw.textbbox((0, 0), credit, font=font)
    text_width, text_height = box[2] - box[0], box[3] - box[1]
    right, bottom = width - inset, height - inset
    draw.rounded_rectangle((right - text_width - 2 * pad_x, bottom - text_height - 2 * pad_y, right, bottom),
                           radius=(text_height + 2 * pad_y) // 2, fill=(255, 255, 255, 214))
    draw.text((right - pad_x - text_width - box[0], bottom - pad_y - text_height - box[1]), credit,
              font=font, fill=(38, 39, 34, 255))
    output = io.BytesIO()
    Image.alpha_composite(image, layer).convert('RGB').save(output, 'PNG', optimize=True)
    return output.getvalue()


class LocationContext:
    """One service for the whole application: saved answers, the day's ledger, the outside transport."""

    def __init__(self, directory=None, *, transport=None, clock=time.time, enabled=None, provider=None,
                 picture_ledger=None):
        self.directory = Path(directory).absolute() if directory else None
        self.transport = transport
        self.clock = clock
        self.enabled = context_enabled() if enabled is None else bool(enabled)
        self.budget = DailyBudget(self.directory / 'budget.json' if self.directory else None,
                                  {'weather': WEATHER_DAILY_CALLS, 'tiles': TILE_DAILY_FETCHES}, clock=clock)
        # provider: None reads the environment, False keeps the tiles, (name, key) names one.
        self.provider = map_provider() if provider is None else (tuple(provider) if provider else None)
        # picture_ledger: None keeps the count in this service's directory, False names no ledger.
        ledger = (self.directory / 'map-pictures.json' if self.directory else None) if picture_ledger is None else (picture_ledger or None)
        self.pictures = ProviderLedger(ledger, PICTURE_DAILY_CALLS, PICTURE_MONTHLY_CALLS, clock=clock)
        if ledger is None:
            self.provider = None  # a provider call is counted in a kept ledger or stays unmade
        self._provider_rests_until = -math.inf
        self._weather = OrderedDict()
        self._pictures = OrderedDict()
        self._drawn = OrderedDict()
        self._failed = {}
        self._locks = {}

    @staticmethod
    def key(place):
        return f"{round(float(place['latitude']), 4):.4f},{round(float(place['longitude']), 4):.4f}"

    def _lock(self, kind, key):
        return self._locks.setdefault((kind, key), asyncio.Lock())

    def _resting(self, kind, key):
        return self.clock() - self._failed.get((kind, key), -math.inf) < RETRY_S

    def _keep(self, table, key, value, limit=LOCATIONS):
        table[key] = value
        table.move_to_end(key)
        while len(table) > limit:
            table.popitem(last=False)

    async def _get(self, client, url, *, params=None, limit):
        """One bounded read: a 200 answer of at most `limit` bytes."""
        async with client.stream('GET', url, params=params) as response:
            if response.status_code != 200:
                raise Refused(response.status_code)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    raise ValueError('The upstream answer exceeds its budget.')
            return response.headers, bytes(body)

    def _client(self):
        import httpx
        return httpx.AsyncClient(transport=self.transport, timeout=TIMEOUT_S, follow_redirects=False,
                                 headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'gzip'})

    async def _fetch_weather(self, place, key):
        import httpx
        if not self.enabled or self._resting('weather', key) or not self.budget.reserve('weather'):
            return None
        latitude, longitude = key.split(',')
        try:
            async with asyncio.timeout(TIMEOUT_S):
                async with self._client() as client:
                    _, body = await self._get(client, WEATHER_URL, limit=WEATHER_BYTES, params={
                        'latitude': latitude, 'longitude': longitude, 'current': CURRENT, 'hourly': HOURLY,
                        'daily': DAILY, 'timezone': 'auto', 'forecast_days': str(FORECAST_DAYS)})
            return checked(json.loads(body))
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
            self._failed['weather', key] = self.clock()
            return None

    async def weather(self, place):
        key = self.key(place)

        def answer(saved, *, stale=False):
            fetched, values = saved
            try:
                return forecast(values, place, self.clock(), fetched, stale=stale)
            except ValueError:
                return None

        def fresh():
            saved = self._weather.get(key)
            return answer(saved) if saved and self.clock() - saved[0] < WEATHER_TTL_S else None

        result = fresh()
        if result:
            return result
        async with self._lock('weather', key):
            result = fresh()
            if result:
                return result
            values = await self._fetch_weather(place, key)
            if values is not None:
                self._keep(self._weather, key, (self.clock(), values))
                result = answer(self._weather[key])
                if result:
                    return result
        saved = self._weather.get(key)
        if saved and self.clock() - saved[0] < WEATHER_STALE_S:
            result = answer(saved, stale=True)
            if result:
                return result
        raise Unavailable('weather')

    def _picture_path(self, key):
        name = hashlib.sha256(f'{ZOOM}:{GRID}:{MAP_SIZE}:{key}'.encode()).hexdigest()[:32]
        return self.directory / f'map-{name}.png' if self.directory else None

    def _saved_picture(self, key):
        return self._saved(key, self._picture_path(key))

    def _save_picture(self, key, picture):
        self._save(key, self._picture_path(key), picture)

    def _saved(self, key, path):
        held = self._pictures.get(key)
        if held:
            return held
        try:
            if path is None or path.is_symlink() or not path.is_file():
                return None
            held = (path.stat().st_mtime, path.read_bytes())
        except OSError:
            return None
        self._keep(self._pictures, key, held, limit=48)
        return held

    def _save(self, key, path, picture, stamp=None):
        stamp = self.clock() if stamp is None else stamp
        self._keep(self._pictures, key, (stamp, picture), limit=48)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle, temporary = tempfile.mkstemp(dir=path.parent, prefix='.map-')
            with os.fdopen(handle, 'wb') as stream:
                stream.write(picture)
                stream.flush()
                os.fsync(stream.fileno())
            os.utime(temporary, (stamp, stamp))
            os.replace(temporary, path)
        except OSError:
            pass  # the picture stays in memory for this process

    async def _fetch_picture(self, place, key):
        import httpx
        tiles, point = tile_block(float(place['latitude']), float(place['longitude']))
        if not self.enabled or self._resting('map', key) or not self.budget.reserve('tiles', len(tiles)):
            return None
        gate = asyncio.Semaphore(2)  # the tile policy's two connections

        async def one(client, column, row):
            async with gate:
                headers, body = await self._get(client, TILE_URL.format(zoom=ZOOM, x=column, y=row), limit=TILE_BYTES)
                if not headers.get('content-type', '').startswith('image/png'):
                    raise ValueError('Use a standard map tile.')
                return body
        try:
            async with asyncio.timeout(TIMEOUT_S * 3):
                async with self._client() as client:
                    contents = await asyncio.gather(*(one(client, column, row) for column, row in tiles.values()))
            return await asyncio.to_thread(compose, dict(zip(tiles, contents)), point)
        except Exception:  # an upstream away, a tile outside its contract, a header that declares an enormous picture
            self._failed['map', key] = self.clock()
            return None

    async def _fetch_provider(self, place, key, width, height):
        """One counted call: (picture, seconds it stays valid), or None with the shape at rest."""
        import httpx
        if (not self.enabled or self.clock() < self._provider_rests_until or self._resting('picture', key)
                or not self.pictures.reserve()):
            return None
        # The call is counted: every ending of it rests this shape, and a kept picture ends the rest.
        self._failed['picture', key] = self.clock()
        url, params = picture_request(*self.provider, place, width, height)
        try:
            async with asyncio.timeout(TIMEOUT_S * 2):
                async with self._client() as client:
                    headers, body = await self._get(client, url, params=params, limit=PICTURE_BYTES)
            # Google draws a notice in place of the map when it refuses a request, and says so here.
            if 'x-staticmap-api-warning' in headers:
                raise ValueError('The provider drew a notice in place of the map.')
            picture = await asyncio.to_thread(provider_png, body, width, height)
        except Refused as refusal:
            if refusal.status in REFUSED_REST_S:  # a refused or throttled key: the provider rests as a whole
                self._provider_rests_until = self.clock() + REFUSED_REST_S[refusal.status]
            return None
        except (httpx.HTTPError, TimeoutError, ValueError, OSError, SyntaxError):
            return None
        self._failed.pop(('picture', key), None)
        return picture, valid_for(headers)

    async def _provider_picture(self, place, width, height):
        """The commissioned provider's picture of this shape, kept while the provider says it is valid.
        None hands the map to the tiles."""
        name = self.provider[0]
        key = f'{name}:{PICTURE_REVISION[name]}:{self.key(place)}:{width}x{height}'
        path = self.directory / f'map-{hashlib.sha256(key.encode()).hexdigest()[:32]}.png' if self.directory else None

        def kept():
            # A kept picture carries the moment its validity ends; a file that is no longer that picture is set aside.
            saved = self._saved(key, path)
            if saved and not kept_provider_png(saved[1], width, height):
                self._pictures.pop(key, None)
                try:
                    if path is not None:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
                return None
            return saved

        def fresh():
            saved = kept()
            return saved[1] if saved and self.clock() < saved[0] else None

        picture = fresh()
        if picture is None:
            async with self._lock('picture', key):
                picture = fresh()
                if picture is None:
                    drawn = await self._fetch_provider(place, key, width, height)
                    if drawn is not None:
                        picture, valid = drawn
                        self._save(key, path, picture, self.clock() + valid)
        if picture is None:
            saved = kept()  # an older picture from the provider still shows these streets, for a bounded time
            picture = saved[1] if saved and self.clock() < saved[0] + PICTURE_STALE_S else None
        return picture

    async def map(self, place, width=MAP_SIZE, height=MAP_SIZE):
        if width not in MAP_SIZES or height not in MAP_SIZES:
            raise ValueError('Choose a map side from 128 to 512 in steps of 32.')
        if self.provider:
            # The provider's picture carries its own mark and credit, so it is served as drawn.
            picture = await self._provider_picture(place, width, height)
            if picture is not None:
                return picture
        key = self.key(place)

        def fresh():
            saved = self._saved_picture(key)
            return saved[1] if saved and self.clock() - saved[0] < MAP_TTL_S else None

        picture = fresh()
        if picture is None:
            async with self._lock('map', key):
                picture = fresh()
                if picture is None:
                    picture = await self._fetch_picture(place, key)
                    if picture is not None:
                        self._save_picture(key, picture)
        if picture is None:
            saved = self._saved_picture(key)  # an older picture still shows these streets
            if saved is None:
                raise Unavailable('map')
            picture = saved[1]
        shape = (hashlib.sha256(picture).hexdigest(), width, height)
        if shape not in self._drawn:
            self._keep(self._drawn, shape, await asyncio.to_thread(credited, picture, width, height), limit=32)
        return self._drawn[shape]


def locate(selected, owner, location_id=None):
    """The session's location and its coordinates, read through the company's own stores.

    The business scope owns geography and grants; the location store owns the coordinates. One
    location in the principal's current access answers a request that names none. The second
    value is the check the host runs again before the answer leaves.
    """
    scope, store = selected.get('business_scope'), selected.get('location_intelligence')
    if scope is None or store is None or getattr(store, 'scope', None) is not scope:
        return None, None
    if location_id is not None and (not isinstance(location_id, str) or not 0 < len(location_id) <= 160):
        return None, None
    found = []
    try:
        for config in store._rows(owner, 'location'):
            point = config.get('coordinates')
            if not point or (location_id is not None and config['location_id'] != location_id):
                continue
            try:
                view = store.authorized(owner, config['collection'], config['location_id'])
            except ValueError:
                continue
            row = next((item for item in view['locations'] if item['id'] == config['location_id']), None)
            if row is not None:
                found.append((point, view, row))
        if len(found) != 1:
            return None, None
        (point, view, row), = found
        place = {'location_id': row['id'], 'name': str(row.get('name') or '')[:200],
                 'address': str(row.get('address') or '')[:500],
                 'latitude': _number(point['latitude'], -90, 90), 'longitude': _number(point['longitude'], -180, 180),
                 'timezone': point.get('timezone')}
        effective = view['effective']
        from business_scope import ScopeActor
        actor = ScopeActor(scope.company_id, owner)
    except (ValueError, LookupError, TypeError, OSError, sqlite3.Error):
        return None, None
    return place, lambda: scope.current(actor, effective['collection'], effective['revision'],
                                        account_role=effective['account_role'])


def service_directory(app):
    """The service's own state directory, beside its transfer ledger."""
    budget = getattr(app.state, 'transfer_budget', None)
    return Path(budget.path).parent / 'location-context' if budget is not None else None


def register_location_context_routes(app, context):
    """Two reads behind the control room's own session and company checks."""
    from retail_urls import retail_router
    app = retail_router(app)
    # The provider key is read once here and leaves the environment; its calls are counted in the host's
    # commissioned ledger, and a user with no home keeps the tiles.
    app.state.location_context = LocationContext(service_directory(app), provider=claim_provider() or False,
                                                 picture_ledger=host_ledger() or False)

    def place_of(request, location_id):
        selected, owner = context(request)
        place, current = locate(selected, owner, location_id)
        if place is None:
            raise HTTPException(404, 'Add this shop’s address to see its weather and map.')
        request.state.scope_guards.append(current)
        return place

    @app.get('/api/retail/location/weather')
    async def location_weather(request: Request, location_id: str | None = None):
        place = place_of(request, location_id)
        try:
            answer = await request.app.state.location_context.weather(place)
        except Unavailable:
            raise HTTPException(503, 'weather_returns_shortly', headers={'Retry-After': str(RETRY_S), 'Cache-Control': 'private, no-store'}) from None
        return JSONResponse(answer, headers={'Cache-Control': 'private, no-store'})

    @app.get('/api/retail/location/map')
    async def location_map(request: Request, location_id: str | None = None, width: int = MAP_SIZE, height: int = MAP_SIZE):
        if width not in MAP_SIZES or height not in MAP_SIZES:
            raise HTTPException(400, 'Choose a map side from 128 to 512 in steps of 32.')
        place = place_of(request, location_id)
        try:
            picture = await request.app.state.location_context.map(place, width, height)
        except Unavailable:
            raise HTTPException(503, 'map_returns_shortly', headers={'Retry-After': str(RETRY_S), 'Cache-Control': 'private, no-store'}) from None
        return Response(picture, media_type='image/png', headers={
            'Cache-Control': 'private, max-age=3600', 'X-Content-Type-Options': 'nosniff'})


def main(argv=None):
    """Host commands for the map provider's call ledger."""
    import argparse
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument('command', choices=['commission-map-ledger', 'map-ledger'])
    args = parser.parse_args(argv)
    path = host_ledger()
    if path is None:
        parser.error('This user has no home directory to keep the ledger in.')
    ledger = ProviderLedger(path, PICTURE_DAILY_CALLS, PICTURE_MONTHLY_CALLS)
    made = ledger.commission() if args.command == 'commission-map-ledger' else False
    day, month = ledger.used()
    print(json.dumps({'ledger': str(path), 'made': made, 'calls_today': day, 'calls_this_month': month,
                      'daily_cap': PICTURE_DAILY_CALLS, 'monthly_cap': PICTURE_MONTHLY_CALLS}))


if __name__ == '__main__':
    main()
