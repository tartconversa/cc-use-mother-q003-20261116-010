from __future__ import annotations

from decimal import Decimal
import json

from .dimensions import Dimensions
from .landing import Landed, Landing
from .model import LakeError, RawEvent, decimal_text, now
from .store import Store


class Ledger:
    def __init__(self, store: Store, dimensions: Dimensions, landing: Landing):
        self.store, self.dimensions, self.landing = store, dimensions, landing

    def ingest(self, data: dict):
        landed = self.landing.accept(data)
        if not landed.created and landed.state == "normalized":
            return {"ingestion_id": landed.ingestion_id, "fact_id": landed.normalized_id,
                    "state": "normalized", "created": False}
        if not landed.created and landed.state == "quarantined":
            stored = self.store.ingestion(landed.ingestion_id)
            return {"ingestion_id": landed.ingestion_id, "state": "quarantined",
                    "created": False, "issue": json.loads(stored["issue"])}
        return self.normalize(landed.ingestion_id, landed.event, landed.created)

    def normalize(self, ingestion_id: int, event: RawEvent | None = None, created: bool = False):
        if event is None:
            retried = self.landing.retry(ingestion_id)
            if isinstance(retried, dict):
                return {**retried, "created": False}
            event = retried
        try:
            return self._normalize(ingestion_id, event, created)
        except LakeError as error:
            self.landing.quarantine(ingestion_id, error)
            return {"ingestion_id": ingestion_id, "state": "quarantined", "created": created,
                    "issue": {"code": error.code, "detail": error.detail}}

    def _normalize(self, ingestion_id: int, event: RawEvent, created: bool):
        with self.store.write() as conn:
            row = self.store.one(conn, "SELECT * FROM ingestions WHERE id=?", (ingestion_id,))
            if row is None:
                raise LakeError("unknown_ingestion", "ingestion not found", 404)
            if row["state"] == "normalized":
                return {"ingestion_id": ingestion_id, "fact_id": row["normalized_id"],
                        "state": "normalized", "created": False}
            dims = self.dimensions.resolve_event(conn, event)
            if event.event_type in {"deposit", "revocation"} and "community" not in dims:
                raise LakeError("missing_location", "deposit device must map to a community")
            if event.event_type in {"payout", "payout_adjustment"} and "actor" not in dims:
                raise LakeError("missing_actor", "payout actor is unknown")
            if event.event_type == "weigh_out" and "station" not in dims:
                raise LakeError("missing_station", "outbound weighing needs station")
            if event.event_type == "transfer" and "station" not in dims:
                raise LakeError("missing_station", "transfer needs station")
            target = None
            if event.event_type == "revocation":
                target = self._revocation_target(conn, event)
            elif event.event_type == "payout_adjustment":
                target = self._adjustment_target(conn, event)
            cursor = conn.execute("INSERT INTO facts(ingestion_id,source,external_id,event_type,happened_at,day,batch_id,material_id,device_id,station_id,actor_id,street_id,community_id,weight_kg,payout_yuan,channel,dimensions_json,target_source,target_external_id,target_fact_id,original_day,adjustment_yuan) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (ingestion_id, event.source, event.external_id, event.event_type,
                                   event.happened_at, event.happened_at[:10], event.batch_id,
                                   event.material_id, event.device_id, event.station_id, event.actor_id,
                                   dims.get("street", {}).get("external_id"), dims.get("community", {}).get("external_id"),
                                   event.weight_kg, event.payout_yuan, event.channel,
                                   json.dumps(dims, ensure_ascii=False, sort_keys=True),
                                   event.target_source, event.target_external_id,
                                   target["id"] if target else None,
                                   target["original_day"] if target else None,
                                   event.adjustment_yuan))
            fact_id = cursor.lastrowid
            if event.event_type == "revocation":
                conn.execute("UPDATE facts SET state='revoked' WHERE id=? AND state='active'",
                             (target["id"],))
                self.store.audit(conn, "deposit_revoked", str(target["id"]),
                                 {"revocation_fact_id": fact_id, "original_day": target["day"],
                                  "batch_id": event.batch_id})
            conn.execute("UPDATE ingestions SET state='normalized', issue=NULL,normalized_id=? WHERE id=?",
                         (fact_id, ingestion_id))
            self.store.audit(conn, "normalized", str(fact_id), {"ingestion_id": ingestion_id,
                                                                 "dimension_versions": {k: v["version"] for k, v in dims.items()}})
        return {"ingestion_id": ingestion_id, "fact_id": fact_id, "state": "normalized", "created": created}

    def _revocation_target(self, conn, event: RawEvent):
        target_source = event.target_source or event.source
        target = self.store.one(conn, "SELECT * FROM facts WHERE source=? AND external_id=?",
                                (target_source, event.target_external_id))
        if target is None:
            raise LakeError("unknown_target", "revoked deposit is not normalized yet", 409)
        if target["event_type"] != "deposit":
            raise LakeError("invalid_target", "only deposits can be revoked", 409)
        if target["batch_id"] != event.batch_id:
            raise LakeError("target_mismatch", "revocation batch must match deposit batch", 409)
        if target["device_id"] and event.device_id != target["device_id"]:
            raise LakeError("target_mismatch", "revocation device must match deposit device", 409)
        if event.material_id and target["material_id"] and event.material_id != target["material_id"]:
            raise LakeError("target_mismatch", "revocation material must match deposit material", 409)
        if event.happened_at < target["happened_at"]:
            raise LakeError("invalid_sequence", "revocation precedes the deposit", 409)
        existing = self.store.one(conn, "SELECT id FROM facts WHERE event_type='revocation' AND target_fact_id=?",
                                  (target["id"],))
        if existing:
            raise LakeError("already_revoked", "deposit is already revoked", 409)
        target["original_day"] = target["day"]
        return target

    def _adjustment_target(self, conn, event: RawEvent):
        target = self.store.one(conn, "SELECT * FROM facts WHERE source=? AND external_id=? AND event_type='revocation'",
                                (event.target_source, event.target_external_id))
        if target is None:
            raise LakeError("unknown_revocation", "referenced revocation is not normalized yet", 409)
        if target["batch_id"] != event.batch_id:
            raise LakeError("target_mismatch", "adjustment batch must match revocation batch", 409)
        if event.happened_at < target["happened_at"]:
            raise LakeError("invalid_sequence", "adjustment precedes the revocation", 409)
        payees = {row["actor_id"] for row in
                  self.store.all(conn, "SELECT DISTINCT actor_id FROM facts WHERE batch_id=? AND event_type='payout'",
                                 (event.batch_id,))}
        if payees and event.actor_id not in payees:
            raise LakeError("payee_mismatch", "adjustment payee must match the batch payout payee", 409)
        return target

    def batch(self, batch_id: str):
        from .model import key
        batch_id = key(batch_id, "batch_id")
        return self.store.facts("WHERE batch_id=?", (batch_id,))

    def period(self, month: str):
        from .model import calendar_day
        calendar_day(month + "-01", "month")
        return self.store.facts("WHERE substr(day,1,7)=?", (month,))

    def trace(self, fact_id: int):
        fact = self.store.fact(fact_id)
        ingestion = self.store.ingestion(fact["ingestion_id"])
        return {"fact": fact, "landing": ingestion,
                "dimension_versions": fact["dimensions"]}

    def batch_balances(self, batch_id: str):
        facts = self.batch(batch_id)
        by_type = {}
        for fact in facts:
            kind = fact["event_type"]
            if kind not in by_type:
                by_type[kind] = {"count": 0, "weight_kg": Decimal("0"), "payout_yuan": Decimal("0"),
                                 "adjustment_yuan": Decimal("0")}
            by_type[kind]["count"] += 1
            by_type[kind]["weight_kg"] += Decimal(fact["weight_kg"] or "0")
            by_type[kind]["payout_yuan"] += Decimal(fact["payout_yuan"] or "0")
            by_type[kind]["adjustment_yuan"] += Decimal(fact["adjustment_yuan"] or "0")
        return {kind: {field: format(value, "f") if isinstance(value, Decimal) else value
                       for field, value in measures.items()} for kind, measures in by_type.items()}

    def payment_flow(self, batch_id: str):
        from .model import key
        batch_id = key(batch_id, "batch_id")
        facts = self.batch(batch_id)
        if not facts:
            raise LakeError("unknown_batch", "batch has no normalized facts", 404)
        relevant = [fact for fact in facts
                    if fact["event_type"] in {"payout", "revocation", "payout_adjustment"}]
        relevant.sort(key=lambda row: (row["happened_at"], row["id"]))
        entries = []
        running = Decimal("0")
        for fact in relevant:
            if fact["event_type"] == "payout":
                running += Decimal(fact["payout_yuan"] or "0")
                entries.append({"kind": "payout", "fact_id": fact["id"],
                                "source": fact["source"], "external_id": fact["external_id"],
                                "business_day": fact["day"], "processing_day": fact["day"],
                                "payee": fact["actor_id"], "amount_yuan": fact["payout_yuan"],
                                "batch_id": fact["batch_id"],
                                "target_fact_id": None, "target_source": None,
                                "target_external_id": None, "original_day": None,
                                "net_paid_yuan": decimal_text(running)})
            elif fact["event_type"] == "revocation":
                entries.append({"kind": "revocation", "fact_id": fact["id"],
                                "source": fact["source"], "external_id": fact["external_id"],
                                "business_day": fact["original_day"], "processing_day": fact["day"],
                                "payee": None, "amount_yuan": None,
                                "batch_id": fact["batch_id"],
                                "target_fact_id": fact["target_fact_id"],
                                "target_source": fact["target_source"] or fact["source"],
                                "target_external_id": fact["target_external_id"],
                                "original_day": fact["original_day"],
                                "net_paid_yuan": decimal_text(running)})
            else:
                running += Decimal(fact["adjustment_yuan"] or "0")
                entries.append({"kind": "payout_adjustment", "fact_id": fact["id"],
                                "source": fact["source"], "external_id": fact["external_id"],
                                "business_day": fact["original_day"], "processing_day": fact["day"],
                                "payee": fact["actor_id"], "amount_yuan": fact["adjustment_yuan"],
                                "batch_id": fact["batch_id"],
                                "target_fact_id": fact["target_fact_id"],
                                "target_source": fact["target_source"],
                                "target_external_id": fact["target_external_id"],
                                "original_day": fact["original_day"],
                                "net_paid_yuan": decimal_text(running)})
        paid = sum((Decimal(fact["payout_yuan"] or "0") for fact in facts
                    if fact["event_type"] == "payout"), Decimal("0"))
        adjusted = sum((Decimal(fact["adjustment_yuan"] or "0") for fact in facts
                        if fact["event_type"] == "payout_adjustment"), Decimal("0"))
        return {"batch_id": batch_id, "entries": entries,
                "paid_yuan": decimal_text(paid), "adjusted_yuan": decimal_text(adjusted),
                "net_paid_yuan": decimal_text(paid + adjusted)}
