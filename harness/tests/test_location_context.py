"""Weather and the neighbourhood map come from this server, bounded, with only the shop's coordinates.

Every upstream here is an injected transport: this file opens no socket to an outside host.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import location_context as lc
from location_context import LocationContext, Unavailable

SOFIA = {'location_id': 'location-7f3a', 'name': 'Vitosha Boulevard 85', 'address': '85 Vitosha Boulevard, Sofia',
         'latitude': 42.6869, 'longitude': 23.3186, 'timezone': 'Europe/Sofia'}
# 10 October 2026, 14:15 at the shop (UTC+3).
NOON = datetime(2026, 10, 10, 11, 15, tzinfo=timezone.utc).timestamp()


def upstream(first_day='2026-10-10', days=8, now='2026-10-10T14:15'):
    """An answer in Open-Meteo's own shape."""
    start = datetime.strptime(first_day, '%Y-%m-%d')
    hours = [start + timedelta(hours=hour) for hour in range(days * 24)]
    dates = [start + timedelta(days=day) for day in range(days)]
    return {
        'latitude': 42.6875, 'longitude': 23.3125, 'utc_offset_seconds': 10800, 'timezone': 'Europe/Sofia',
        'current': {'time': now, 'interval': 900, 'temperature_2m': 20.2, 'apparent_temperature': 19.1,
                    'relative_humidity_2m': 48, 'weather_code': 2, 'wind_speed_10m': 9.4, 'is_day': 1},
        'hourly': {'time': [hour.strftime('%Y-%m-%dT%H:00') for hour in hours],
                   'temperature_2m': [10 + (hour.hour % 12) + hour.day / 10 for hour in hours],
                   'precipitation_probability': [None if hour.hour == 3 else hour.hour for hour in hours],
                   'weather_code': [61 if hour.hour == 16 else 2 for hour in hours],
                   'is_day': [int(7 <= hour.hour < 19) for hour in hours]},
        'daily': {'time': [date.strftime('%Y-%m-%d') for date in dates], 'weather_code': [3] + [61] * (days - 1),
                  'temperature_2m_max': [21.4 + day for day in range(days)], 'temperature_2m_min': [9.6 + day for day in range(days)],
                  'precipitation_probability_max': [10 * day for day in range(days)],
                  'sunrise': [date.strftime('%Y-%m-%dT07:33') for date in dates],
                  'sunset': [date.strftime('%Y-%m-%dT18:52') for date in dates]},
    }


class Clock:
    def __init__(self, at=NOON): self.at = at
    def __call__(self): return self.at
    def forward(self, seconds): self.at += seconds


class Upstream:
    """Records every outside request and answers it from the fixture."""
    def __init__(self, weather=None, tile=None):
        self.requests, self.weather, self.tile, self.fail = [], weather or (lambda request: httpx.Response(200, json=upstream())), tile, False

    def __call__(self, request):
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectError('outside host is away', request=request)
        if request.url.host == 'api.open-meteo.com':
            return self.weather(request)
        if request.url.host == 'tile.openstreetmap.org':
            return self.tile(request)
        raise AssertionError(f'Unexpected outside host {request.url.host}')

    def to(self, host): return [request for request in self.requests if request.url.host == host]


@pytest.fixture(autouse=True)
def untoned(request, monkeypatch):
    """The tile tests read exact pixels; the tone this service bakes in has its own test."""
    if 'quiet_tone' not in request.node.name:
        monkeypatch.setattr(lc, 'MAP_TONE', None)


def service(tmp_path, outside, clock=None, **options):
    return LocationContext(tmp_path / 'context', transport=httpx.MockTransport(outside), clock=clock or Clock(), **options)


def run(work): return asyncio.run(work)


def test_answer_names_the_place_the_current_hour_twenty_four_hours_and_seven_days(tmp_path):
    outside = Upstream(); answer = run(service(tmp_path, outside).weather(SOFIA))
    assert answer['place'] == 'Vitosha Boulevard 85' and answer['city'] == 'Sofia' and answer['stale'] is False
    assert answer['current'] == {'temperature_c': 20.2, 'condition': 'Partly cloudy', 'icon': 'cloud-sun', 'is_day': True,
                                 'feels_like_c': 19.1, 'wind_kmh': 9.4, 'humidity_percent': 48}
    assert len(answer['hourly']) == 24 and len(answer['daily']) == 7
    assert [hour['time'] for hour in answer['hourly']][:2] == ['2026-10-10T14:00', '2026-10-10T15:00']
    assert answer['hourly'][-1]['time'] == '2026-10-11T13:00'
    rain = answer['hourly'][2]; night = answer['hourly'][6]
    assert (rain['condition'], rain['icon'], rain['rain_chance_percent']) == ('Light rain', 'cloud-rain', 16)
    assert (night['icon'], night['is_day']) == ('cloud-moon', False)
    assert next(hour for hour in answer['hourly'] if hour['time'].endswith('T03:00'))['rain_chance_percent'] is None
    today = answer['daily'][0]
    assert today == {'date': '2026-10-10', 'weekday': 'Saturday', 'high_c': 21.4, 'low_c': 9.6, 'condition': 'Cloudy',
                     'icon': 'cloudy', 'rain_chance_percent': 0, 'sunrise': '07:33', 'sunset': '18:52'}
    assert [day['weekday'] for day in answer['daily']][1:3] == ['Sunday', 'Monday']


def test_upstream_receives_the_coordinates_and_nothing_else_about_the_shop(tmp_path):
    outside = Upstream(); run(service(tmp_path, outside).weather(SOFIA))
    request, = outside.requests
    assert request.url.host == 'api.open-meteo.com' and request.url.path == '/v1/forecast'
    sent = dict(request.url.params)
    assert sent['latitude'] == '42.6869' and sent['longitude'] == '23.3186' and sent['timezone'] == 'auto'
    assert set(sent) == {'latitude', 'longitude', 'current', 'hourly', 'daily', 'timezone', 'forecast_days'}
    everything = str(request.url) + json.dumps(dict(request.headers))
    for private in ('Vitosha', 'Boulevard', 'location-7f3a', 'Sofia', 'peter'):
        assert private not in everything
    assert request.headers['user-agent'].startswith('Rocoto/')


