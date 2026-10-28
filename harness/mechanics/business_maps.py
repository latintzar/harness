"""Owned, immutable map data published from generated JSON, with no network IO.

Validation proves bounded inert data and its current business binding. Public
citations and mapped facts remain attributed research, not host verification.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
import uuid

from business_context import public_url, _public_text
from chat_files import ChatFiles

MAX_BYTES = 4 * 1024 * 1024
MAX_FEATURES = 20_000
MAX_COORDINATES = 250_000
MAX_STORED_BYTES = 64 * 1024 * 1024
MAX_MAPS = 200
LAYERS = ('buildings', 'roads', 'green')
USES = {'residential', 'business', 'mixed', 'civic', 'unknown'}
POI_CATEGORIES = ('cafe', 'shop', 'transit', 'school', 'health', 'leisure')
SPEC = {'type': 'function', 'name': 'publish_business_map',
    'description': 'Publish an interactive neighbourhood map from a JSON file already published in this parent chat. Read get_business_profile.map_format for the exact JSON template, item formats, limits and source recipe; fill its source placeholders using researched evidence. Empty unsourced layers, POIs and catchments are supported. Publish the JSON with native publish_file, then pass its file_id and current profile_revision here. Returns a saved map reference; the app displays the map.',
    'inputSchema': {'type': 'object', 'additionalProperties': False,
        'properties': {'file_id': {'type': 'string', 'pattern': '^file-[a-f0-9]{32}$'},
            'expected_profile_revision': {'type': 'integer', 'minimum': 0}},
        'required': ['file_id', 'expected_profile_revision']}}

# This is the single host-provided authoring guide. Native workers receive it
# through get_business_profile; they do not need filesystem access to host code.
MAP_FORMAT = {
    'version': 1,
    'purpose': 'Sourced business-neighbourhood research with explicit geographic coverage and uncertainty.',
    'template_state': 'Null geometry/source fields are placeholders to fill from actual research before publication. Empty feature lists, POIs and catchments are valid coverage; they mean that layer is unavailable.',
    'template': {
        'kind': 'business_neighborhood', 'schema_version': 1,
        'binding': {'business_name': '', 'profile_location': ''},
        'origin': {'name': '', 'coordinates': None, 'address': '', 'source': None, 'url': None},
        'bbox': None,
        **{layer: {'type': 'FeatureCollection', 'features': []} for layer in LAYERS},
        'pois': [], 'catchments': [],
        'stats': {'totalBuildings': 0, 'taggedBuildings': 0, 'counts': {key: 0 for key in sorted(USES)}},
        'methodology': {'source': None, 'sourceTimestamp': None, 'walkingMethod': None,
            'walkingSpeedKmh': None, 'catchmentNote': None, 'uncertainty': []},
        'sources': [],
    },
    'item_formats': {
        'feature': {'type': 'Feature', 'properties': {}, 'geometry': {'type': 'Polygon', 'coordinates': []}},
        'poi': {'id': None, 'name': None, 'category': None, 'coordinates': None, 'url': None},
        'catchment': {'minutes': None, 'geometry': {'type': 'Polygon', 'coordinates': []}},
        'source': {'url': None, 'retrieved_at': None},
    },
    'limits': {'utf8_bytes': MAX_BYTES, 'aggregate_layer_features': MAX_FEATURES,
        'aggregate_coordinate_pairs': MAX_COORDINATES, 'pois': 200, 'catchments': 3, 'sources': 24,
        'feature_properties': 16, 'feature_property_key_characters': 64,
        'feature_property_text_characters': 1000, 'poi_id_characters': 128, 'poi_name_characters': 256,
        'origin_text_characters': 512, 'source_title_characters': 256,
        'methodology_text_characters': 4096, 'uncertainty_items': 16, 'uncertainty_text_characters': 2000},
    'structure': {
        'objects': 'Use exactly the template keys and listed optional item keys. JSON has unique keys, finite numbers, Unicode text, and inert data values.',
        'binding': 'business_name and profile_location exactly equal the saved profile.name and profile.location; origin.name exactly equals business_name. Both binding strings must be populated (limits 200 and 500 characters).',
        'origin': 'All five origin keys are required. coordinates is a sourced [longitude,latitude] pair; address and source are nonempty descriptions, url is its public source link.',
        'bbox': '[west,south,east,north], west<east and south<north. Longitude is within ±180 and latitude within ±85. Every coordinate lies inside the bbox. Neighbourhood diagonal <=20,000 metres, computed as hypot((east-west)*111320*cos(radians((south+north)/2)),(north-south)*111320).',
        'features': 'Feature has exactly type,properties,geometry; geometry has exactly type,coordinates. Buildings/green allow Polygon or MultiPolygon; roads allow LineString or MultiLineString. Feature properties are a flat object: text, booleans, null, or finite numbers within ±1e15. Empty source feature geometry is preserved as coordinates:[].',
        'geometry': 'Position=[longitude,latitude]. LineString=[position,...] needs >=2 positions. Polygon=[outer_closed_ring,...closed_holes]; each ring has >=4 positions and first equals last. MultiLineString=[LineString coordinates,...]; MultiPolygon=[Polygon coordinates,...]. Populated geometries use nonempty child lists.',
        'building_use': {'property': 'use', 'values': sorted(USES), 'missing_value': 'unknown'},
        'stats': 'Include all five counts keys. Count buildings.features by properties.use (missing means unknown), including empty source features. totalBuildings=sum(counts.values()); taggedBuildings=totalBuildings-counts.unknown. All values are nonnegative integers; empty buildings use all zeros.',
        'pois': {'required_keys': ['id', 'name', 'category', 'coordinates', 'url'],
            'optional_keys': ['walkMinutes'], 'categories': list(POI_CATEGORIES), 'reserved_id': 'origin',
            'rules': 'IDs are distinct nonempty strings. Name is nonempty; URL and coordinates come from sources. Omit walkMinutes when unavailable; a supplied value is finite, 0–240, and requires the declared walking method/speed.'},
        'catchments': 'Each has exactly minutes and geometry. Minutes are distinct integers from 5,10,15. Geometry is a nonempty Polygon or MultiPolygon backed by the declared method. Keep catchments:[] when evidence is unavailable; a missing area stays missing.',
        'sources': 'Provide 1–24 objects with url and retrieved_at, optionally title. retrieved_at is the actual retrieval timestamp as a timezone-qualified ISO datetime (<=64 characters). methodology.sourceTimestamp is the source dataset timestamp in the same format, or null when unknown; retrieval time is a separate fact.',
        'urls': 'Public HTTP(S) links <=2000 characters, with a public domain or global IP, optional port 80/443, and credentials kept in private setup. Benign query parameters (<=16, total <=1000 characters, keys <=100, values <=500) and fragments <=500 are supported. Credential/signature/authentication parameters are rejected.',
        'methodology': {
            'required_keys': ['source', 'sourceTimestamp', 'walkingMethod', 'walkingSpeedKmh', 'catchmentNote', 'uncertainty'],
            'optional_text_keys': ['attribution', 'poiTimeNote', 'buildingUseNote', 'projectionNote'],
            'optional_link_keys': ['license'],
            'optional_numeric_keys': {'radiusMeters': [0, 10000], 'originConnectorMeters': [0, 10000],
                'geometrySimplificationMeters': [0, 10000]},
            'optional_integer_keys': {'walkingGraphNodes': [0, 1000000], 'walkingGraphEdges': [0, 1000000]},
            'walking_rules': 'With supplied catchments or POI walking estimates, walkingMethod is network or straight_line_estimate and walkingSpeedKmh is finite within 1–10. State the actual computation and assumptions in catchmentNote. With neither, both fields may be null.',
            'text_rules': 'source and catchmentNote are nonempty plain descriptions; uncertainty contains 1–16 nonempty caveats. Preserve unavailable layers and source limitations explicitly.',
        },
    },
    'recipe': [
        'Save the researched business profile, then read get_business_profile again for its current profile_revision and bound template.',
        'Research the actual site and obtain sourced longitude/latitude. Establish the intended site for multi-location businesses. Continue with other researched profile facts if location evidence remains insufficient.',
        'Fill origin and a bounded viewport around its sourced coordinates. Add only acquired or explicitly computed geographic evidence; keep unsourced layers/POIs/catchments empty. Record sources, actual retrieval times, method assumptions and uncertainty.',
        'Compute stats from the supplied building feature list. Retain unknown source timestamps as null. For an origin-only map, keep method/speed null and describe its coverage in catchmentNote and uncertainty.',
        'Write UTF-8 JSON under /workspace/outputs and use native publish_file with its relative path. Then call publish_business_map with the returned file_id and the saved profile_revision. The parent publishes; child research returns evidence to the parent.',
    ],
}


def format_guide(business):
    guide = copy.deepcopy(MAP_FORMAT)
    profile = business['profile']
    guide['template']['binding'].update(business_name=profile['name'], profile_location=profile['location'])
    guide['template']['origin'].update(name=profile['name'], address=profile['location'])
    guide['publish'] = {'tool': SPEC['name'], 'expected_profile_revision': business['profile_revision'],
        'file_id': 'Use the file_id returned by native publish_file for this parent chat.'}
    return guide


def _fail():
    raise ValueError('Use the exact map_format template and constraints returned by get_business_profile; fill its source fields from research.')


def _text(value, limit=1000, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip())
            or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
        _fail()
    return _public_text(value)


def _number(value, minimum, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        _fail()
    return value


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        _fail()
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() is None:
            _fail()
    except ValueError:
        _fail()


def _url(value):
    # URLs are inert links, never fetched. Permit the reference's normal locale
    # and map query parameters; reject credential-bearing links and private hosts.
    try:
        if (not isinstance(value, str) or len(value) > 2000 or any(c.isspace() or ord(c) < 32 for c in value)
                or '\\' in value):
            _fail()
        parsed = urlsplit(value)
        public_url(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', '')))
        if len(parsed.query) > 1000:
            _fail()
        _text(unquote(parsed.fragment), 500, empty=True)
        for key, val in parse_qsl(parsed.query, max_num_fields=16):
            if re.search(r'token|secret|passw|auth|credential|signature|api.?key|^key$', key, re.I):
                _fail()
            _text(key, 100); _text(val, 500, empty=True)
    except (ValueError, UnicodeError):
        _fail()
    return value


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result or key in {'__proto__', 'constructor', 'prototype'}:
            _fail()
        result[key] = value
    return result


def decode(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_BYTES:
        _fail()
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
            parse_constant=lambda _: _fail())
    except (UnicodeError, ValueError, RecursionError):
        _fail()


def validate(raw):
    pack = decode(raw)
    required = {'kind', 'schema_version', 'binding', 'origin', 'bbox', 'sources', 'pois',
        'catchments', 'methodology', 'stats', *LAYERS}
    if (not isinstance(pack, dict) or set(pack) != required or pack['kind'] != 'business_neighborhood'
            or type(pack['schema_version']) is not int or pack['schema_version'] != 1):
        _fail()
    binding = pack['binding']
    if not isinstance(binding, dict) or set(binding) != {'business_name', 'profile_location'}:
        _fail()
    _text(binding['business_name'], 200); _text(binding['profile_location'], 500)
    bbox = pack['bbox']
    if not isinstance(bbox, list) or len(bbox) != 4:
        _fail()
    west, south, east, north = bbox
    _number(west, -180, 180); _number(east, -180, 180)
    _number(south, -85, 85); _number(north, -85, 85)
    if not west < east or not south < north or math.hypot(
            (east-west)*111_320*math.cos(math.radians((south+north)/2)), (north-south)*111_320) > 20_000:
        _fail()
    coordinate_count = 0
    def coordinate(value):
        nonlocal coordinate_count
        if not isinstance(value, list) or len(value) != 2:
            _fail()
        _number(value[0], west, east); _number(value[1], south, north)
        coordinate_count += 1
        if coordinate_count > MAX_COORDINATES:
            _fail()
    def line(values, ring=False):
        if not isinstance(values, list) or len(values) < (4 if ring else 2):
            _fail()
        for value in values:
            coordinate(value)
        if ring and values[0] != values[-1]:
            _fail()
    def polygon(values):
        if not isinstance(values, list) or not values:
            _fail()
        for ring in values:
            line(ring, True)
    def geometry(value, allowed, *, empty=False):
        if (not isinstance(value, dict) or set(value) != {'type', 'coordinates'}
                or not isinstance(value['type'], str) or value['type'] not in allowed):
            _fail()
        kind, coords = value['type'], value['coordinates']
        # Empty GeoJSON source features carry metadata but draw no geometry.
        # Preserve them from the original data; a claimed catchment stays nonempty.
        if empty and coords == []:
            return
        if kind == 'LineString':
            line(coords)
        elif kind == 'Polygon':
            polygon(coords)
        else:
            if not isinstance(coords, list) or not coords:
                _fail()
            for child in coords:
                (line if kind == 'MultiLineString' else polygon)(child)
    origin = pack['origin']
    if not isinstance(origin, dict) or set(origin) != {'name', 'coordinates', 'address', 'source', 'url'}:
        _fail()
    for key in ('name', 'address', 'source'):
        _text(origin[key], 512)
    if origin['name'] != binding['business_name']:
        _fail()
    coordinate(origin['coordinates']); _url(origin['url'])
    feature_count = 0
    counts = {key: 0 for key in USES}
    for layer in LAYERS:
        collection = pack[layer]
        if (not isinstance(collection, dict) or set(collection) != {'type', 'features'}
                or collection['type'] != 'FeatureCollection' or not isinstance(collection['features'], list)):
            _fail()
        feature_count += len(collection['features'])
        if feature_count > MAX_FEATURES:
            _fail()
        for feature in collection['features']:
            if not isinstance(feature, dict) or set(feature) != {'type', 'properties', 'geometry'} or feature['type'] != 'Feature':
                _fail()
            props = feature['properties']
            if not isinstance(props, dict) or len(props) > 16:
                _fail()
            for key, value in props.items():
                _text(key, 64)
                if isinstance(value, str):
                    _text(value, 1000, empty=True)
                elif value is not None and type(value) is not bool:
                    _number(value, -10**15, 10**15)
            geometry(feature['geometry'], {'LineString', 'MultiLineString'} if layer == 'roads' else {'Polygon', 'MultiPolygon'}, empty=True)
            if layer == 'buildings':
                use = props.get('use', 'unknown')
                if not isinstance(use, str) or use not in USES:
                    _fail()
                counts[use] += 1
    stats = pack['stats']
    if (not isinstance(stats, dict) or set(stats) != {'totalBuildings', 'taggedBuildings', 'counts'}
            or type(stats['totalBuildings']) is not int or type(stats['taggedBuildings']) is not int
            or stats['totalBuildings'] != sum(counts.values())
            or stats['taggedBuildings'] != sum(counts.values()) - counts['unknown']
            or not isinstance(stats['counts'], dict) or stats['counts'] != counts
            or any(type(value) is not int for value in stats['counts'].values())):
        _fail()
    pois = pack['pois']
    if not isinstance(pois, list) or len(pois) > 200:
        _fail()
    ids = {'origin'}
    for poi in pois:
        if not isinstance(poi, dict) or set(poi) - {'id', 'name', 'category', 'coordinates', 'url', 'walkMinutes'} or not {'id', 'name', 'category', 'coordinates', 'url'} <= set(poi):
            _fail()
        _text(poi['id'], 128); _text(poi['name'], 256)
        if not isinstance(poi['category'], str) or poi['category'] not in POI_CATEGORIES:
            _fail()
        if poi['id'] in ids:
            _fail()
        ids.add(poi['id']); coordinate(poi['coordinates']); _url(poi['url'])
        if 'walkMinutes' in poi:
            _number(poi['walkMinutes'], 0, 240)
    catchments = pack['catchments']
    if not isinstance(catchments, list) or len(catchments) > 3:
        _fail()
    minutes = set()
    for item in catchments:
        if (not isinstance(item, dict) or set(item) != {'minutes', 'geometry'}
                or type(item['minutes']) is not int or item['minutes'] not in {5, 10, 15} or item['minutes'] in minutes):
            _fail()
        minutes.add(item['minutes']); geometry(item['geometry'], {'Polygon', 'MultiPolygon'})
    sources = pack['sources']
    if not isinstance(sources, list) or not 1 <= len(sources) <= 24:
        _fail()
    for source in sources:
        if not isinstance(source, dict) or set(source) - {'url', 'retrieved_at', 'title'} or not {'url', 'retrieved_at'} <= set(source):
            _fail()
        _url(source['url']); _timestamp(source['retrieved_at'])
        if 'title' in source:
            _text(source['title'], 256)
    methodology = pack['methodology']
    allowed = {'source', 'sourceTimestamp', 'attribution', 'license', 'radiusMeters', 'walkingSpeedKmh',
        'originConnectorMeters', 'walkingGraphNodes', 'walkingGraphEdges', 'catchmentNote', 'poiTimeNote',
        'buildingUseNote', 'projectionNote', 'geometrySimplificationMeters', 'walkingMethod', 'uncertainty'}
    if not isinstance(methodology, dict) or set(methodology) - allowed or not {
            'source', 'sourceTimestamp', 'walkingSpeedKmh', 'catchmentNote', 'walkingMethod', 'uncertainty'} <= set(methodology):
        _fail()
    walking = bool(catchments) or any('walkMinutes' in poi for poi in pois)
    empty_walking = not walking and methodology['walkingMethod'] is None and methodology['walkingSpeedKmh'] is None
    if not empty_walking:
        if not isinstance(methodology['walkingMethod'], str) or methodology['walkingMethod'] not in {'network', 'straight_line_estimate'}:
            _fail()
        _number(methodology['walkingSpeedKmh'], 1, 10)
    if methodology['sourceTimestamp'] is not None:
        _timestamp(methodology['sourceTimestamp'])
    uncertainty = methodology['uncertainty']
    if not isinstance(uncertainty, list) or not 1 <= len(uncertainty) <= 16:
        _fail()
    for item in uncertainty:
        _text(item, 2000)
    for key, value in methodology.items():
        if key in {'sourceTimestamp', 'uncertainty', 'walkingMethod', 'walkingSpeedKmh'}:
            continue
        if key == 'license':
            _url(value)
        elif key in {'radiusMeters', 'originConnectorMeters', 'geometrySimplificationMeters'}:
            _number(value, 0, 10_000)
        elif key in {'walkingGraphNodes', 'walkingGraphEdges'}:
            if type(value) is not int:
                _fail()
            _number(value, 0, 1_000_000)
        else:
            _text(value, 4096)
    return pack


def current_member(access_path, environment, owner):
    """Cheap host-selected membership fence; logout leaves durable ownership intact."""
    try:
        path = Path(access_path)
        if path.is_symlink():
            return False
        db = sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=0)
        try:
            return db.execute('''SELECT 1 FROM principal_environments m
                JOIN principals p ON p.id=m.principal_id JOIN environments e ON e.id=m.environment
                WHERE m.principal_id=? AND m.environment=? AND m.role IN ('owner','operator')''',
                (owner, environment)).fetchone() is not None
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        return False


class BusinessMaps:
    def __init__(self, state):
        self.state = Path(state)
        self.path = self.state / 'business-maps.sqlite'
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS maps (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, chat_id TEXT NOT NULL, collection TEXT NOT NULL,
                profile_revision INTEGER NOT NULL, business_name TEXT NOT NULL, profile_location TEXT NOT NULL,
                created_at TEXT NOT NULL, source_file_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
                sha256 TEXT NOT NULL, data BLOB NOT NULL,
                UNIQUE(owner,chat_id,collection,source_file_id,source_sha256,profile_revision));
                CREATE INDEX IF NOT EXISTS maps_scope ON maps(owner,collection,created_at);
                CREATE TRIGGER IF NOT EXISTS immutable_map_update BEFORE UPDATE ON maps
                BEGIN SELECT RAISE(ABORT,'Published map data is immutable'); END;''')
        self.path.chmod(0o600)

    @staticmethod
    def metadata(row, stale=False):
        return {'id': row['id'], 'collection': row['collection'], 'title': row['business_name'],
            'url': '/api/business/map?' + urlencode({'collection': row['collection'], 'map_id': row['id']}),
            'profile_revision': row['profile_revision'], 'created_at': row['created_at'], 'stale': stale,
            'provenance': 'agent_research', 'validation': 'bounded_geometry_and_profile_binding'}

    def publish(self, files, business, *, file_id, expected_profile_revision, owner, chat_id, collection, current, created):
        ChatFiles._scope(owner, chat_id, collection)
        if (not isinstance(file_id, str) or not re.fullmatch(r'file-[a-f0-9]{32}', file_id)
                or type(expected_profile_revision) is not int or expected_profile_revision < 0 or not current()):
            raise ValueError('Publish this map in its active owning chat and current business revision.')
        authority = dict(owner_principal_id=owner, chat_id=chat_id, dataset=collection)
        record = files.get(file_id, **authority)
        if Path(record['name']).suffix.lower() != '.json' or record['size_bytes'] > MAX_BYTES:
            raise ValueError('Publish a generated JSON map of up to 4 MiB.')
        record, raw = files.download(file_id, **authority)
        source_sha = hashlib.sha256(raw).hexdigest()
        if source_sha != record['sha256']:
            raise ValueError('Refresh the original generated map file.')
        pack = validate(raw)
        data = json.dumps(pack, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
        if len(data) > MAX_BYTES:
            _fail()
        # The profile lock spans the CAS and publication in the separate attached
        # store. A profile edit cannot race a checked name/location binding.
        with business.connect() as db:
            db.execute('ATTACH DATABASE ? AS map_store', (str(self.path),))
            db.execute('BEGIN IMMEDIATE')
            business._scope(db, collection)
            profile = business._current(db, collection)
            if (profile['profile_revision'] != expected_profile_revision
                    or pack['binding'] != {'business_name': profile['profile']['name'], 'profile_location': profile['profile']['location']}):
                raise ValueError('Refresh the business name, location and profile revision before publishing this map.')
            if not current():
                raise ValueError('The map publication request has finished.')
            prior = db.execute('SELECT * FROM map_store.maps WHERE owner=? AND chat_id=? AND collection=? AND source_file_id=? AND source_sha256=? AND profile_revision=?',
                (owner, chat_id, collection, file_id, source_sha, expected_profile_revision)).fetchone()
            if prior:
                return self.metadata(prior)
            count, used = db.execute('SELECT COUNT(*),COALESCE(SUM(length(data)),0) FROM map_store.maps').fetchone()
            if count >= MAX_MAPS or used + len(data) > MAX_STORED_BYTES:
                raise ValueError('This workspace’s saved map allowance is full.')
            key = 'map-' + uuid.uuid4().hex
            values = (key, owner, chat_id, collection, expected_profile_revision, pack['binding']['business_name'],
                pack['binding']['profile_location'], datetime.now(timezone.utc).isoformat(), file_id, source_sha,
                hashlib.sha256(data).hexdigest(), data)
            db.execute('INSERT INTO map_store.maps VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', values)
            if not current():
                raise ValueError('The map publication request has finished.')
            row = db.execute('SELECT * FROM map_store.maps WHERE id=?', (key,)).fetchone()
        created.append(key)
        return self.metadata(row)

    def discard(self, ids, *, owner, chat_id, collection):
        """Remove only this interrupted publication's newly inserted rows."""
        with sqlite3.connect(self.path) as db:
            db.executemany('DELETE FROM maps WHERE id=? AND owner=? AND chat_id=? AND collection=?',
                [(key, owner, chat_id, collection) for key in ids])

    def get(self, business, *, owner, collection, map_id=None):
        ChatFiles._scope(owner, dataset=collection)
        if map_id is not None and not re.fullmatch(r'map-[a-f0-9]{32}', map_id):
            raise ValueError('Choose a map from this business collection.')
        with business.connect() as db:
            db.execute('ATTACH DATABASE ? AS map_store', (str(self.path),))
            db.execute('BEGIN')
            business._scope(db, collection)
            profile = business._current(db, collection)['profile']
            row = db.execute('SELECT * FROM map_store.maps WHERE owner=? AND collection=?' +
                (' AND id=?' if map_id else ' ORDER BY created_at DESC LIMIT 1'),
                (owner, collection, map_id) if map_id else (owner, collection)).fetchone()
        if row is None:
            if map_id:
                raise ValueError('Choose a map belonging to this person and business collection.')
            return {'status': 'empty', 'map': None, 'data': None}
        stale = (row['business_name'], row['profile_location']) != (profile['name'], profile['location'])
        metadata = self.metadata(row, stale)
        if stale and map_id is None:
            return {'status': 'stale', 'map': metadata, 'data': None}
        raw = row['data']
        if not isinstance(raw, bytes) or len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('Refresh the saved map record.')
        return {'status': 'stale' if stale else 'ready', 'map': metadata, 'data': decode(raw)}
