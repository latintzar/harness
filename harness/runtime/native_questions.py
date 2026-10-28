"""Durable ordinary Codex questions, scoped to a host-admitted worker generation.

The bridge owns native thread admission and the live JSON-RPC connection. This
store owns opaque public IDs, answer validation and at-most-once delivery. Secret
questions use the separate private credential workflow; their fields stay out of
this store. A written response with uncertain delivery is never replayed.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from native_events import PublicActivity

QUESTION_TIMEOUT_SECONDS = 30

CLARIFICATION_SPEC = {
    "type":"function", "name":"request_clarification",
    "description":"Ask optional questions in the question panel and wait up to 30 seconds. Single choice sends immediately; multiple choice uses Send. Returns submitted answers, skipped, or timed_out. Ask mandatory questions in your normal response and end the turn. Use the private connection credential tool for secrets. Answers never authorize external actions.",
    "inputSchema":{"type":"object", "additionalProperties":False, "required":["questions"],
        "properties":{"questions":{"type":"array", "minItems":1, "maxItems":3, "items":{
            "type":"object", "additionalProperties":False,
            "required":["id","header","question","multiple","allow_free_text","options"],
            "properties":{
                "id":{"type":"string","minLength":1,"maxLength":100},
                "header":{"type":"string","minLength":1,"maxLength":100},
                "question":{"type":"string","minLength":1,"maxLength":4000},
                "multiple":{"type":"boolean"}, "allow_free_text":{"type":"boolean"},
                "options":{"type":"array","maxItems":10,"items":{"type":"object","additionalProperties":False,
                    "required":["label","description"],"properties":{
                        "label":{"type":"string","minLength":1,"maxLength":200},
                        "description":{"type":"string","maxLength":1000}}}}}}}}}
}


def _expires(row):
    # created_at is durable; reconnects and duplicate native events retain the
    # original host deadline rather than starting another browser countdown.
    return datetime.fromisoformat(row["created_at"]) + timedelta(seconds=QUESTION_TIMEOUT_SECONDS)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _identity(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or "\x00" in value:
        raise ValueError("Use the current chat request.")
    return value


def _native_key(value):
    if (type(value) not in (int, str) or isinstance(value, str) and not 1 <= len(value) <= 256
            or isinstance(value, int) and not -(2**63) <= value < 2**63):
        raise ValueError("Use a valid native request identity.")
    return json.dumps(value)


class NativeQuestions:
    def __init__(self, state):
        directory = Path(state) / "native-questions"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.database = directory / "questions.sqlite"
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY, root_chat_id TEXT NOT NULL, actor_chat_id TEXT NOT NULL,
                actor_title TEXT NOT NULL, owner TEXT NOT NULL, generation TEXT NOT NULL,
                native_key TEXT NOT NULL, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                item_id TEXT NOT NULL, fingerprint TEXT NOT NULL, blocking INTEGER NOT NULL,
                questions TEXT NOT NULL, status TEXT NOT NULL, resolution_reason TEXT,
                response TEXT, created_at TEXT NOT NULL, resolved_at TEXT,
                UNIQUE(root_chat_id,generation,native_key))""")
        self.database.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _public(row):
        result = {key: row[key] for key in ("id", "root_chat_id", "actor_chat_id", "actor_title",
            "status", "created_at", "resolved_at", "resolution_reason")}
        result.update(kind="question", blocking=bool(row["blocking"]), questions=json.loads(row["questions"]),
                      expires_at=_expires(row).isoformat())
        return result

    def open(self, *, root_chat_id, actor_chat_id, owner, generation, native_request_id, params,
             actor_title="Agent", transport="native"):
        if transport not in {"native", "host"}:
            raise ValueError("Choose a host question transport.")
        for value in (root_chat_id, actor_chat_id, owner, generation):
            _identity(value)
        native_key = _native_key(native_request_id)
        # Native JSON-RPC IDs are validated scalars. The host's tagged array is
        # disjoint from every native ID and keeps the existing durable schema.
        if transport == "host":
            native_key = json.dumps(["host", native_request_id])
        if (not isinstance(params, dict) or len(json.dumps(params)) > 65536
                or not isinstance(actor_title, str) or len(actor_title) > 256):
            raise ValueError("Use a bounded native question.")
        for field in ("threadId", "turnId", "itemId"):
            _identity(params.get(field))
        projected = PublicActivity().translate("item/tool/requestUserInput", params,
            request_id=native_request_id)["request"]
        if projected["status"] == "pending":
            for question, source in zip(projected["questions"], params["questions"]):
                if type(source.get("isMultiSelect", False)) is not bool:
                    projected.update(status="unsupported", resolution_reason="invalid_questions", questions=[])
                    break
                question["multiple"] = source.get("isMultiSelect") is True
        # Hash normalized public question data. Unsupported secret fields are
        # deliberately absent, even from stored fingerprints and error reports.
        fingerprint = hashlib.sha256(json.dumps({"actor":actor_chat_id, "title":actor_title,
            "thread":params["threadId"], "turn":params["turnId"], "item":params["itemId"],
            "blocking":projected["blocking"], "questions":projected["questions"],
            "status":projected["status"], "reason":projected.get("resolution_reason")}, sort_keys=True).encode()).hexdigest()
        with self._db() as db:
            existing = db.execute("SELECT * FROM questions WHERE root_chat_id=? AND generation=? AND native_key=?",
                (root_chat_id, generation, native_key)).fetchone()
            if existing:
                if existing["owner"] != owner or existing["fingerprint"] != fingerprint:
                    raise ValueError("This native request has a different chat binding.")
                return self._public(existing)
            identity = "question-" + uuid.uuid4().hex
            now = _now()
            db.execute("INSERT INTO questions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity, root_chat_id, actor_chat_id, actor_title, owner, generation, native_key,
                 params["threadId"], params["turnId"], params["itemId"], fingerprint, int(projected["blocking"]),
                 json.dumps(projected["questions"]), projected["status"], projected.get("resolution_reason"),
                 None, now, now if projected["status"] == "unsupported" else None))
            return self._public(db.execute("SELECT * FROM questions WHERE id=?", (identity,)).fetchone())

    def open_host(self, arguments, *, root_chat_id, owner, generation):
        from jsonschema import Draft202012Validator
        if next(Draft202012Validator(CLARIFICATION_SPEC["inputSchema"]).iter_errors(arguments), None):
            raise ValueError("Provide one to three bounded optional questions.")
        questions = arguments["questions"]
        if (len({q["id"] for q in questions}) != len(questions) or any(
                len({o["label"] for o in q["options"]}) != len(q["options"])
                or any(not value.strip() or "\x00" in value for value in
                    [q["id"], q["header"], q["question"], *[o["label"] for o in q["options"]]])
                for q in questions)):
            raise ValueError("Use unique question IDs and clear answer options.")
        identity = "host-" + uuid.uuid4().hex
        record = self.open(root_chat_id=root_chat_id, actor_chat_id=root_chat_id, owner=owner,
            generation=generation, native_request_id=identity, transport="host", params={
                "threadId":root_chat_id,"turnId":generation,"itemId":identity,"isBlocking":True,
                "questions":[{"id":q["id"],"header":q["header"],"question":q["question"],
                    "isMultiSelect":q["multiple"],"isOther":q["allow_free_text"],"options":q["options"]}
                    for q in arguments["questions"]]})
        if record["status"] != "pending":
            raise ValueError("Use unique question IDs and clear answer options.")
        return record

    def host_request(self, request_id, *, owner, root_chat_id):
        with self._db() as db:
            return isinstance(json.loads(self._owned(db, request_id, owner, root_chat_id)["native_key"]), list)

    @staticmethod
    def _owned(db, request_id, owner, root_chat_id, generation=None):
        row = db.execute("SELECT * FROM questions WHERE id=? AND owner=? AND root_chat_id=?",
            (_identity(request_id), _identity(owner), _identity(root_chat_id))).fetchone()
        if row is None or generation is not None and row["generation"] != _identity(generation):
            raise ValueError("Use a request from this active parent chat.")
        return row

    def get(self, request_id, *, owner, root_chat_id):
        with self._db() as db:
            return self._public(self._owned(db, request_id, owner, root_chat_id))

    def for_chat(self, root_chat_id, *, owner):
        with self._db() as db:
            return [self._public(row) for row in db.execute(
                "SELECT * FROM questions WHERE root_chat_id=? AND owner=? ORDER BY created_at,id",
                (_identity(root_chat_id), _identity(owner)))]

    def interrupt(self, request_id, *, owner, root_chat_id, reason="runtime_ended"):
        """Reconcile a saved request after the host confirms its run has ended."""
        with self._db() as db:
            row = self._owned(db, request_id, owner, root_chat_id)
            if row["status"] in {"pending", "delivering"}:
                db.execute("UPDATE questions SET status='interrupted',resolution_reason=?,resolved_at=? WHERE id=?",
                    (_identity(reason), _now(), request_id))
            return self._public(db.execute("SELECT * FROM questions WHERE id=?", (request_id,)).fetchone())

    @staticmethod
    def _response(row, action, answers):
        if action in {"skip", "timeout"}:
            if answers not in (None, {}):
                raise ValueError("Skip this question or submit its answers.")
            return {"answers":{}}
        questions = json.loads(row["questions"])
        if action != "submit" or not isinstance(answers, dict) or set(answers) != {q["id"] for q in questions}:
            raise ValueError("Answer each question before submitting.")
        selected = {}
        for question in questions:
            values = answers[question["id"]]
            if (not isinstance(values, list) or not 1 <= len(values) <= 10
                    or not question.get("multiple", False) and len(values) != 1
                    or any(not isinstance(value, str) or not value.strip() or len(value) > 4000
                        or "\x00" in value for value in values)
                    or len(set(values)) != len(values)):
                raise ValueError("Use a short answer for each question.")
            labels = {option["label"] for option in question["options"]}
            if labels and not question["is_other"] and any(value not in labels for value in values):
                raise ValueError("Choose one of the provided answers.")
            selected[question["id"]] = {"answers":values}
        response = {"answers":selected}
        if len(json.dumps(response).encode()) > 32768:
            raise ValueError("Keep the answers within the request size limit.")
        return response

    async def answer(self, request_id, *, owner, root_chat_id, generation, action, answers=None,
                     deliver, cancelled):
        if not isinstance(cancelled, asyncio.Event) or not callable(deliver):
            raise ValueError("An active native connection is required.")
        with self._db() as db:
            row = self._owned(db, request_id, owner, root_chat_id, generation)
            if row["status"] not in {"pending", "delivering", "submitted", "skipped"}:
                return self._public(row)
            if action == "timeout" and row["status"] != "pending":
                return self._public(row)
            response = self._response(row, action, answers)
            encoded = json.dumps(response, sort_keys=True)
            if row["status"] in {"delivering", "submitted", "skipped"}:
                if row["response"] != encoded:
                    raise ValueError("This question already has a submitted response.")
                return self._public(row)
            if cancelled.is_set():
                db.execute("UPDATE questions SET status='interrupted',resolution_reason='stopped',resolved_at=? WHERE id=?",
                    (_now(), request_id))
                return self._public(db.execute("SELECT * FROM questions WHERE id=?", (request_id,)).fetchone())
            if _expires(row) <= datetime.fromisoformat(_now()):
                action, response = "timeout", {"answers":{}}
                encoded = json.dumps(response, sort_keys=True)
            elif action == "timeout":
                raise ValueError("This question is still awaiting your answer.")
            # Commit before transport I/O. Other processes see delivering and
            # cannot duplicate the response, including after a service restart.
            db.execute("UPDATE questions SET status='delivering',response=?,resolution_reason=? WHERE id=?",
                       (encoded, "timed_out" if action == "timeout" else None, request_id))
            native_id = json.loads(row["native_key"])
        try:
            # No suspension occurs between the cancellation check, durable claim
            # and entry into the live connection's write operation.
            await deliver(native_id, response)
        except asyncio.CancelledError:
            self._finish_delivery(request_id, "delivery_uncertain", "delivery_cancelled")
            raise
        except (OSError, RuntimeError, TimeoutError):
            return self._finish_delivery(request_id, "delivery_uncertain", "connection_ended")
        return self._finish_delivery(request_id,
            {"submit":"submitted", "skip":"skipped", "timeout":"timed_out"}[action],
            "timed_out" if action == "timeout" else None)

    async def expire(self, *, owner, root_chat_id, generation, deliver, cancelled):
        """Release expired native waits even when every browser is disconnected."""
        with self._db() as db:
            now = datetime.fromisoformat(_now())
            due = [row["id"] for row in db.execute(
                "SELECT * FROM questions WHERE owner=? AND root_chat_id=? AND generation=? AND status='pending'",
                (_identity(owner), _identity(root_chat_id), _identity(generation)))
                if not isinstance(json.loads(row["native_key"]), list) and _expires(row) <= now]
        results = []
        for request_id in due:
            results.append(await self.answer(request_id, owner=owner, root_chat_id=root_chat_id,
                generation=generation, action="timeout", deliver=deliver, cancelled=cancelled))
        return results

    def _finish_delivery(self, request_id, status, reason=None):
        with self._db() as db:
            db.execute("UPDATE questions SET status=?,resolution_reason=?,resolved_at=? WHERE id=? AND status='delivering'",
                (status, reason, _now(), request_id))
            return self._public(db.execute("SELECT * FROM questions WHERE id=?", (request_id,)).fetchone())

    def resolve_native(self, root_chat_id, generation, native_request_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM questions WHERE root_chat_id=? AND generation=? AND native_key=?",
                (_identity(root_chat_id), _identity(generation), _native_key(native_request_id))).fetchone()
            if row is None:
                return None
            # Resolution is ambiguous; preserve the host's submitted/skip outcome
            # and the delivering claim while its transport call is still running.
            if row["status"] == "pending":
                db.execute("UPDATE questions SET status='resolved',resolution_reason='native_resolved',resolved_at=? WHERE id=?",
                    (_now(), row["id"]))
            return self._public(db.execute("SELECT * FROM questions WHERE id=?", (row["id"],)).fetchone())

    def retire_generation(self, root_chat_id, generation, reason="interrupted"):
        with self._db() as db:
            rows = list(db.execute("SELECT id FROM questions WHERE root_chat_id=? AND generation=? AND status IN ('pending','delivering')",
                (_identity(root_chat_id), _identity(generation))))
            for row in rows:
                db.execute("UPDATE questions SET status='interrupted',resolution_reason=?,resolved_at=? WHERE id=?",
                    (_identity(reason), _now(), row["id"]))
            return [self._public(db.execute("SELECT * FROM questions WHERE id=?", (row["id"],)).fetchone()) for row in rows]

    def recover(self):
        """Call once on bridge startup after its workers have been reconciled."""
        with self._db() as db:
            rows = list(db.execute("SELECT id FROM questions WHERE status IN ('pending','delivering')"))
            for row in rows:
                db.execute("UPDATE questions SET status='interrupted',resolution_reason='runtime_restarted',resolved_at=? WHERE id=?",
                    (_now(), row["id"]))
            return [self._public(db.execute("SELECT * FROM questions WHERE id=?", (row["id"],)).fetchone()) for row in rows]