def test_one_answer_serves_a_location_for_fifteen_minutes(tmp_path):
    outside, clock = Upstream(), Clock(); context = service(tmp_path, outside, clock)

    async def burst():
        return await asyncio.gather(*(context.weather(SOFIA) for _ in range(6)))
    assert len(run(burst())) == 6 and len(outside.requests) == 1
    clock.forward(14 * 60 + 59); run(context.weather(SOFIA)); assert len(outside.requests) == 1
    clock.forward(2); run(context.weather(SOFIA)); assert len(outside.requests) == 2
    # A second shop is its own location.
    run(context.weather({**SOFIA, 'latitude': 41.3874, 'longitude': 2.1686})); assert len(outside.requests) == 3


def test_saved_answer_serves_while_the_upstream_fails_and_rests_before_the_next_call(tmp_path):
    outside, clock = Upstream(), Clock(); context = service(tmp_path, outside, clock)
    first = run(context.weather(SOFIA)); outside.fail = True
    clock.forward(16 * 60); stale = run(context.weather(SOFIA))
    assert stale['stale'] is True and stale['current'] == first['current'] and len(outside.requests) == 2
    # The failed upstream rests five minutes: this read makes no call.
    clock.forward(60); run(context.weather(SOFIA)); assert len(outside.requests) == 2
    # Past the hour the saved answer speaks through its own forecast for the current hour.
    clock.forward(2 * 3600); later = run(context.weather(SOFIA))
    assert later['stale'] is True and later['hourly'][0]['time'] == '2026-10-10T16:00'
    assert later['current']['temperature_c'] == later['hourly'][0]['temperature_c'] and later['current']['wind_kmh'] is None
    assert later['observed_at'] == '2026-10-10T16:00'
    # Six hours on, the saved answer has ended its service.
    clock.forward(4 * 3600)
    with pytest.raises(Unavailable): run(context.weather(SOFIA))
    outside.fail = False; clock.forward(lc.RETRY_S); assert run(context.weather(SOFIA))['stale'] is False


def test_first_read_with_the_upstream_away_is_unavailable(tmp_path):
    outside = Upstream(); outside.fail = True
    with pytest.raises(Unavailable): run(service(tmp_path, outside).weather(SOFIA))


@pytest.mark.parametrize('damage', [
    lambda answer: answer['current'].update(temperature_2m=float('nan')),
    lambda answer: answer['current'].update(temperature_2m='20'),
    lambda answer: answer['current'].update(weather_code=1234),
    lambda answer: answer['hourly'].update(temperature_2m=answer['hourly']['temperature_2m'][:-1]),
    lambda answer: answer['hourly'].update(time=answer['hourly']['time'][:12]),
    lambda answer: answer.pop('daily'),
    lambda answer: answer.update(utc_offset_seconds='10800'),
    lambda answer: answer['daily'].update(sunrise=['soon'] * 8),
])
def test_answer_outside_the_upstream_contract_is_refused(tmp_path, damage):
    body = upstream(); damage(body)
    outside = Upstream(weather=lambda request: httpx.Response(200, content=json.dumps(body).replace('NaN', 'null')))
    with pytest.raises(Unavailable): run(service(tmp_path, outside).weather(SOFIA))


def test_oversized_and_refused_answers_are_unavailable(tmp_path):
    for response in (httpx.Response(200, content=b' ' * (lc.WEATHER_BYTES + 1)), httpx.Response(429, json={'error': True}),
                     httpx.Response(200, content=b'<html>')):
        outside = Upstream(weather=lambda request, response=response: response)
        with pytest.raises(Unavailable): run(service(tmp_path / str(response.status_code) / str(len(response.content)), outside).weather(SOFIA))


def test_slow_upstream_is_cut_at_the_timeout(tmp_path, monkeypatch):
    assert lc.TIMEOUT_S == 5
    monkeypatch.setattr(lc, 'TIMEOUT_S', 0.05); calls = []

    async def slow(request):
        calls.append(request); await asyncio.sleep(2); return httpx.Response(200, json=upstream())
    context = LocationContext(tmp_path / 'context', transport=httpx.MockTransport(slow), clock=Clock())
    started = asyncio.new_event_loop().time()

    async def read():
        begun = asyncio.get_running_loop().time()
        with pytest.raises(Unavailable): await context.weather(SOFIA)
        return asyncio.get_running_loop().time() - begun
    assert run(read()) < 1 and len(calls) == 1 and started >= 0


def test_two_hundred_upstream_calls_a_day_for_the_whole_service(tmp_path):
    assert lc.WEATHER_DAILY_CALLS == 200
    outside, clock = Upstream(), Clock(); first = service(tmp_path, outside, clock); second = service(tmp_path, outside, clock)
    place = lambda number: {**SOFIA, 'latitude': 10 + number / 100}

    async def day(contexts, count, offset=0):
        for number in range(count):
            await contexts[number % len(contexts)].weather(place(offset + number))
    run(day([first, second], 200))
    assert len(outside.requests) == 200 and first.budget.used('weather') == 200
    with pytest.raises(Unavailable): run(second.weather(place(999)))
    assert len(outside.requests) == 200
    # A process started later the same day reads the same ledger.
    with pytest.raises(Unavailable): run(service(tmp_path, outside, clock).weather(place(998)))
    assert len(outside.requests) == 200
    # Locations answered today keep their saved answers.
    assert run(first.weather(place(0)))['stale'] is False
    clock.forward(24 * 3600); run(first.weather(place(997))); assert len(outside.requests) == 201 and first.budget.used('weather') == 1


def test_unreadable_ledger_admits_no_call(tmp_path):
    outside = Upstream(); context = service(tmp_path, outside)
    (tmp_path / 'context').mkdir(); (tmp_path / 'context/budget.json').write_text('{"day":')
    with pytest.raises(Unavailable): run(context.weather(SOFIA))
    assert outside.requests == [] and context.budget.used('weather') == 200
    (tmp_path / 'context/budget.json').write_text(json.dumps({'day': '2026-10-10', 'used': {'weather': -4}}))
    with pytest.raises(Unavailable): run(context.weather(SOFIA))
    assert outside.requests == []


