"""Company-scoped instruction skills with immutable revisions and explicit installation.

The caller supplies registered tools and verified connection scopes at resolution.
This module stores reference instructions; it installs neither code nor capabilities.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

import pack
from workspace_identity import workspace_scope


AUTHORITY = (
    "Skills supply workflow instructions within the current request. The host controls company "
    "and source access, registered tools, connection scopes, compute budgets, scheduling and "
    "external delivery. Those controls retain priority over every skill instruction. "
    "Installation records an instruction revision; execution uses existing host capabilities."
)
_FIELDS = {"name", "description", "instructions", "required_connections", "required_tools"}
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,79}\Z")
_TOOL = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")
_SCOPE = re.compile(r"read:[a-z][a-z0-9_.-]{0,79}\Z")


def __getattr__(name):
    # BUILTINS is the tuple of skill seeds of the template this process serves (pack.py):
    # packs/<id>/skills.json. A new company's catalogue starts with them.
    if name == "BUILTINS":
        return pack.active().skills()
    raise AttributeError(name)


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value,maximum):
    if (not isinstance(value,str) or not value.strip() or len(value)>maximum
            or any(ord(char)<32 and char not in "\n\r\t" for char in value)):
        raise ValueError("Use a bounded, readable skill name, description and instructions.")
    return value


def _names(value,pattern,maximum):
    if (not isinstance(value,(list,tuple)) or len(value)>maximum
            or any(not isinstance(item,str) or not pattern.fullmatch(item) for item in value)
            or len(set(value))!=len(value)):
        raise ValueError("Use distinct supported tool, provider and read-scope identifiers.")
    return list(value)


def _connections(value):
    if not isinstance(value,(list,tuple)) or len(value)>8:
        raise ValueError("Choose up to eight explicit read-only connection requirements.")
    result=[]
    for connection in value:
        if not isinstance(connection,dict) or set(connection)!={"provider","scopes"}:
            raise ValueError("Use provider and read scopes from the host's verified capabilities.")
        provider=_names([connection["provider"]],_NAME,1)[0]
        scopes=_names(connection["scopes"],_SCOPE,16)
        if not scopes:
            raise ValueError("Specify the connection's required read scopes.")
        if any(item["provider"]==provider for item in result):
            raise ValueError("Combine each provider's read scopes into one requirement.")
        result.append({"provider":provider,"scopes":scopes})
    return result


def _definition(body,previous=None):
    if not isinstance(body,dict) or not body or set(body)-_FIELDS:
        raise ValueError("Use skill description fields, instructions and explicit dependencies.")
    merged={key:previous[key] for key in _FIELDS} if previous else {
        "required_connections":[],"required_tools":[]}
    merged.update(body)
    if set(merged)!=_FIELDS:
        raise ValueError("Supply a skill name, description and instructions.")
    return {"name":_text(merged["name"],200),"description":_text(merged["description"],2000),
        "instructions":_text(merged["instructions"],16000),
        "required_connections":_connections(merged["required_connections"]),
        "required_tools":_names(merged["required_tools"],_TOOL,32)}


def _revision(value):
    if type(value) is not int or value<1:
        raise ValueError("Supply the current skill revision.")


def _seeds(template):
    """A template's skill seeds as this store saves them: (id, definition), each through `_definition`."""
    seeds=[]
    for seed in template.skills():
        try:
            seeds.append((seed["id"],_definition({key:seed[key] for key in _FIELDS})))
        except ValueError as error:
            raise pack.PackError(
                f"Correct the skill seed {seed['id']} of the vertical {template.id}: {error}") from None
    return seeds


# The service imports this module while it starts (server.py). Loading the template here reads
# ROCOTO_PACK once, at start, and a template that fails validation stops the start with its reason.
# Its seeds pass this store's own rule in the same step, so every company the service opens can be seeded.
_seeds(pack.active())


