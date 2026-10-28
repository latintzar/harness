"""Retail presentation adapter: shelf/product vocabulary and location-scoped saved revisions.

The SDK owns validation and page projection; retail owns records, management access,
tracker lifecycle and trusted source fields. Existing saved presentation IDs remain readable.
"""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from display_presentation import PresentationKit, Unfilled, extend_kit, SHAPE, _number, _text, _records, inert, short

KIT = extend_kit(json.loads(Path(__file__).with_name('presentation_kit.json').read_text()))
STATES = ('empty', 'replenished', 'stocked')
KEEP_REVISIONS = 50
READ_AGAIN = 'Read the presentation again with get_tracker_presentation, then save with its current revision.'
MANAGEMENT = 'Use location management access to change this tracker’s presentation.'
DISPLAY_KIT = PresentationKit(KIT,
    shapes={
        'shelves': lambda value: _records(value, lambda s: _text(s.get('shelf')) and _text(s.get('label')) and s.get('state') in STATES),
        'ranked': lambda value: _records(value, lambda r: _text(r.get('product')) and _number(r.get('value'))),
    },
    aliases={'metric_history': 'metric', 'ranking_product': 'ranking', 'state_replenishment': 'state', 'live_updates': 'live'},
    envelope=('key', 'tile_family', 'canvas_family', 'company_id', 'location_id', 'camera_ids', 'tracker_id', 'core', 'chat', 'locked', 'script_revision'),
    row_fields={'rows': ('id', 'at', 'label', 'description', 'product', 'shelf', 'at_s'),
                'timed_rows': ('id', 'at', 'label', 'description', 'product', 'shelf', 'at_s'),
                'ranked': ('product', 'value', 'picked', 'returned', 'bought')},
    views={'state': 'shelves'}, default_label='Tracker')
SCHEMA_ID = DISPLAY_KIT.schema_id
LIMITS = DISPLAY_KIT.limits
MARKS = DISPLAY_KIT.marks
FORMATS = DISPLAY_KIT.formats
FAMILIES = DISPLAY_KIT.families
BLOCKS = DISPLAY_KIT.blocks
SHAPES = DISPLAY_KIT.shapes
FIELD = DISPLAY_KIT.field
FITS = DISPLAY_KIT.fits
ENVELOPE = DISPLAY_KIT.envelope
SCHEMA = DISPLAY_KIT.schema
TOO_LARGE = DISPLAY_KIT.too_large
output_fields = DISPLAY_KIT.output_fields
check_output = DISPLAY_KIT.check_output
family_blocks = DISPLAY_KIT.family_blocks
default_presentation = DISPLAY_KIT.default_presentation
merge_presentation = DISPLAY_KIT.merge_presentation
validate_presentation = DISPLAY_KIT.validate_presentation
families_for = DISPLAY_KIT.families_for
canvas_for_page = DISPLAY_KIT.canvas_for_page
apply_to_tile = DISPLAY_KIT.apply_to_tile


def record_id(tracker):
    return 'presentation-' + tracker['id'].removeprefix('tracker-')


def public(record):
    """A saved presentation as a tool, a route and the page carry it."""
    return {'schema': SCHEMA_ID, 'tile': record['tile'], 'canvas': record['canvas'], 'revision': record['revision'],
            'saved_at': record.get('saved_at'), 'saved_by': record.get('saved_by')}


def belongs(saved, tracker):
    """Whether a saved presentation is this tracker's own. A tracker's id comes from its key, so a
    tracker made again under a removed tracker's key meets that one's rows until they are cleared:
    a presentation saved at a later revision than the tracker has reached was the earlier one's."""
    return bool(saved) and saved.get('tracker_revision', 0) <= tracker.get('revision', 1)


def presentation_of(tracker, saved=None, output=None):
    """A tracker's presentation: the saved one, or its default at revision 0."""
    if belongs(saved, tracker):
        return public(saved)
    return {**default_presentation(tracker, output), 'revision': 0, 'saved_at': None, 'saved_by': None}


def forget(db, owner, tracker):
    """Remove a tracker's saved presentation and every kept revision, inside the caller's
    transaction. A tracker's removal calls this beside the rows of its script."""
    key = record_id(tracker)
    db.execute("DELETE FROM records WHERE owner=? AND kind='tracker_presentation' AND id=?", (owner, key))
    db.execute("DELETE FROM records WHERE owner=? AND kind='tracker_presentation_revision' AND id LIKE ?", (owner, key + '-r%'))