def test_switched_off_service_makes_no_outside_call_and_keeps_saved_answers(tmp_path):
    outside, clock = Upstream(), Clock(); context = service(tmp_path, outside, clock)
    run(context.weather(SOFIA)); context.enabled = False; clock.forward(20 * 60)
    assert run(context.weather(SOFIA))['stale'] is True and len(outside.requests) == 1
    with pytest.raises(Unavailable): run(service(tmp_path / 'other', outside, clock, enabled=False).weather(SOFIA))
    assert len(outside.requests) == 1
    assert lc.context_enabled({}) is True and lc.context_enabled({'ROCOTO_VIDEO_LOCATION_CONTEXT': 'off'}) is False
    with pytest.raises(ValueError): lc.context_enabled({'ROCOTO_VIDEO_LOCATION_CONTEXT': 'maybe'})


@pytest.mark.parametrize('address,zone,city', [
    ('85 Vitosha Boulevard, Sofia', 'Europe/Sofia', 'Sofia'),
    ('Carrer de Mallorca 12, 08001 Barcelona, Spain', 'Europe/Madrid', 'Barcelona'),
    ('Main Street 1, Lozenets, Sofia, Bulgaria', 'Europe/Sofia', 'Sofia'),
    ('10 Downing Street, London SW1A 2AA', 'Europe/London', 'London'),
    ('Sofia', 'Europe/Sofia', 'Sofia'), ('', 'America/New_York', 'New York'), ('', None, '')])
def test_city_comes_from_the_stored_address(address, zone, city):
    assert lc.city_of(address, zone) == city


# ---- map ------------------------------------------------------------------------------------------------

def tile_png(column, row):
    """A tile whose every pixel carries its own position on the world map."""
    from PIL import Image
    image = Image.new('RGB', (lc.TILE, lc.TILE)); pixels = image.load()
    for x in range(lc.TILE):
        for y in range(lc.TILE):
            pixels[x, y] = ((column * lc.TILE + x) % 256, (row * lc.TILE + y) % 256, (column + row) % 256)
    output = io.BytesIO(); image.save(output, 'PNG'); return output.getvalue()


TILES = {}
def tile_answer(request):
    zoom, column, row = request.url.path.removesuffix('.png').strip('/').split('/')
    assert zoom == '16'
    key = (int(column), int(row))
    if key not in TILES: TILES[key] = tile_png(*key)
    return httpx.Response(200, content=TILES[key], headers={'Content-Type': 'image/png'})


def opened(picture):
    from PIL import Image
    image = Image.open(io.BytesIO(picture)); image.load(); return image