class SkillStore:
    def __init__(self,state):
        self.state=Path(state).resolve()
        self.state.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.path=self.state/"skills.sqlite"
        if self.path.is_symlink():
            raise ValueError("Use the company's private skill database.")
        self.company_scope=workspace_scope(self.state)
        # A state directory stays with the template that created it (pack.bind): the seeds below are immutable rows.
        seeds=_seeds(pack.bind(self.state))
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS skill_revisions (
                    skill_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    body TEXT NOT NULL, definition_sha256 TEXT NOT NULL,
                    PRIMARY KEY(skill_id,revision));
                CREATE TABLE IF NOT EXISTS skill_installations (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_id TEXT NOT NULL, revision INTEGER NOT NULL, installed_at TEXT NOT NULL,
                    FOREIGN KEY(skill_id,revision) REFERENCES skill_revisions(skill_id,revision));
                CREATE INDEX IF NOT EXISTS skill_installation_current
                    ON skill_installations(skill_id,sequence DESC);
                CREATE TRIGGER IF NOT EXISTS skill_revisions_no_update
                    BEFORE UPDATE ON skill_revisions BEGIN
                    SELECT RAISE(ABORT,'Skill revisions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS skill_revisions_no_delete
                    BEFORE DELETE ON skill_revisions BEGIN
                    SELECT RAISE(ABORT,'Skill revisions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS skill_installations_no_update
                    BEFORE UPDATE ON skill_installations BEGIN
                    SELECT RAISE(ABORT,'Skill installations are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS skill_installations_no_delete
                    BEFORE DELETE ON skill_installations BEGIN
                    SELECT RAISE(ABORT,'Skill installations are immutable'); END;
            """)
            db.execute("BEGIN IMMEDIATE")
            for skill_id,fields in seeds:
                if db.execute("SELECT 1 FROM skill_revisions WHERE skill_id=? LIMIT 1",(skill_id,)).fetchone():
                    continue
                self._append(db,skill_id,1,fields,"builtin","Rocoto")
        self.path.chmod(0o600)

    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _append(db,skill_id,revision,fields,origin,publisher):
        record={**fields,"id":skill_id,"revision":revision,"publisher":publisher,
            "origin":origin,"created_at":datetime.now(timezone.utc).isoformat()}
        digest=_hash(record)
        db.execute("INSERT INTO skill_revisions VALUES (?,?,?,?)",(skill_id,revision,_json(record),digest))
        return {**record,"definition_sha256":digest}

    @staticmethod
    def _read(row):
        if row is None:
            raise ValueError("Choose a skill from this company's catalogue.")
        try:
            record=json.loads(row["body"])
            if (not isinstance(record,dict) or record["id"]!=row["skill_id"]
                    or record["revision"]!=row["revision"] or _hash(record)!=row["definition_sha256"]):
                raise ValueError()
        except (KeyError,ValueError,TypeError):
            raise ValueError("The stored skill revision failed its integrity check.") from None
        return {**record,"definition_sha256":row["definition_sha256"]}

    def _current(self,db,skill_id):
        if not isinstance(skill_id,str) or not _NAME.fullmatch(skill_id):
            raise ValueError("Choose a skill from this company's catalogue.")
        return self._read(db.execute("SELECT * FROM skill_revisions WHERE skill_id=? ORDER BY revision DESC LIMIT 1",
            (skill_id,)).fetchone())

    @staticmethod
    def _installed(db,skill_id):
        return db.execute("SELECT revision FROM skill_installations WHERE skill_id=? ORDER BY sequence DESC LIMIT 1",
            (skill_id,)).fetchone()

    def _decorate(self,db,record):
        installation=self._installed(db,record["id"])
        installed_revision=installation["revision"] if installation else None
        latest=self._current(db,record["id"])["revision"]
        return {**record,"installed":installation is not None,"installed_revision":installed_revision,
            "latest_revision":latest,"is_installed_revision":installed_revision==record["revision"],
            "update_available":installed_revision is not None and installed_revision<latest,
            "authority":"instructions_only"}

    def catalogue(self):
        with self.connect() as db:
            db.execute("BEGIN")
            rows=db.execute("SELECT r.* FROM skill_revisions r JOIN (SELECT skill_id, MAX(revision) AS revision "
                "FROM skill_revisions GROUP BY skill_id) latest USING(skill_id,revision) ORDER BY r.skill_id").fetchall()
            return {"skills":[self._decorate(db,self._read(row)) for row in rows]}

    def list(self):
        with self.connect() as db:
            db.execute("BEGIN")
            rows=db.execute("SELECT r.* FROM skill_revisions r JOIN skill_installations i "
                "ON r.skill_id=i.skill_id AND r.revision=i.revision JOIN (SELECT skill_id, MAX(sequence) AS sequence "
                "FROM skill_installations GROUP BY skill_id) latest ON i.sequence=latest.sequence ORDER BY r.skill_id").fetchall()
            return {"skills":[self._decorate(db,self._read(row)) for row in rows]}

    def install(self,skill_id,expected_revision=None):
        if expected_revision is not None:
            _revision(expected_revision)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current=self._current(db,skill_id)
            if expected_revision is not None and current["revision"]!=expected_revision:
                raise ValueError("Refresh the skill revision before installation.")
            installed=self._installed(db,skill_id)
            if not installed or installed["revision"]!=current["revision"]:
                db.execute("INSERT INTO skill_installations(skill_id,revision,installed_at) VALUES (?,?,?)",
                    (skill_id,current["revision"],datetime.now(timezone.utc).isoformat()))
            return self._decorate(db,current)

    def create(self,body,origin="agent"):
        if origin not in {"agent","operator"}:
            raise ValueError("Identify an agent or operator skill author.")
        fields=_definition(body)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            record=self._append(db,"skill-"+uuid.uuid4().hex,1,fields,origin,"Company")
            return self._decorate(db,record)

    def update(self,skill_id,expected_revision,body,origin="agent"):
        _revision(expected_revision)
        if origin not in {"agent","operator"}:
            raise ValueError("Identify an agent or operator skill author.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current=self._current(db,skill_id)
            if current["origin"]=="builtin":
                raise ValueError("Create a company skill to adapt a Rocoto template.")
            if current["revision"]!=expected_revision:
                raise ValueError("Refresh the skill revision before saving changes.")
            fields=_definition(body,current)
            record=self._append(db,skill_id,current["revision"]+1,fields,origin,"Company")
            return self._decorate(db,record)

    def history(self,skill_id):
        with self.connect() as db:
            db.execute("BEGIN")
            self._current(db,skill_id)
            return [self._read(row) for row in db.execute("SELECT * FROM skill_revisions WHERE skill_id=? ORDER BY revision",
                (skill_id,)).fetchall()]

    def resolve(self,ids,*,capabilities=(),available_tools=(),max_count=8):
        """Snapshot installed instructions using capabilities supplied by trusted host code.

        Saved business connection drafts are deliberately not consulted. Every selected
        skill must satisfy its exact read scopes and registered tools in this invocation.
        A caller can persist the returned snapshot to keep a run's instructions stable.
        """
        if type(max_count) is not int or not 1<=max_count<=8:
            raise ValueError("Choose a skill selection limit between one and eight.")
        selected=_names(ids,_NAME,max_count)
        if not isinstance(available_tools,(list,tuple,set,frozenset)):
            raise ValueError("Supply registered tool names from the host.")
        tools=set(_names(list(available_tools),_TOOL,256))
        connected={item["provider"]:set(item["scopes"]) for item in _connections(capabilities)}
        records=[]
        with self.connect() as db:
            db.execute("BEGIN")
            for skill_id in selected:
                self._current(db,skill_id)
                installation=self._installed(db,skill_id)
                if installation is None:
                    raise ValueError(f"Install skill {skill_id} before selecting it.")
                record=self._read(db.execute("SELECT * FROM skill_revisions WHERE skill_id=? AND revision=?",
                    (skill_id,installation["revision"])).fetchone())
                missing_tools=set(record["required_tools"])-tools
                if missing_tools:
                    raise ValueError(f"Skill {skill_id} requires registered tools: {', '.join(sorted(missing_tools))}.")
                for connection in record["required_connections"]:
                    if not set(connection["scopes"])<=connected.get(connection["provider"],set()):
                        raise ValueError(f"Skill {skill_id} requires verified {connection['provider']} read access.")
                records.append(record)
        snapshot={"skills":records,"company_scope":self.company_scope,
            "authority":"instructions_only","authority_boundary":AUTHORITY}
        return {**snapshot,"snapshot_sha256":_hash(snapshot)}
