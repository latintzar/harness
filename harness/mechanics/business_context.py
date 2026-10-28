"""Workspace business profiles, authorship and immutable revision history.

The selected vertical owns any additional domain fields and setup operations.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from typing_extensions import Annotated
from workspace_identity import onboarding_identity, workspace_scope


Text = Annotated[str, Field(min_length=1, max_length=500)]
_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s<>\"']+", re.IGNORECASE)
_SECRET = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+\S+|"
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|hf_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b|"
    r"\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|"
    r"password|passwd|authorization|credential|secret|token)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)


def public_url(value):
    """Accept public reference URLs, never credential or private service URLs.

    No DNS or network requests are made here. Query strings/fragments are omitted
    from this data model so signed/authentication links cannot become references.
    """
    if not isinstance(value,str) or len(value)>2000 or any(c.isspace() for c in value) or "\\" in value:
        raise ValueError("Use a public HTTP or HTTPS reference URL.")
    try:
        parsed=urlsplit(value)
        host=parsed.hostname
        port=parsed.port
        if (parsed.scheme not in {"https","http"} or not host or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or "?" in value or "#" in value
                or "%" in parsed.netloc or port not in {None,80,443}):
            raise ValueError("Use a public HTTP or HTTPS reference URL.")
        host=host.rstrip(".").encode("idna").decode("ascii").lower()
        try:
            address=ipaddress.ip_address(host)
        except ValueError:
            if ("." not in host or host.endswith((".local",".localhost",".internal",".lan",".home",".test",".invalid"))
                    or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}",host)):
                raise ValueError("Use a public website address.")
        else:
            if not address.is_global:
                raise ValueError("Use a public website address.")
        if _SECRET.search(unquote(parsed.path)):
            raise ValueError("Keep credentials in protected connection setup.")
    except (ValueError, UnicodeError):
        raise ValueError("Use a public reference URL with credentials kept in protected setup.") from None
    return value


def _public_text(value):
    if any(ord(c)<32 and c not in "\n\r\t" for c in value) or _SECRET.search(value):
        raise ValueError("Keep credentials in protected connection setup.")
    for match in _URL.finditer(value):
        public_url(match.group(0).rstrip(".,);"))
    return value


class BusinessProfile(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    name: str=Field(default="",max_length=200)
    type: str=Field(default="",max_length=200)
    website: str=Field(default="",max_length=2000)
    location: str=Field(default="",max_length=500)
    description: str=Field(default="",max_length=6000)
    operations: list[Text]=Field(default_factory=list,max_length=32)
    source_urls: list[Annotated[str,Field(min_length=1,max_length=2000)]]=Field(default_factory=list,max_length=24)
    assumptions: list[Text]=Field(default_factory=list,max_length=32)

    @model_validator(mode="after")
    def reference_data(self):
        for value in [self.name,self.type,self.location,self.description,*self.operations,*self.assumptions]:
            _public_text(value)
        if any(not value.strip() for value in [*self.operations,*self.assumptions]):
            raise ValueError("Use explicit operations and assumptions.")
        for value in [*self.source_urls,*([self.website] if self.website else [])]:
            public_url(value)
        self.source_urls=list(dict.fromkeys(self.source_urls))
        return self


def _validate(model,body):
    try:
        return model.model_validate(body).model_dump()
    except ValidationError:
        # Pydantic's default text includes submitted values, including rejected secrets.
        raise ValueError("Use the supported public business fields and source references.") from None


def _now():
    return datetime.now(timezone.utc).isoformat()


class BusinessStore:
    def __init__(self,state,*,collections=None):
        self.state=Path(state)
        self.collections = collections
        self.state.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.company_scope=workspace_scope(self.state)
        self.path=self.state/"business-context.sqlite"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS business_revisions (
                    collection TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(collection,revision));
            """)
        self.path.chmod(0o600)

    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        return db

    def _collection_exists(self, collection):
        return self.collections is not None and self.collections.contains(collection)

    def _scope(self,db,collection):
        if not isinstance(collection,str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}",collection):
            raise ValueError("Choose a business collection from this workspace.")
        if (collection!="company" and not self._collection_exists(collection)
                and db.execute("SELECT 1 FROM business_revisions WHERE collection=? LIMIT 1",(collection,)).fetchone() is None):
            raise ValueError("Choose a business collection from this workspace.")

    def _initial(self,collection):
        identity=onboarding_identity(self.state,collection)
        return {"collection":collection,"revision":0,"profile_revision":0,
            "profile":BusinessProfile().model_dump(),
            "provenance":{"origin":"unprovided","revision":0,"recorded_at":None,
                "epistemic_status":"awaiting_business_context","source_urls":[]},
            "onboarding_chat_id":"onboarding-"+identity,
            "created_at":None,"updated_at":None,"last_change":None}

    def _current(self,db,collection):
        row=db.execute("SELECT body FROM business_revisions WHERE collection=? ORDER BY revision DESC LIMIT 1",(collection,)).fetchone()
        return json.loads(row["body"]) if row else self._initial(collection)

    def _present(self, record):
        # Earlier shared hosts saved domain extensions in the same immutable rows.
        # Retain their bytes while the common public profile exposes its own fields.
        return {key: record[key] for key in self._initial(record["collection"]) if key in record}

    def get(self,collection):
        with self.connect() as db:
            self._scope(db,collection)
            return self._present(self._current(db,collection))

    def history(self,collection,limit=100):
        if type(limit) is not int or not 1<=limit<=200:
            raise ValueError("Choose one to 200 business revisions.")
        with self.connect() as db:
            self._scope(db,collection)
            rows=db.execute("SELECT body FROM business_revisions WHERE collection=? ORDER BY revision DESC LIMIT ?",(collection,limit)).fetchall()
        return [self._present(json.loads(row["body"])) for row in reversed(rows)]

    def _mutate(self,collection,expected_revision,kind,origin,change):
        if type(expected_revision) is not int or expected_revision<0:
            raise ValueError("Supply the current business revision.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._scope(db,collection)
            current=self._current(db,collection)
            if current["revision"]!=expected_revision:
                raise ValueError("Refresh the business revision before saving changes.")
            updated=copy.deepcopy(current)
            revision,timestamp=expected_revision+1,_now()
            change(updated,revision,timestamp)
            updated.update(revision=revision,created_at=current["created_at"] or timestamp,
                updated_at=timestamp,last_change={"kind":kind,"origin":origin,"recorded_at":timestamp})
            db.execute("INSERT INTO business_revisions VALUES (?,?,?)",(collection,revision,json.dumps(updated,allow_nan=False)))
        return self._present(updated)

    def update(self,collection,expected_revision,profile,origin="operator"):
        if origin not in {"operator","agent_research"}:
            raise ValueError("Identify an operator note or agent research record.")
        profile=_validate(BusinessProfile,profile)
        if origin=="agent_research" and not profile["source_urls"]:
            raise ValueError("Include public source references for agent research.")
        def change(current,revision,timestamp):
            current.update(profile=profile,profile_revision=revision,
                provenance={"origin":origin,"revision":revision,"recorded_at":timestamp,
                    "epistemic_status":"operator_provided" if origin=="operator" else "agent_researched_unconfirmed",
                    "source_urls":profile["source_urls"]})
        return self._mutate(collection,expected_revision,"profile",origin,change)