def test_map_is_one_png_centred_on_the_shop_from_nine_tiles_with_the_credit_drawn_in(tmp_path):
    outside = Upstream(tile=tile_answer); context = service(tmp_path, outside)
    picture = run(context.map(SOFIA)); image = opened(picture)
    assert image.format == 'PNG' and image.size == (512, 512)
    fetched = outside.to('tile.openstreetmap.org'); assert len(fetched) == 9 == len(outside.requests)
    x, y = lc.tile_position(SOFIA['latitude'], SOFIA['longitude'])
    assert {request.url.path for request in fetched} == {f'/16/{int(x) + dx}/{int(y) + dy}.png' for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
    for request in fetched:
        assert request.headers['user-agent'].startswith('Rocoto/') and 'soyrocoto.com' in request.headers['user-agent']
        assert not request.url.query and 'Vitosha' not in str(request.url)
    # The shop's own pixel sits at the picture's centre.
    centre = image.convert('RGB').getpixel((256, 256))
    assert centre[:2] == (round(x * 256) % 256, round(y * 256) % 256)
    # The credit is drawn into the bottom-right corner; the rest of the picture is the map as fetched.
    bare = opened(lc.compose({(dx, dy): TILES[(int(x) + dx - 1, int(y) + dy - 1)] for dx in range(3) for dy in range(3)},
                             ((x - int(x) + 1) * 256, (y - int(y) + 1) * 256))).convert('RGB')
    from PIL import ImageChops
    changed = ImageChops.difference(image.convert('RGB'), bare).getbbox()
    # The plate keeps sixteen pixels from both edges, so a frame that trims the picture's edge leaves it whole.
    assert changed and changed[0] > 180 and changed[1] > 452 and changed[2] <= 497 and changed[3] <= 497
    light = sum(1 for pixel in image.convert('RGB').crop(changed).getdata() if min(pixel) > 180)
    dark = sum(1 for pixel in image.convert('RGB').crop(changed).getdata() if max(pixel) < 110)
    assert light > 2000 and dark > 300  # a light plate with dark lettering


def test_map_shapes_keep_the_shop_centred_and_the_credit_inside(tmp_path):
    outside = Upstream(tile=tile_answer); context = service(tmp_path, outside)
    square = opened(run(context.map(SOFIA))); wide = opened(run(context.map(SOFIA, 512, 320)))
    assert wide.size == (512, 320) and len(outside.requests) == 9
    assert wide.convert('RGB').getpixel((256, 160)) == square.convert('RGB').getpixel((256, 256))
    assert wide.convert('RGB').getpixel((472, 292)) != square.convert('RGB').getpixel((472, 388))  # the credit moved with the corner
    for width, height in ((500, 512), (512, 544), (96, 256)):
        with pytest.raises(ValueError): run(context.map(SOFIA, width, height))


def test_map_is_kept_thirty_days_across_restarts(tmp_path):
    assert lc.MAP_TTL_S == 30 * 24 * 3600
    outside, clock = Upstream(tile=tile_answer), Clock(); first = service(tmp_path, outside, clock)
    picture = run(first.map(SOFIA)); run(first.map(SOFIA)); assert len(outside.requests) == 9
    clock.forward(29 * 24 * 3600)
    assert run(service(tmp_path, outside, clock).map(SOFIA)) == picture and len(outside.requests) == 9
    clock.forward(2 * 24 * 3600); later = service(tmp_path, outside, clock)
    assert run(later.map(SOFIA)) == picture and len(outside.requests) == 18
    # A picture past its thirty days still serves while the tile server is away.
    clock.forward(31 * 24 * 3600); outside.fail = True
    assert run(service(tmp_path, outside, clock).map(SOFIA)) == picture
    saved = list((tmp_path / 'context').glob('map-*.png'))
    assert len(saved) == 1 and saved[0].stat().st_mode & 0o077 == 0


def test_forty_tile_fetches_a_day(tmp_path):
    assert lc.TILE_DAILY_FETCHES == 40
    outside, clock = Upstream(tile=tile_answer), Clock(); context = service(tmp_path, outside, clock)
    place = lambda number: {**SOFIA, 'latitude': 42 + number / 10}
    for number in range(4): run(context.map(place(number)))
    assert len(outside.requests) == 36 and context.budget.used('tiles') == 36
    # A fifth picture needs nine fetches; four remain today, so it makes none.
    with pytest.raises(Unavailable): run(context.map(place(4)))
    assert len(outside.requests) == 36 and context.budget.used('tiles') == 36
    assert opened(run(context.map(place(0)))).size == (512, 512)
    clock.forward(24 * 3600); run(context.map(place(4))); assert len(outside.requests) == 45


def test_broken_tile_leaves_the_map_unavailable_and_rests(tmp_path):
    answers = {'broken': True}

    def tile(request):
        if answers['broken'] and request.url.path.endswith('/24158.png'):
            return httpx.Response(200, content=b'not a picture', headers={'Content-Type': 'image/png'})
        return tile_answer(request)
    outside, clock = Upstream(tile=tile), Clock(); context = service(tmp_path, outside, clock)
    with pytest.raises(Unavailable): run(context.map(SOFIA))
    assert len(outside.requests) == 9
    with pytest.raises(Unavailable): run(context.map(SOFIA))
    assert len(outside.requests) == 9
    answers['broken'] = False; clock.forward(lc.RETRY_S)
    assert opened(run(context.map(SOFIA))).size == (512, 512)
    html = Upstream(tile=lambda request: httpx.Response(200, content=TILES[next(iter(TILES))], headers={'Content-Type': 'text/html'}))
    with pytest.raises(Unavailable): run(service(tmp_path / 'html', html).map(SOFIA))


TOKEN = 'pk.' + 'a1B2' * 12
KEY = 'AIza' + 'c3D4' * 9
DAY = 24 * 3600


def provider_png_of(width, height, kind='PNG', **save):
    from PIL import Image
    output = io.BytesIO(); Image.new('RGB', (width, height), (232, 233, 228)).save(output, kind, **save); return output.getvalue()


class Provider:
    """A commissioned map provider and the tile server beside it; every request is recorded."""
    HOSTS = {'api.mapbox.com': 'mapbox', 'maps.googleapis.com': 'google'}

    def __init__(self, answer=None, headers=None):
        self.requests, self.answer, self.headers = [], answer, headers or {}

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == 'tile.openstreetmap.org':
            return tile_answer(request)
        assert request.url.host in self.HOSTS, f'Unexpected outside host {request.url.host}'
        if self.answer:
            return self.answer(request)
        if request.url.host == 'api.mapbox.com':
            width, height = request.url.path.rsplit('/', 1)[1].removesuffix('@2x').split('x')
        else:
            width, height = request.url.params['size'].split('x')
        return httpx.Response(200, content=provider_png_of(2 * int(width), 2 * int(height)), headers={'Content-Type': 'image/png', **self.headers})

    def drawn(self): return [request for request in self.requests if request.url.host in self.HOSTS]
    def tiles(self): return [request for request in self.requests if request.url.host == 'tile.openstreetmap.org']


def provided(tmp_path, outside, clock=None, name='context', provider=('mapbox', TOKEN), **options):
    """A service with a commissioned provider and its ledger made, as the host command makes it."""
    context = LocationContext(tmp_path / name, transport=httpx.MockTransport(outside), clock=clock or Clock(), provider=provider, **options)
    context.pictures.commission()
    return context


def place(number): return {**SOFIA, 'latitude': 42 + number / 10}


def test_provider_comes_from_the_environment_once_and_leaves_it():
    assert lc.map_provider({}) is None and lc.map_provider({'ROCOTO_VIDEO_MAPBOX_TOKEN': '  '}) is None
    assert lc.map_provider({'ROCOTO_VIDEO_GOOGLE_MAPS_API_KEY': KEY}) == ('google', KEY)
    both = {'ROCOTO_VIDEO_MAPBOX_TOKEN': TOKEN, 'ROCOTO_VIDEO_GOOGLE_MAPS_API_KEY': KEY, 'PATH': '/usr/bin'}
    assert lc.map_provider(both) == ('mapbox', TOKEN)
    with pytest.raises(ValueError): lc.map_provider({'ROCOTO_VIDEO_MAPBOX_TOKEN': 'pk.short'})
    with pytest.raises(ValueError): lc.map_provider({'ROCOTO_VIDEO_GOOGLE_MAPS_API_KEY': KEY + ' second line'})
    # The service claims the key at start: a child process started later inherits neither name.
    assert lc.claim_provider(both) == ('mapbox', TOKEN) and both == {'PATH': '/usr/bin'}
    assert lc.claim_provider(both) is None


def test_mapbox_draws_one_shape_in_one_call_with_the_coordinates_and_nothing_else_about_the_shop(tmp_path):
    outside = Provider(); context = provided(tmp_path, outside)
    picture = run(context.map(SOFIA, 512, 288)); image = opened(picture)
    assert image.format == 'PNG' and image.size == (1024, 576)  # two pixels per point, as the provider drew it
    request, = outside.requests
    assert request.url.host == 'api.mapbox.com'
    assert request.url.path == '/styles/v1/mapbox/light-v11/static/23.31860,42.68690,15,0/512x288@2x'
    assert dict(request.url.params) == {'access_token': TOKEN}
    sent = str(request.url) + json.dumps(dict(request.headers))
    for private in ('Vitosha', 'location-7f3a', 'Sofia'): assert private not in sent
    # The same shape is kept; a second shape is a second call.
    assert run(context.map(SOFIA, 512, 288)) == picture and len(outside.requests) == 1
    run(context.map(SOFIA, 256, 128)); assert len(outside.drawn()) == 2 and outside.tiles() == []
    assert context.pictures.used() == (2, 2)
    # Nothing kept by the service carries the key.
    for kept in (tmp_path / 'context').iterdir(): assert TOKEN not in kept.name and TOKEN.encode() not in kept.read_bytes()


def test_google_draws_one_shape_in_one_call_and_a_drawn_notice_hands_the_map_to_the_tiles(tmp_path):
    outside = Provider(); context = provided(tmp_path, outside, provider=('google', KEY))
    assert opened(run(context.map(SOFIA, 512, 288))).size == (1024, 576)
    request, = outside.requests
    assert request.url.host == 'maps.googleapis.com' and request.url.path == '/maps/api/staticmap'
    params = request.url.params
    assert (params['center'], params['zoom'], params['size'], params['scale'], params['maptype'], params['key']) == ('42.68690,23.31860', '16', '512x288', '2', 'roadmap', KEY)
    assert params.get_list('style') == list(lc.GOOGLE_STYLE) and set(params.keys()) == {'center', 'zoom', 'size', 'scale', 'maptype', 'style', 'key'}
    # Google answers a refused request with a picture of a notice and this header: that picture is never kept.
    refused = Provider(headers={'X-Staticmap-API-Warning': 'Error geocoding: center'}); clock = Clock()
    second = provided(tmp_path, refused, clock, 'second', provider=('google', KEY))
    assert opened(run(second.map(SOFIA))).size == (512, 512) and len(refused.drawn()) == 1 and len(refused.tiles()) == 9
    assert len(list((tmp_path / 'second').glob('map-*.png'))) == 1  # the tiles' picture alone
    run(second.map(SOFIA)); assert len(refused.drawn()) == 1  # the shape rests after a refusal
    clock.forward(lc.RETRY_S); run(second.map(SOFIA)); assert len(refused.drawn()) == 2


def enormous_png():
    """A 69-byte PNG whose header declares 40000 by 40000 pixels."""
    import struct, zlib
    def chunk(kind, data): return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 40000, 40000, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\0')) + chunk(b'IEND', b'')


@pytest.mark.parametrize('answer', [
    lambda request: httpx.Response(500, text='away'),
    lambda request: httpx.Response(200, content=b'<html>quota</html>', headers={'Content-Type': 'text/html'}),
    lambda request: httpx.Response(200, content=provider_png_of(640, 640), headers={'Content-Type': 'image/png'}),
    lambda request: httpx.Response(200, content=provider_png_of(512, 512), headers={'Content-Type': 'image/png'}),
    lambda request: httpx.Response(200, content=b'x' * (lc.PICTURE_BYTES + 1), headers={'Content-Type': 'image/png'}),
    lambda request: httpx.Response(200, content=enormous_png(), headers={'Content-Type': 'image/png'}),
    lambda request: httpx.Response(302, headers={'Location': 'https://elsewhere.example/map.png'}),
], ids=['away', 'not_a_picture', 'another_shape', 'one_pixel_per_point', 'oversized', 'declares_an_enormous_size', 'redirect'])
def test_provider_answer_outside_its_contract_hands_the_map_to_the_tiles_and_rests_the_shape(tmp_path, answer):
    outside = Provider(answer); context = provided(tmp_path, outside)
    for _ in range(3):
        assert opened(run(context.map(SOFIA))).size == (512, 512)  # the tiles' picture, credit drawn in
    assert len(outside.drawn()) == 1 and len(outside.tiles()) == 9 and context.pictures.used() == (1, 1)
    assert all(request.url.host != 'elsewhere.example' for request in outside.requests)


def test_kept_provider_picture_is_plain_and_a_spoiled_file_is_redrawn_in_one_counted_call(tmp_path):
    from PIL import Image
    profile = b'profile bytes ' * 4000
    outside = Provider(lambda request: httpx.Response(200, content=provider_png_of(1024, 1024, 'JPEG', icc_profile=profile), headers={'Content-Type': 'image/jpeg'}))
    context = provided(tmp_path, outside); picture = run(context.map(SOFIA))
    kept, = (tmp_path / 'context').glob('map-*.png')
    assert opened(picture).format == 'PNG' and 'icc_profile' not in opened(picture).info and profile[:28] not in kept.read_bytes()
    assert kept.stat().st_mode & 0o077 == 0
    # A zero-byte or foreign kept file is set aside: the next read is one counted call, then it is kept again.
    for spoiled in (b'', b'<html>not a picture</html>', provider_png_of(512, 512)):
        kept.write_bytes(spoiled); before = len(outside.drawn())
        again = provided(tmp_path, outside)
        assert opened(run(again.map(SOFIA))).size == (1024, 1024) and len(outside.drawn()) == before + 1
        assert run(again.map(SOFIA)) and len(outside.drawn()) == before + 1


def test_provider_picture_is_kept_as_long_as_the_provider_says_inside_bounds(tmp_path):
    assert (lc.PICTURE_VALID_S, lc.PICTURE_VALID_RANGE_S, lc.PICTURE_STALE_S) == (12 * 3600, (3600, 30 * DAY), 7 * DAY)
    assert lc.valid_for({}) == 12 * 3600 and lc.valid_for({'cache-control': 'public, max-age=86400'}) == DAY
    assert lc.valid_for({'cache-control': 'max-age=5'}) == 3600 and lc.valid_for({'cache-control': 'max-age=999999999'}) == 30 * DAY
    assert lc.valid_for({'cache-control': 'no-store, s-maxage=9'}) == 12 * 3600
    outside, clock = Provider(headers={'Cache-Control': 'public, max-age=43200'}), Clock()
    context = provided(tmp_path, outside, clock); picture = run(context.map(SOFIA))
    clock.forward(11 * 3600); assert run(provided(tmp_path, outside, clock).map(SOFIA)) == picture and len(outside.drawn()) == 1
    clock.forward(2 * 3600); run(provided(tmp_path, outside, clock).map(SOFIA)); assert len(outside.drawn()) == 2
    # A clock that steps back keeps the picture: validity is an end moment, never an age.
    clock.forward(-40 * DAY); assert opened(run(provided(tmp_path, outside, clock).map(SOFIA))).size == (1024, 1024) and len(outside.drawn()) == 2
    clock.forward(40 * DAY)
    # With the provider away the older picture serves seven days past its validity, then the tiles draw.
    outside.answer = lambda request: httpx.Response(500, text='away')
    clock.forward(13 * 3600); assert opened(run(provided(tmp_path, outside, clock).map(SOFIA))).size == (1024, 1024)
    clock.forward(7 * DAY); assert opened(run(provided(tmp_path, outside, clock).map(SOFIA))).size == (512, 512)
    # A switched-off service asks nobody: neither the provider nor the tiles.
    silent = Provider(); off = provided(tmp_path, silent, clock, 'off', enabled=False)
    with pytest.raises(Unavailable): run(off.map(SOFIA))
    assert silent.requests == [] and off.pictures.used() == (0, 0)


def test_sixty_provider_calls_a_day_and_a_thousand_a_month_for_the_host(tmp_path, monkeypatch):
    assert (lc.PICTURE_DAILY_CALLS, lc.PICTURE_MONTHLY_CALLS) == (60, 1000)
    monkeypatch.setattr(lc, 'PICTURE_DAILY_CALLS', 3); monkeypatch.setattr(lc, 'PICTURE_MONTHLY_CALLS', 5)
    outside, clock, ledger = Provider(), Clock(), tmp_path / 'host/map-pictures.json'
    # Two services of one host share the ledger: their calls count together.
    first = provided(tmp_path, outside, clock, 'one', picture_ledger=ledger)
    second = provided(tmp_path, outside, clock, 'two', picture_ledger=ledger)
    run(first.map(place(0))); run(second.map(place(1))); run(first.map(place(2)))
    assert len(outside.drawn()) == 3 and outside.tiles() == []
    # The day is full: the next picture comes from the tiles and the provider hears nothing.
    assert opened(run(second.map(place(3)))).size == (512, 512)
    assert len(outside.drawn()) == 3 and len(outside.tiles()) == 9
    assert json.loads(ledger.read_text()) == {'days': {'2026-10-10': 3}, 'months': {'2026-10': 3}} and ledger.stat().st_mode & 0o077 == 0
    # The next day admits calls until the month is full; the month's count outlives the day's.
    clock.forward(DAY); run(first.map(place(4))); run(first.map(place(5)))
    assert len(outside.drawn()) == 5 and first.pictures.used() == (2, 5)
    run(first.map(place(6))); assert len(outside.drawn()) == 5
    clock.forward(DAY); run(first.map(place(7))); assert len(outside.drawn()) == 5
    # 12 October plus 20 days is November: the month starts again.
    clock.forward(20 * DAY); run(first.map(place(8))); assert len(outside.drawn()) == 6


def test_a_clock_that_moves_away_and_returns_finds_its_count_and_a_clock_behind_admits_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, 'PICTURE_DAILY_CALLS', 2); monkeypatch.setattr(lc, 'PICTURE_MONTHLY_CALLS', 3)
    outside, clock = Provider(), Clock(); context = provided(tmp_path, outside, clock)
    for number in range(3): run(context.map(place(number)))
    assert len(outside.drawn()) == 2  # the day is full
    # The clock reads January for a moment (a boot before time sync): nothing leaves, and October keeps its count.
    clock.forward(-280 * DAY); run(context.map(place(3))); assert len(outside.drawn()) == 2
    clock.forward(280 * DAY); run(context.map(place(4))); assert len(outside.drawn()) == 2 and context.pictures.used() == (2, 2)
    # Ten seconds back over midnight: yesterday is behind a day already counted.
    clock.forward(DAY); run(context.map(place(5))); assert len(outside.drawn()) == 3
    clock.at = datetime(2026, 10, 10, 23, 59, 55, tzinfo=timezone.utc).timestamp(); run(context.map(place(6)))
    assert len(outside.drawn()) == 3 and json.loads((tmp_path / 'context/map-pictures.json').read_text()) == {'days': {'2026-10-10': 2, '2026-10-11': 1}, 'months': {'2026-10': 3}}


