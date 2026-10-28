"""Maintained retail projections over admitted, explicitly bound observations.

This reducer performs no inference, provider call or identity matching. The host
selects current event revisions and authorized subject bindings before calling it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from realtime.contracts import CanonicalEvent, TrackingContinuity

MODULE_REVISION = "retail-core.v1"
EPOCH_MODULE_REVISION = "retail-core.epoch.v1"
ALIASES = {
    "enter_view": "visitor.enter",
    "leave_view": "visitor.exit",
    "touch": "product.touch",
    "pick_up": "product.pick_up",
    "pickup": "product.pick_up",
    "picked_up_item": "product.pick_up",
    "carry": "product.carry",
    "place_in_basket": "product.place_in_basket",
    "put_back": "product.put_back",
    "put_back_item": "product.put_back",
    "put_down": "product.put_down",
    "place_elsewhere": "product.place_elsewhere",
    "drop_item": "product.drop",
    "hand_over": "product.hand_over",
    "examine_item": "product.examine",
    "examine_object": "product.examine",
    "rotate_item": "product.rotate",
    "entered_queue": "queue.enter",
    "left_queue": "queue.exit",
    "reached_checkout": "checkout.reached",
    "left_checkout": "checkout.exit",
    "handed_cash": "payment.cash_handed",
    "presented_card": "payment.card_presented",
    "shelf_empty": "stock.empty",
    "shelf_available": "stock.available",
    "shelf_replenished": "stock.replenished",
    "look_at_display": "display.look",
}
HELD = {"product.pick_up", "product.carry"}
ACTIVE = {"held", "in_basket"}
TRANSITIONS = HELD | {
    "product.place_in_basket",
    "product.put_back",
    "product.put_down",
    "product.drop",
    "product.place_elsewhere",
    "product.hand_over",
}
TIMED = {"product.examine": "examining", "display.look": "viewing_display"}
STOCK = {"stock.empty": "empty", "stock.available": "available", "stock.replenished": "replenished"}
PAYMENT = {"payment.cash_handed", "payment.card_presented"}
KNOWN = (
    TRANSITIONS
    | set(TIMED)
    | set(STOCK)
    | PAYMENT
    | {
        "visitor.enter",
        "visitor.exit",
        "product.touch",
        "product.rotate",
        "queue.enter",
        "queue.exit",
        "checkout.reached",
        "checkout.exit",
    }
)


def _number(value: Decimal | None):
    return None if value is None else str(value)


def _text(value, fallback=""):
    return value[:2000] if isinstance(value, str) else fallback


def _reference(event):
    return {"event_id": event.event_id, "event_revision": event.event_revision}


def _subject(binding):
    subject = binding["subject"]
    return (
        None
        if binding.get("current_status", subject.get("current_status", subject["status"])) == "retracted"
        else subject
    )


def _one(bindings, role):
    choices = [_subject(b) for b in bindings if b["role"] == role]
    choices = [s for s in choices if s is not None]
    # Ambiguous bindings remain unresolved; order cannot choose identity.
    ids = {s["subject_id"] for s in choices}
    return choices[0] if len(ids) == 1 else None


def project_observations(facts: list[dict[str, Any]], at_s=None) -> dict[str, Any]:
    """Return JSON-safe journey, basket, interest and stock views as of source time.

    Source assets and replay loops are independent clocks. Calling code supplies
    one asset/loop and current revisions; this boundary also rejects mixed assets.
    Unknown quantity and price stay unknown. Completed interval duration uses
    source seconds, never ingestion or browser time.
    """
    if len(facts) > 1000:
        raise ValueError("Project at most 1000 current observations per source window")
    at = None if at_s is None else Decimal(str(at_s))
    if at is not None and (not at.is_finite() or at < 0):
        raise ValueError("Use finite nonnegative source seconds")
    latest: dict[str, dict[str, Any]] = {}
    assets = set()
    for fact in facts:
        event = fact["event"]
        if not isinstance(event, CanonicalEvent):
            event = CanonicalEvent.model_validate(event)
        if event.origin_kind != "observation":
            raise ValueError("The observation module accepts admitted camera observations")
        assets.add(fact.get("_clock_asset_id", event.provenance.source_asset_id))
        prior = latest.get(event.event_id)
        if prior and prior["event"].event_revision == event.event_revision and prior != fact:
            raise ValueError("Conflicting instances of the same event revision")
        if prior is None or prior["event"].event_revision < event.event_revision:
            latest[event.event_id] = {**fact, "event": event}
    if len(assets) > 1:
        raise ValueError("Project one source asset clock at a time")
    retracted = [_reference(f["event"]) for f in latest.values() if f["event"].event_state == "retracted"]
    rows = sorted(
        (f for f in latest.values() if f["event"].event_state == "active"),
        key=lambda f: (
            (f.get("_clock_window") or f["event"].provenance.source_window).end_s,
            (f.get("_clock_window") or f["event"].provenance.source_window).start_s,
            f["event"].event_id,
        ),
    )
    rows = [
        f
        for f in rows
        if at is None or (f.get("_clock_window") or f["event"].provenance.source_window).start_s <= at
    ]
    visits: dict[str, dict[str, Any]] = {}
    items: dict[str, dict[str, Any]] = {}
    stock: dict[tuple[str, str | None], dict[str, Any]] = {}
    products: dict[str, dict[str, Any]] = {}
    pending, inactive, scene, unsupported = [], [], [], set()
    last_s = max(
        (
            min((f.get("_clock_window") or f["event"].provenance.source_window).end_s, at)
            if at is not None
            else (f.get("_clock_window") or f["event"].provenance.source_window).end_s
            for f in rows
        ),
        default=Decimal(0),
    )

    def visit(subject):
        if subject is None:
            return None
        key = subject["subject_id"]
        return visits.setdefault(
            key,
            {
                "visit_id": key,
                "observed_from_s": None,
                "observed_through_s": None,
                "entered_at_s": None,
                "exited_at_s": None,
                "state": "observed",
                "chapters": [],
                "intervals": [],
                "payment_observations": [],
                "open_intervals": {},
            },
        )

    def observe(v, start, end):
        if v is None:
            return
        v["observed_from_s"] = start if v["observed_from_s"] is None else min(v["observed_from_s"], start)
        v["observed_through_s"] = (
            end if v["observed_through_s"] is None else max(v["observed_through_s"], end)
        )

    def close_interval(v, name, end, ref):
        interval = v["open_intervals"].pop(name, None)
        if interval is not None:
            # Overlapping or reversed observations do not produce negative time.
            finish = max(end, interval["start_s"])
            v["intervals"].append(
                interval
                | {
                    "end_s": finish,
                    "duration_s": finish - interval["start_s"],
                    "complete": True,
                    "evidence": interval["evidence"] + [ref],
                }
            )

    for fact in rows:
        event, bindings = fact["event"], fact["bindings"]
        clock_window = fact.get("_clock_window") or event.provenance.source_window
        start, end = clock_window.start_s, clock_window.end_s
        ref = _reference(event)
        if bindings is None:
            pending.append(ref | {"reason": "subjects_pending"})
            continue
        if any(_subject(b) is None for b in bindings):
            inactive.append(ref)
        actor, recipient = _one(bindings, "actor"), _one(bindings, "recipient")
        observed_item, zone = _one(bindings, "item"), _one(bindings, "zone")
        product = _one(bindings, "product_confirmed")
        candidate = _one(bindings, "product_candidate") if product is None else None
        v, receiver = visit(actor), visit(recipient)
        through = min(end, at) if at is not None else end
        observe(v, start, through)
        observe(receiver, start, through)
        kind = ALIASES.get(event.event_type, event.event_type)
        label = _text(event.payload.get("product_description") or event.payload.get("object_description"))
        chapter = ref | {
            "type": kind,
            "start_s": _number(start),
            "end_s": _number(end),
            "description": _text(event.payload.get("description")),
            "product_description": label,
            "item_id": observed_item["subject_id"] if observed_item else None,
            "product_id": product["subject_id"] if product else None,
            "candidate_product_id": candidate["subject_id"] if candidate else None,
        }
        if fact.get("_continuity") is not None:
            # The reducer orders by the epoch stream clock while retaining the
            # immutable artifact-local evidence needed to audit each action.
            chapter["continuity"] = fact["_continuity"]
        if v is not None:
            v["chapters"].append(chapter)
        else:
            scene.append(chapter)
        if kind not in KNOWN:
            unsupported.add(event.event_type)
        if kind in STOCK:
            if zone is None:
                pending.append(ref | {"reason": "stock_zone_pending"})
            else:
                key = (zone["subject_id"], product["subject_id"] if product else None)
                current = stock.get(key)
                state = STOCK[kind]
                if current and current["at_s"] == end and current["state"] != state:
                    state = "unknown"
                stock[key] = {
                    "zone_id": key[0],
                    "product_id": key[1],
                    "state": state,
                    "quantity": 0 if state == "empty" else None,
                    "at_s": end,
                    "evidence": (
                        [*current["evidence"], ref] if current and current["at_s"] == end else [ref]
                    ),
                }
        if v is not None:
            if kind == "visitor.enter":
                if v["entered_at_s"] is None:
                    v["entered_at_s"] = start
                v["state"] = "in_view"
            elif kind == "visitor.exit":
                v["exited_at_s"], v["state"] = end, "out_of_view"
                # Leaving a camera is not evidence that a queue interaction ended.
            if kind in TIMED:
                v["intervals"].append(
                    {
                        "type": TIMED[kind],
                        "item_id": chapter["item_id"],
                        "start_s": start,
                        "end_s": end if through == end else None,
                        "observed_through_s": through,
                        "duration_s": through - start,
                        "complete": through == end,
                        "evidence": [ref],
                    }
                )
            if kind == "queue.enter":
                v["open_intervals"].setdefault(
                    "queue",
                    {
                        "type": "queue",
                        "start_s": start,
                        "zone_id": zone["subject_id"] if zone else None,
                        "evidence": [ref],
                    },
                )
            if kind in {"queue.exit", "checkout.reached"}:
                close_interval(v, "queue", start, ref)
            if kind == "checkout.reached":
                v["open_intervals"].setdefault(
                    "checkout",
                    {
                        "type": "checkout",
                        "start_s": start,
                        "zone_id": zone["subject_id"] if zone else None,
                        "evidence": [ref],
                    },
                )
            if kind == "checkout.exit":
                close_interval(v, "checkout", start, ref)
            if kind in PAYMENT:
                v["payment_observations"].append(chapter)
        if product and kind.startswith("product."):
            metric = products.setdefault(
                product["subject_id"],
                {
                    "product_id": product["subject_id"],
                    "interaction_events": [],
                    "pickup_events": [],
                    "putback_events": [],
                    "visit_ids": set(),
                },
            )
            metric["interaction_events"].append(ref)
            if actor:
                metric["visit_ids"].add(actor["subject_id"])
        else:
            metric = None
        if kind not in TRANSITIONS or (at is not None and end > at):
            continue
        if actor is None or observed_item is None:
            pending.append(ref | {"reason": "item_or_visit_pending"})
            # An unresolved return cannot identify which prior held item left.
            if actor and kind in {"product.put_back", "product.put_down", "product.hand_over"}:
                for held_item in items.values():
                    if held_item["owner_visit_id"] == actor["subject_id"] and held_item["state"] in ACTIVE:
                        held_item.update(state="uncertain", reason="item_association_pending")
            continue
        attrs = observed_item["attributes"]
        quantity = attrs.get("quantity")
        if type(quantity) is not int or not 1 <= quantity <= 1000:
            quantity = None
        key = observed_item["subject_id"]
        item = items.get(key)
        previous_state = item["state"] if item else None
        previous_owner = item["owner_visit_id"] if item else None
        if item is None:
            item = items[key] = {
                "item_id": key,
                "owner_visit_id": actor["subject_id"],
                "state": "uncertain",
                "quantity": quantity,
                "quantity_basis": attrs.get("quantity_basis"),
                "identity_basis": attrs.get("identity_basis"),
                "product_id": None,
                "candidate_product_id": None,
                "description": label,
                "at_s": end,
                "evidence": [],
                "reason": None,
            }
        conflict = item["quantity"] is not None and quantity is not None and item["quantity"] != quantity
        if item["evidence"] and item["at_s"] == end and previous_owner != actor["subject_id"]:
            conflict = True
        if previous_owner != actor["subject_id"] and previous_state in ACTIVE:
            conflict = True
        if item["evidence"] and item["at_s"] == end:
            earlier_held = item.get("last_action") in HELD | {"product.place_in_basket"}
            now_held = kind in HELD | {"product.place_in_basket"}
            if earlier_held != now_held:
                conflict = True
        item["evidence"].append(ref)
        item.update(at_s=end, last_action=kind, description=label or item["description"])
        if product:
            if item["product_id"] not in {None, product["subject_id"]}:
                conflict = True
            else:
                item["product_id"] = product["subject_id"]
        if candidate:
            item["candidate_product_id"] = candidate["subject_id"]
        if item["quantity"] is None and quantity is not None:
            item.update(quantity=quantity, quantity_basis=attrs.get("quantity_basis"))
        if conflict:
            item.update(state="uncertain", reason="conflicting_item_evidence")
            continue
        item.update(owner_visit_id=actor["subject_id"], reason=None)
        if kind in HELD:
            if not (kind == "product.carry" and previous_state == "in_basket"):
                item["state"] = "held"
            if (
                kind == "product.pick_up"
                and metric
                and not (previous_state in ACTIVE and previous_owner == actor["subject_id"])
            ):
                metric["pickup_events"].append(ref)
        elif kind == "product.place_in_basket":
            item["state"] = "in_basket"
        elif kind == "product.put_back":
            item["state"] = "returned"
            if metric and previous_state in ACTIVE:
                metric["putback_events"].append(ref)
        elif kind == "product.hand_over":
            if recipient:
                item.update(state="held", owner_visit_id=recipient["subject_id"])
            else:
                item.update(state="uncertain", reason="handover_recipient_pending")
        else:
            item.update(state="uncertain", reason="item_location_pending")

    for v in visits.values():
        v["chapters"].sort(key=lambda c: (Decimal(c["start_s"]), Decimal(c["end_s"]), c["event_id"]))
        through = v["observed_through_s"] or last_s
        for interval in v.pop("open_intervals").values():
            v["intervals"].append(
                interval
                | {
                    "end_s": None,
                    "observed_through_s": through,
                    "duration_s": max(Decimal(0), through - interval["start_s"]),
                    "complete": False,
                }
            )
        # Merge only explicitly associated overlapping timed observations. Separate
        # unknown objects or disjoint windows retain separate evidence intervals.
        merged: list[dict[str, Any]] = []
        for interval in sorted(v["intervals"], key=lambda i: (i["start_s"], i["type"])):
            target = next(
                (
                    prior
                    for prior in reversed(merged)
                    if interval.get("item_id") is not None
                    and prior["type"] == interval["type"]
                    and prior.get("item_id") == interval.get("item_id")
                    and (prior["end_s"] if prior["end_s"] is not None else prior["observed_through_s"])
                    >= interval["start_s"]
                ),
                None,
            )
            if target is None:
                merged.append(interval)
                continue
            through = max(
                target.get("observed_through_s", target["end_s"]),
                interval.get("observed_through_s", interval["end_s"]),
            )
            target["complete"] = target["complete"] and interval["complete"]
            target.update(
                end_s=through if target["complete"] else None,
                observed_through_s=through,
                duration_s=through - target["start_s"],
            )
            target["evidence"].extend(r for r in interval["evidence"] if r not in target["evidence"])
        v["intervals"] = merged
        owned = [i for i in items.values() if i["owner_visit_id"] == v["visit_id"]]
        v["basket"] = {
            "items": [i for i in owned if i["state"] in ACTIVE],
            "uncertain_items": [i for i in owned if i["state"] == "uncertain"],
            "known_units": sum(
                i["quantity"] or 0
                for i in owned
                if i["state"] in ACTIVE
                and i["quantity_basis"] != "estimated"
                and i["identity_basis"] in {"object_tracker", "human_review"}
            ),
            "estimated_units": sum(
                i["quantity"] or 0
                for i in owned
                if i["state"] in ACTIVE
                and (
                    i["quantity_basis"] == "estimated"
                    or i["identity_basis"] not in {"object_tracker", "human_review"}
                )
            ),
            "unquantified_items": sum(i["quantity"] is None for i in owned if i["state"] in ACTIVE),
            "subtotal": None,
            "valuation_status": "awaiting_prices",
            "paid_amount": None,
        }
        v["observed_duration_s"] = max(Decimal(0), through - (v["observed_from_s"] or Decimal(0)))
    for metric in products.values():
        metric["visit_ids"] = sorted(metric["visit_ids"])
        metric["interactions"] = len(metric["interaction_events"])
        metric["pickups"] = len(metric["pickup_events"])
        metric["putbacks"] = len(metric["putback_events"])

    def wire(value):
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {k: wire(v) for k, v in value.items()}
        if isinstance(value, list):
            return [wire(v) for v in value]
        return value

    return wire(
        {
            "schema": "rocoto.retail-core.v1",
            "module_revision": MODULE_REVISION,
            "as_of_source_s": at if at is not None else last_s,
            "visits": sorted(visits.values(), key=lambda v: (v["observed_from_s"], v["visit_id"])),
            "products": sorted(products.values(), key=lambda p: p["product_id"]),
            "stock": sorted(stock.values(), key=lambda s: (s["zone_id"], s["product_id"] or "")),
            "scene_events": scene,
            "coverage": {
                "pending": pending,
                "retracted_events": retracted,
                "retracted_subject_events": inactive,
                "unsupported_event_types": sorted(unsupported),
                "sales_basis": "pos_required",
            },
        }
    )


def project_continuous_observations(
    facts: list[dict[str, Any]], at_s=None, *, epoch_id: str | None = None
) -> dict[str, Any]:
    """Project one tracker epoch across rotated artifacts on its stream clock.

    Each admitted event must carry the typed ``TrackingContinuity`` mapping
    produced by the host. Local artifact windows remain in the evidence
    attached to chapters; reducer ordering and playheads use only the mapped
    ``stream_window``. Subject bindings therefore keep one visit and basket
    identity across file boundaries without comparing unrelated local clocks.
    """
    if not facts:
        if epoch_id is None:
            raise ValueError("An epoch-qualified projection requires an epoch ID")
        result = project_observations([], at_s=at_s)
        result["module_revision"] = EPOCH_MODULE_REVISION
        result["continuity"] = {"epoch_id": epoch_id, "clock": "stream", "source_assets": []}
        return result

    normalized: list[dict[str, Any]] = []
    assets: dict[str, set[str]] = {}
    selected_epoch = epoch_id
    for fact in facts:
        event = fact["event"]
        if not isinstance(event, CanonicalEvent):
            event = CanonicalEvent.model_validate(event)
        if event.origin_kind != "observation" or event.provenance.continuity is None:
            raise ValueError("Epoch projections require typed observation continuity")
        continuity = TrackingContinuity.model_validate(event.provenance.continuity)
        if selected_epoch is None:
            selected_epoch = continuity.epoch_id
        if continuity.epoch_id != selected_epoch:
            raise ValueError("An epoch projection cannot mix tracker epochs")
        source_asset = event.provenance.source_asset_id
        assets.setdefault(source_asset, set()).add(continuity.mapping_id)
        evidence = {
            "source_asset_id": source_asset,
            "epoch_id": continuity.epoch_id,
            "mapping_id": continuity.mapping_id,
            "source_window": event.provenance.source_window.model_dump(mode="json"),
            "stream_window": continuity.stream_window.model_dump(mode="json"),
        }
        normalized.append(
            {
                **fact,
                "event": event,
                "_clock_asset_id": f"epoch:{selected_epoch}",
                "_clock_window": continuity.stream_window,
                "_continuity": evidence,
            }
        )
    result = project_observations(normalized, at_s=at_s)
    result["module_revision"] = EPOCH_MODULE_REVISION
    result["continuity"] = {
        "epoch_id": selected_epoch,
        "clock": "stream",
        "source_assets": [
            {
                "source_asset_id": source_asset,
                "epoch_id": selected_epoch,
                "mapping_ids": sorted(assets[source_asset]),
            }
            for source_asset in sorted(assets)
        ],
    }
    return result
