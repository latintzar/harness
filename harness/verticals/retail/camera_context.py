"""Versioned camera-view context, confirmed by an operator and bound to evidence.

Initial identities are imported source views. A matching filename or camera label
does not merge independent recordings into a physical camera. Confirmed context is
reference data for models; it grants no tool, network or execution authority.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from library import SourceLibrary, read_json


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class Zone(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)

    @model_validator(mode="after")
    def geometry(self):
        if not self.name.strip():
            raise ValueError("Give the zone a name.")
        if self.bbox is not None:
            x, y, width, height = self.bbox
            if not (0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1-x and 0 < height <= 1-y):
                raise ValueError("Place the zone inside the normalized source frame.")
        return self


class ContextDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    anchor_clip_id: str = Field(min_length=1, max_length=150)
    valid_from_seconds: float = Field(ge=0, allow_inf_nan=False)
    room: str = Field(default="", max_length=160)
    zones: list[Zone] = Field(default_factory=list, max_length=20)
    layout_notes: str = Field(default="", max_length=6000)
    change_type: Literal["context_update", "camera_moved"] = "context_update"
    observation_ref: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def content(self):
        if not (self.room.strip() or self.zones or self.layout_notes.strip()):
            raise ValueError("Add room, zone or layout context for review.")
        return self


class CameraStore:
    def __init__(self, state, index):
        self.state, self.index = Path(state), index
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.state/"camera-context.sqlite"
        self.library = SourceLibrary(state, index)
        self.hash_cache = {}
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cameras (id TEXT PRIMARY KEY, asset_id TEXT UNIQUE NOT NULL,
                    collection TEXT NOT NULL, name TEXT NOT NULL, revision INTEGER NOT NULL,
                    draft_id TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS camera_drafts (id TEXT PRIMARY KEY, camera_id TEXT NOT NULL,
                    body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS camera_views (camera_id TEXT NOT NULL, view_revision INTEGER NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(camera_id,view_revision));
            """)
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def camera_id(record):
        key = json.dumps([record["dataset"], record["video_id"]], separators=(",", ":"))
        return "camera-" + hashlib.sha256(key.encode()).hexdigest()[:24]

    def sync(self):
        # Refresh from the committed in-memory index; no directories are traversed.
        records = {c["video_id"]:c for c in self.index.clips}
        with self.connect() as db:
            for record in records.values():
                db.execute("INSERT OR IGNORE INTO cameras VALUES (?,?,?,?,1,NULL,?)",
                    (self.camera_id(record),record["video_id"],record["dataset"],record["camera"],timestamp()))
        return records

    def _row(self, db, camera_id, collection=None):
        row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if row is None or (collection is not None and row["collection"] != collection):
            raise KeyError("Choose a camera in this collection.")
        if not any(c["video_id"] == row["asset_id"] and c["dataset"] == row["collection"] for c in self.index.clips):
            raise KeyError("Choose a currently indexed camera view.")
        return dict(row)

    def _records(self, row):
        return sorted([c for c in self.index.clips if c["video_id"] == row["asset_id"] and c["dataset"] == row["collection"]],
                      key=lambda c:(c["start"],c["id"]))

    @staticmethod
    def _views(db, camera_id):
        return [json.loads(r["body"]) for r in db.execute(
            "SELECT body FROM camera_views WHERE camera_id=? ORDER BY view_revision", (camera_id,))]

    def _public(self, db, row, *, details=False):
        clips = self._records(row)
        reference = clips[0]
        views = self._views(db,row["id"])
        draft = db.execute("SELECT body FROM camera_drafts WHERE id=?", (row["draft_id"],)).fetchone() if row["draft_id"] else None
        result = {"id":row["id"],"name":row["name"],"collection":row["collection"],"revision":row["revision"],
            "view_revision":views[-1]["view_revision"] if views else 0,
            "identity_basis":"observed_source_view", "asset_ids":[row["asset_id"]],"clip_count":len(clips),
            "reference_clip":{**self.library.public_clip(reference),"sample_time":reference.get("sample_time")},
            "source_metadata":{k:reference.get(k) for k in ("camera_semantics","provenance","source_reference")},
            "confirmed_context":views[-1] if views else None,"draft":json.loads(draft["body"]) if draft else None,
            "url":f"/?cameras=1&collection={row['collection']}&camera={row['id']}"}
        if details:
            result["history"] = views
            result["assets"] = [self.library.public_asset(self.library.resolve(row["asset_id"]), include_clips=True)]
            result["descriptions"] = self.observations(clips)
        return result

    def list(self, collection):
        self.sync()
        if collection not in {c["dataset"] for c in self.index.clips}:
            raise ValueError("Choose an indexed camera collection.")
        active = {c["video_id"] for c in self.index.clips if c["dataset"] == collection}
        with self.connect() as db:
            rows = db.execute("SELECT * FROM cameras WHERE collection=? ORDER BY name,id", (collection,)).fetchall()
            return {"collection":collection,"cameras":[self._public(db,dict(r)) for r in rows if r["asset_id"] in active]}

    def get(self, camera_id, collection=None):
        self.sync()
        with self.connect() as db:
            return self._public(db,self._row(db,camera_id,collection),details=True)

    def observations(self, clips):
        observations = []
        for clip in clips:
            entries = read_json(self.state/"descriptions"/(clip["id"]+".json"), [])
            for observation in entries if isinstance(entries,list) else []:
                if not isinstance(observation,dict):
                    continue
                data = {k:observation.get(k) for k in ("model","model_revision","created_at","text","prompt","verification","source_url")}
                data["clip_id"] = clip["id"]
                data["observation_ref"] = hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
                observations.append(data)
        return observations

    def _source_hash(self, record):
        path = Path(record["path"])
        stat = path.stat()
        key = (str(path.resolve()),stat.st_mtime_ns,stat.st_size)
        if key not in self.hash_cache:
            with path.open("rb") as source:
                value = hashlib.file_digest(source,"sha256").hexdigest()
            after = path.stat()
            if (after.st_mtime_ns,after.st_size) != (stat.st_mtime_ns,stat.st_size):
                raise ValueError("Finish importing this source before confirming context.")
            self.hash_cache[key] = value
        return self.hash_cache[key]

    def propose(self, camera_id, body, *, origin, collection=None):
        spec = ContextDraft.model_validate(body).model_dump()
        if origin not in {"operator_note","agent_proposal"}:
            raise ValueError("Use the registered camera context controls.")
        self.sync()
        # Hash outside the write transaction; confirm checks the source again.
        with self.connect() as db:
            row = self._row(db,camera_id,collection)
        clip = self.index.get_clip(spec["anchor_clip_id"])
        if not clip or (clip["video_id"],clip["dataset"]) != (row["asset_id"],row["collection"]):
            raise ValueError("Anchor context to a clip from this camera view.")
        if not float(clip["start"]) <= spec["valid_from_seconds"] < float(clip["end"]):
            raise ValueError("Choose a start time inside the reference clip.")
        observation = None
        if spec["observation_ref"]:
            observation = next((o for o in self.observations([clip]) if o["observation_ref"] == spec["observation_ref"]),None)
            if observation is None:
                raise ValueError("Choose an existing model observation from the reference clip.")
        draft = {"id":"draft-"+uuid.uuid4().hex,"context":{k:spec[k] for k in ("room","zones","layout_notes")},
            "anchor_clip_id":clip["id"],"valid_from_seconds":spec["valid_from_seconds"],"change_type":spec["change_type"],
            "created_at":timestamp(),"provenance":{"proposal_origin":origin,"source_asset_id":clip["video_id"],
                "source_sha256":self._source_hash(clip),"anchor_clip_id":clip["id"],"anchor_seconds":clip.get("sample_time",clip["start"]),
                "anchor_thumbnail_url":f"/thumbnails/{clip['id']}.jpg", "source_url":f"/?clip={clip['id']}",
                "valid_from_seconds":spec["valid_from_seconds"],"coordinate_space":"normalized_full_source_frame",
                "observation":observation}}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db,camera_id,collection)
            if row["revision"] != spec["expected_revision"]:
                raise ValueError("Review the latest camera revision before saving context.")
            if db.execute("SELECT COUNT(*) FROM camera_drafts WHERE camera_id=?",(camera_id,)).fetchone()[0] >= 500:
                raise ValueError("This preview retains up to 500 context drafts per camera.")
            db.execute("INSERT INTO camera_drafts VALUES (?,?,?)",(draft["id"],camera_id,json.dumps(draft)))
            db.execute("UPDATE cameras SET revision=revision+1,draft_id=? WHERE id=?",(draft["id"],camera_id))
        return self.get(camera_id,collection)

    def confirm(self, camera_id, draft_id, *, expected_revision, operator, collection=None):
        self.sync()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db,camera_id,collection)
            if row["revision"] != expected_revision or row["draft_id"] != draft_id:
                raise ValueError("Review the latest camera revision and draft before confirming.")
            saved = db.execute("SELECT body FROM camera_drafts WHERE id=? AND camera_id=?",(draft_id,camera_id)).fetchone()
            if saved is None:
                raise ValueError("Save a context draft for review first.")
            draft = json.loads(saved["body"])
            clip = self.index.get_clip(draft["anchor_clip_id"])
            if not clip or self._source_hash(clip) != draft["provenance"]["source_sha256"]:
                raise ValueError("Review a fresh context draft for the current source file.")
            views = self._views(db,camera_id)
            if views:
                previous = views[-1]["provenance"]["valid_from_seconds"]
                if draft["valid_from_seconds"] < previous or (draft["change_type"] == "camera_moved" and draft["valid_from_seconds"] <= previous):
                    raise ValueError("Place a new camera view after the previous view's start time.")
            revision = len(views)+1
            view = {"camera_id":camera_id,"view_revision":revision,"context":draft["context"],
                    "provenance":{**draft["provenance"],"kind":"operator_confirmed","confirmed_at":timestamp(),
                        "confirmed_by":operator,"draft_id":draft_id,"change_type":draft["change_type"]}}
            db.execute("INSERT INTO camera_views VALUES (?,?,?)",(camera_id,revision,json.dumps(view)))
            db.execute("UPDATE cameras SET revision=revision+1,draft_id=NULL WHERE id=?",(camera_id,))
        return self.get(camera_id,collection)

    def get_context_for_clip(self, record):
        current = self.index.get_clip(record["id"])
        if not current or any(current.get(k) != record.get(k) for k in ("video_id","dataset","path","start","end")):
            return None
        self.sync()
        with self.connect() as db:
            views = self._views(db,self.camera_id(current))
        source_hash = None
        for position in range(len(views)-1,-1,-1):
            view = views[position]
            start = view["provenance"]["valid_from_seconds"]
            end = views[position+1]["provenance"]["valid_from_seconds"] if position+1 < len(views) else None
            if current["start"] < start or (end is not None and current["end"] > end):
                continue
            try:
                source_hash = self._source_hash(current)
            except (OSError,ValueError):
                return None
            if source_hash == view["provenance"]["source_sha256"]:
                return view
        return None


def get_context_for_clip(state, index, record):
    """Host-only resolution for the model adapter; callers cannot supply context text."""
    return CameraStore(state,index).get_context_for_clip(record)