@pytest.mark.parametrize('damage', [
    '{"days":', json.dumps([1]), json.dumps({'days': {}}), json.dumps({'days': {}, 'months': {}, 'day': '2026-10-10'}),
    json.dumps({'days': {'2026-10-10': -1}, 'months': {}}), json.dumps({'days': {'2026-10-10': 1.0}, 'months': {}}),
    json.dumps({'days': {'2026-10-10 ': 1}, 'months': {}}), json.dumps({'days': {}, 'months': {'202610': 1}}),
    json.dumps({'days': {}, 'months': {'2026-10': True}}), json.dumps({'days': None, 'months': {}}),
    json.dumps({'day': '2026-10-10', 'used': {'pictures': 0}, 'month': '2026-10', 'month_used': {'pictures': 0}}),
])
def test_a_damaged_provider_ledger_admits_no_call(tmp_path, damage):
    outside = Provider(); context = provided(tmp_path, outside)
    (tmp_path / 'context/map-pictures.json').write_text(damage)
    assert opened(run(context.map(SOFIA))).size == (512, 512) and outside.drawn() == []
    assert context.pictures.used() == (lc.PICTURE_DAILY_CALLS, lc.PICTURE_MONTHLY_CALLS)
    assert (tmp_path / 'context/map-pictures.json').read_text() == damage  # serving leaves a damaged ledger as it found it