def forget_removed(company, location_id, trackers):
    """Clear the presentations of trackers a location has removed; returns how many. The page pass
    calls this with the location's trackers, so a key used again starts with its own presentation."""
    with company.locations.connect() as db:
        held = [] if db is None else [json.loads(row[0])['tracker_id'] for row in db.execute(
            "SELECT body FROM records WHERE owner=? AND kind IN ('tracker_presentation','tracker_presentation_revision') AND collection=? AND location=?",
            (company.owner, company.collection, location_id))]
    gone = sorted(set(held) - {tracker['id'] for tracker in trackers})
    if gone:
        with company.locations.connect(True) as db:
            for tracker_id in gone:
                forget(db, company.owner, {'id': tracker_id})
    return len(gone)


def _read(state, query, values):
    path = Path(state) / 'location-intelligence.sqlite'
    if not path.is_file():
        return []
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            return [json.loads(row[0]) for row in db.execute(query, values)]
    except sqlite3.OperationalError:
        return []


def saved_presentations(state, owner, collection):
    """The owner's saved presentations of a collection by tracker id, read without opening the company for writing."""
    rows = _read(state, "SELECT body FROM records WHERE owner=? AND kind='tracker_presentation' AND collection=?", (owner, collection))
    return {row['tracker_id']: row for row in rows}


def saved_revisions(state, owner, tracker):
    """Every kept revision of one tracker's presentation, newest first."""
    head = _read(state, "SELECT body FROM records WHERE owner=? AND kind='tracker_presentation' AND id=?", (owner, record_id(tracker)))
    if not head or not belongs(head[0], tracker):
        return []
    rows = _read(state, "SELECT body FROM records WHERE owner=? AND kind='tracker_presentation_revision' AND collection=? AND location=?",
                 (owner, tracker['collection'], tracker['location_id']))
    return sorted((public(row) for row in rows if row.get('tracker_id') == tracker['id'] and row['revision'] <= head[0]['revision']), key=lambda row: -row['revision'])


def current_output(state, owner, tracker):
    """The tracker's current output revision, once it has one."""
    rows = _read(state, "SELECT body FROM records WHERE owner=? AND kind='tracker_output' AND id=?",
                 (owner, 'output-' + tracker['id'].removeprefix('tracker-')))
    return rows[0] if rows else None


def save_presentation(company, tracker, change, *, expected_revision=None, saved_by='harness', clock=time.time):
    """Save a company tracker's presentation as its next revision; returns it in its public form.

    The change names the tile, the canvas or both. It is merged over the saved presentation (or the
    default), checked against the kit and, for the part it names, against the tracker's current
    output, and written with a copy among the revisions, in one transaction that also compares
    `expected_revision`. A Core Tracker answers `core_trackers.LOCKED`. The write touches the two
    presentation rows alone.
    """
    from core_trackers import LOCKED
    from location_intelligence import encoded
    import retail_records as records
    if tracker.get('core') is not False or tracker.get('locked'):
        raise ValueError(LOCKED)
    owner, collection, location_id = company.owner, company.collection, tracker['location_id']
    try:
        company.locations.authorized(owner, collection, location_id, write=True)
    except ValueError:
        raise ValueError(MANAGEMENT) from None
    output = current_output(company.state, owner, tracker)
    key = record_id(tracker)
    with company.locations.connect(True) as db:
        row = db.execute("SELECT body FROM records WHERE owner=? AND kind='tracker_presentation' AND id=?", (owner, key)).fetchone()
        prior = json.loads(row['body']) if row else None
        if prior and not belongs(prior, tracker):
            forget(db, owner, tracker)  # rows of a removed tracker that had this key
            prior = None
        revision = prior['revision'] if prior else 0
        if expected_revision != revision and (prior or expected_revision is not None):
            raise ValueError(READ_AGAIN)
        spec = DISPLAY_KIT.revise(presentation_of(tracker, prior, output), change,
            output=output, expected_revision=expected_revision, saved_by=saved_by,
            saved_at=datetime.fromtimestamp(round(clock(), 3), timezone.utc).isoformat().replace('+00:00', 'Z'))
        saved = records.stamp(company, location_id, {'id': key, 'tracker_id': tracker['id'], 'tracker_key': tracker['key'], **spec,
            'tracker_revision': tracker.get('revision', 1), 'revision': revision + 1})
        for kind, name in (('tracker_presentation', key), ('tracker_presentation_revision', f'{key}-r{revision + 1}')):
            db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?)', (owner, kind, name, collection, location_id, encoded({**saved, 'id': name})))
        if revision + 1 > KEEP_REVISIONS:
            db.execute("DELETE FROM records WHERE owner=? AND kind='tracker_presentation_revision' AND id=?",
                       (owner, f'{key}-r{revision + 1 - KEEP_REVISIONS}'))
    return public(saved)