def test_a_provider_ledger_is_made_by_commissioning_and_never_by_serving(tmp_path):
    import shutil
    outside = Provider()
    bare = LocationContext(tmp_path / 'bare', transport=httpx.MockTransport(outside), clock=Clock(), provider=('mapbox', TOKEN))
    assert opened(run(bare.map(SOFIA))).size == (512, 512) and outside.drawn() == []
    assert not (tmp_path / 'bare/map-pictures.json').exists()
    assert bare.pictures.commission() is True and bare.pictures.commission() is False
    run(bare.map(SOFIA, 256, 256)); assert len(outside.drawn()) == 1
    # A removed ledger file or directory stops the provider; it restarts no count.
    ledger = tmp_path / 'host/map-pictures.json'
    hosted = provided(tmp_path, outside, name='hosted', picture_ledger=ledger); run(hosted.map(place(1))); assert len(outside.drawn()) == 2
    shutil.rmtree(ledger.parent); run(hosted.map(place(2))); assert len(outside.drawn()) == 2 and not ledger.parent.exists()
    ledger.parent.mkdir(); ledger.symlink_to(tmp_path / 'bare/map-pictures.json'); run(hosted.map(place(3))); assert len(outside.drawn()) == 2
    # With no place to keep the count, the provider stays unasked.
    assert LocationContext(None, provider=('mapbox', TOKEN)).provider is None
    assert LocationContext(tmp_path / 'kept', provider=('mapbox', TOKEN), picture_ledger=False).provider is None
    assert LocationContext(tmp_path / 'kept', provider=False).provider is None


def test_a_refused_key_rests_the_provider_as_a_whole(tmp_path):
    assert lc.REFUSED_REST_S == {401: 6 * 3600, 403: 6 * 3600, 429: 3600}
    for status, rest in lc.REFUSED_REST_S.items():
        outside, clock = Provider(lambda request, status=status: httpx.Response(status, text='refused')), Clock()
        context = provided(tmp_path, outside, clock, f'refused-{status}')
        for side in range(128, 320, 32): assert opened(run(context.map(SOFIA, side, 256))).size == (side, 256)
        assert len(outside.drawn()) == 1 and context.pictures.used() == (1, 1)  # one call told us; no other shape asks
        clock.forward(rest - 1); run(context.map(SOFIA, 384, 256)); assert len(outside.drawn()) == 1
        clock.forward(2); run(context.map(SOFIA, 416, 256)); assert len(outside.drawn()) == 2
    # A provider that is away rests the asked shape alone.
    away, clock = Provider(lambda request: httpx.Response(503, text='away')), Clock(); context = provided(tmp_path, away, clock, 'away')
    run(context.map(place(0))); run(context.map(place(0))); run(context.map(place(1))); assert len(away.drawn()) == 2


def test_host_ledger_is_one_file_for_the_user_of_this_host(tmp_path, monkeypatch):
    home = Path.home() / '.local/state/rocoto/location-context/map-pictures.json'
    assert lc.host_ledger({'XDG_STATE_HOME': str(tmp_path)}) == tmp_path / 'rocoto/location-context/map-pictures.json'
    # The XDG rule: an empty or relative value is set aside, so two working directories share one ledger.
    for value in ('', '  ', 'state', './state'): assert lc.host_ledger({'XDG_STATE_HOME': value}) == home
    assert lc.host_ledger({}) == home
    def homeless(): raise RuntimeError('no home')
    monkeypatch.setattr(lc.Path, 'home', staticmethod(homeless))
    assert lc.host_ledger({}) is None  # a user with no home keeps the tiles


def test_commissioning_command_makes_the_host_ledger_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    lc.main(['commission-map-ledger']); made = json.loads(capsys.readouterr().out)
    ledger = tmp_path / 'rocoto/location-context/map-pictures.json'
    assert made == {'ledger': str(ledger), 'made': True, 'calls_today': 0, 'calls_this_month': 0, 'daily_cap': 60, 'monthly_cap': 1000}
    assert json.loads(ledger.read_text()) == {'days': {}, 'months': {}} and ledger.stat().st_mode & 0o077 == 0 and ledger.parent.stat().st_mode & 0o077 == 0
    ledger.write_text(json.dumps({'days': {'2026-10-10': 7}, 'months': {'2026-10': 7}}))
    lc.main(['commission-map-ledger']); assert json.loads(capsys.readouterr().out)['made'] is False
    assert json.loads(ledger.read_text())['months'] == {'2026-10': 7}  # a standing ledger keeps its count


def test_tiles_picture_arrives_in_the_themes_quiet_tone_and_a_narrow_one_keeps_its_credit_whole(tmp_path):
    assert lc.theme_tone() == (0.42, 0.94, 1.04) == lc.MAP_TONE
    outside = Upstream(tile=tile_answer); context = service(tmp_path, outside)
    toned = opened(run(context.map(SOFIA))).convert('RGB'); x, y = lc.tile_position(SOFIA['latitude'], SOFIA['longitude'])
    raw = (round(x * 256) % 256, round(y * 256) % 256)
    red, green, blue = toned.getpixel((256, 256))
    assert abs(red - green) < abs(raw[0] - raw[1]) or raw[0] == raw[1]  # colours drawn towards grey by this service
    # The narrowest shape: the credit plate sits inside the picture with its inset.
    narrow = opened(run(context.map(SOFIA, 128, 128))).convert('RGB')
    from PIL import ImageChops
    lc_tone, lc.MAP_TONE = lc.MAP_TONE, None
    try: bare = opened(lc.credited(lc.compose({(dx, dy): TILES[(int(x) + dx - 1, int(y) + dy - 1)] for dx in range(3) for dy in range(3)}, ((x - int(x) + 1) * 256, (y - int(y) + 1) * 256)), 128, 128)).convert('RGB')
    finally: lc.MAP_TONE = lc_tone
    plain = opened(lc.compose({(dx, dy): TILES[(int(x) + dx - 1, int(y) + dy - 1)] for dx in range(3) for dy in range(3)}, ((x - int(x) + 1) * 256, (y - int(y) + 1) * 256))).convert('RGB').crop((192, 192, 320, 320))
    plate = ImageChops.difference(bare, plain).getbbox()
    assert plate and plate[0] >= 14 and plate[2] <= 113 and plate[3] <= 113 and narrow.size == (128, 128)


def test_tile_block_stays_on_the_map_at_its_edges():
    for latitude, longitude in ((89.9, 179.999), (-89.9, -180.0), (0.0, 0.0)):
        tiles, point = lc.tile_block(latitude, longitude)
        assert len(tiles) == 9 and all(0 <= column < 2 ** 16 and 0 <= row < 2 ** 16 for column, row in tiles.values())
        assert 0 <= point[0] <= 768 and 0 <= point[1] <= 768


# ---- routes ---------------------------------------------------------------------------------------------

WEATHER = {'name': 'Vitosha Boulevard 85', 'address': '85 Vitosha Boulevard, Sofia', 'city': 'Sofia',
           'latitude': 42.6869, 'longitude': 23.3186, 'timezone': 'Europe/Sofia',
           'coordinate_source': 'https://www.openstreetmap.org/', 'coordinate_precision': 'address',
           'provenance': 'Owner-supplied shop address.'}


def shop(app, client, tmp_path, outside, clock=None):
    from business_scope import ScopeActor
    from location_intelligence import coordinates_from_weather
    from test_skill_runtime import HEADERS
    client.post('/api/session', json={'code': app.state.access.issue('demo', owner=True, principal_id='peter')}, headers=HEADERS)
    workspace = app.state.workspaces['demo']; scope = workspace['business_scope']
    place = scope.mutate(ScopeActor(scope.company_id, 'peter'), 'cafe', 'locations',
                         {'name': WEATHER['name'], 'address': WEATHER['address']}, 0)['locations'][0]
    app.state.location_context = LocationContext(tmp_path / 'outside', transport=httpx.MockTransport(outside), clock=clock or Clock())
    return workspace, place['id'], lambda: workspace['location_intelligence'].configure_location(
        'peter', {'collection': 'cafe', 'location_id': place['id'], 'coordinates': coordinates_from_weather(WEATHER)})


def test_routes_answer_the_signed_in_company_for_its_own_location(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app, HEADERS
    app = make_app(tmp_path, monkeypatch); outside = Upstream(tile=tile_answer)
    assert isinstance(app.state.location_context, LocationContext) and app.state.location_context.transport is None
    assert app.state.location_context.directory == (tmp_path / 'state/location-context').absolute()
    # Provider calls of every server process of this host's user are counted in one ledger; with no key the tiles draw.
    assert app.state.location_context.pictures.path == lc.host_ledger().absolute() and app.state.location_context.provider is None
    with TestClient(app) as client:
        for path in ('/api/control-room/weather', '/api/control-room/map'):
            assert client.get(path).status_code == 401
        _, location_id, add_coordinates = shop(app, client, tmp_path, outside)
        # A location whose coordinates are still to add: both routes say so and call nobody.
        for path in ('/api/control-room/weather', '/api/control-room/map'):
            assert client.get(path).status_code == 404
        assert outside.requests == []
        add_coordinates()
        answer = client.get('/api/control-room/weather')
        assert answer.status_code == 200 and 'no-store' in answer.headers['cache-control']
        body = answer.json()
        assert (body['place'], body['city'], body['location_id']) == ('Vitosha Boulevard 85', 'Sofia', location_id)
        assert set(body['current']) >= {'temperature_c', 'condition', 'icon'} and len(body['hourly']) == 24 and len(body['daily']) == 7
        assert client.get('/api/control-room/weather', params={'location_id': location_id}).json()['current'] == body['current']
        assert client.get('/api/control-room/weather', params={'location_id': 'another-shop'}).status_code == 404
        assert len(outside.to('api.open-meteo.com')) == 1
        picture = client.get('/api/control-room/map')
        assert picture.status_code == 200 and picture.headers['content-type'] == 'image/png'
        assert picture.headers['cache-control'] == 'private, max-age=3600' and picture.headers['x-content-type-options'] == 'nosniff'
        assert opened(picture.content).size == (512, 512) and len(outside.to('tile.openstreetmap.org')) == 9
        assert opened(client.get('/api/control-room/map', params={'width': 512, 'height': 384}).content).size == (512, 384)
        assert client.get('/api/control-room/map', params={'width': 4096}).status_code == 400
        assert len(outside.requests) == 10
        context = app.state.location_context
        assert 'map' not in body  # the page shows every picture the same way and holds no word on who drew it
        # A commissioned provider: its picture is served as drawn, and its key stays inside this service.
        drawn, tiles_transport = Provider(), context.transport
        context.provider, context.transport = ('mapbox', TOKEN), httpx.MockTransport(drawn)
        context.pictures = lc.ProviderLedger(tmp_path / 'host/map-pictures.json', 60, 1000, clock=context.clock); context.pictures.commission()
        named = client.get('/api/control-room/weather'); shaped = client.get('/api/control-room/map', params={'width': 256, 'height': 128})
        assert opened(shaped.content).size == (512, 256) and len(drawn.drawn()) == 1
        for response in (named, shaped):
            assert TOKEN.encode() not in response.content and TOKEN not in json.dumps(dict(response.headers))
        context.provider, context.transport = None, tiles_transport
        # Another member of the company without this location sees neither.
        client.post('/api/session', json={'code': app.state.access.issue('demo', principal_id='other')}, headers=HEADERS)
        for path in ('/api/control-room/weather', '/api/control-room/map'):
            assert client.get(path).status_code == 404
        client.cookies.clear()
        assert client.get('/api/control-room/weather').status_code == 401
        assert len(outside.requests) == 10


def test_routes_answer_503_with_a_retry_time_while_the_upstream_is_away(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app
    app = make_app(tmp_path, monkeypatch); outside = Upstream(tile=tile_answer); outside.fail = True
    with TestClient(app) as client:
        _, _, add_coordinates = shop(app, client, tmp_path, outside); add_coordinates()
        for path in ('/api/control-room/weather', '/api/control-room/map'):
            answer = client.get(path)
            assert answer.status_code == 503 and answer.headers['retry-after'] == '300'


def test_withdrawn_access_takes_the_answer_back(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_skill_runtime import make_app
    app = make_app(tmp_path, monkeypatch); outside = Upstream(tile=tile_answer)
    with TestClient(app) as client:
        workspace, _, add_coordinates = shop(app, client, tmp_path, outside); add_coordinates()
        assert client.get('/api/control-room/weather').status_code == 200
        # The host's own check runs again before the answer leaves.
        monkeypatch.setattr(workspace['business_scope'], 'current', lambda *args, **kwargs: False)
        assert client.get('/api/control-room/weather').status_code == 403
        assert client.get('/api/control-room/map').status_code == 403


def test_page_policy_stays_closed_to_outside_hosts():
    import control_room_routes
    policy = control_room_routes.PRIVATE_VIEWER_CSP
    assert "connect-src 'self'" in policy and "img-src 'self' data:" in policy and 'http' not in policy
    widget = (Path(control_room_routes.CODE) / 'weather-widget.js').read_text()
    assert '/api/retail/location/weather' in widget and '/api/retail/location/map' in widget
    for host in ('open-meteo', 'openstreetmap.org/export', 'tile.openstreetmap'):
        assert host not in widget.replace('https://open-meteo.com/', '')
