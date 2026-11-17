from __future__ import annotations

from tempfile import TemporaryDirectory
from pathlib import Path
import json

from .service import LakeService


def dimensions(service: LakeService):
    service.register_source("box-feed", "smart_box")
    service.register_source("scale-feed", "scale")
    service.register_source("pay-feed", "payment")
    service.register_dimension({"kind": "street", "external_id": "street-1", "name": "合成街道",
                                "valid_from": "2026-01-01"})
    service.register_dimension({"kind": "community", "external_id": "community-1", "name": "合成社区",
                                "valid_from": "2026-01-01", "attributes": {"street_id": "street-1"}})
    service.register_dimension({"kind": "device", "external_id": "box-1", "name": "合成回收箱",
                                "valid_from": "2026-01-01", "attributes": {"community_id": "community-1"}})
    service.register_dimension({"kind": "station", "external_id": "station-1", "name": "合成中转站",
                                "valid_from": "2026-01-01"})
    service.register_dimension({"kind": "material", "external_id": "foam", "name": "泡沫",
                                "valid_from": "2026-01-01"})
    service.register_dimension({"kind": "actor", "external_id": "collector-1", "name": "合成投递人",
                                "valid_from": "2026-01-01"})
    service.settlement.put_rate("foam", "smart_box", "2026-01-01", "0.08")


def events():
    base = {"happened_at": "2026-07-15T10:00:00+08:00", "batch_id": "batch-1"}
    return [
        {**base, "source": "box-feed", "external_id": "box-event-1", "event_type": "deposit",
         "channel": "smart_box", "material_id": "foam", "device_id": "box-1", "weight_kg": "10"},
        {**base, "source": "scale-feed", "external_id": "scale-event-1", "event_type": "weigh_in",
         "channel": "scale", "material_id": "foam", "station_id": "station-1", "weight_kg": "10"},
        {**base, "source": "pay-feed", "external_id": "pay-event-1", "event_type": "payout",
         "channel": "payment", "actor_id": "collector-1", "payout_yuan": "0.8"},
    ]


def clawback_events():
    return [
        {"source": "box-feed", "external_id": "box-revoke-1", "event_type": "revocation",
         "happened_at": "2026-08-03T10:00:00+08:00", "batch_id": "batch-1",
         "channel": "smart_box", "device_id": "box-1", "target_external_id": "box-event-1"},
        {"source": "pay-feed", "external_id": "pay-adjust-1", "event_type": "payout_adjustment",
         "happened_at": "2026-08-10T10:00:00+08:00", "batch_id": "batch-1",
         "channel": "payment", "actor_id": "collector-1", "adjustment_yuan": "-0.8",
         "target_source": "box-feed", "target_external_id": "box-revoke-1"},
    ]


def run_demo():
    with TemporaryDirectory() as folder:
        service = LakeService(Path(folder) / "lake.sqlite3")
        dimensions(service)
        outcomes = [service.ingest(event) for event in events()]
        if any(row["state"] != "normalized" for row in outcomes):
            raise RuntimeError("synthetic events did not normalize")
        duplicate = service.ingest(events()[0])
        if duplicate["created"]:
            raise RuntimeError("duplicate event created another fact")
        checked = service.batch_status("batch-1")
        if not checked["ready"] or checked["deposit_kg"] != "10":
            raise RuntimeError("material balance failed")
        preview = service.preview("2026-07")
        if not preview["ready"] or preview["mart"]["rows"][0]["deposit_kg"] != "10":
            raise RuntimeError("mart preview failed")
        release = service.publish("july-final", "2026-07", preview["fingerprint"])
        frozen = service.releases.get(release["release_id"])
        if frozen["fingerprint"] != preview["fingerprint"]:
            raise RuntimeError("release fingerprint mismatch")
        lineage = service.lineage.fact(outcomes[0]["fact_id"])
        if lineage["fact"]["street_id"] != "street-1":
            raise RuntimeError("fact lineage lost effective dimension")
        clawback = [service.ingest(event) for event in clawback_events()]
        if any(row["state"] != "normalized" for row in clawback):
            raise RuntimeError("clawback events did not normalize")
        replay = service.ingest(clawback_events()[1])
        if replay["created"]:
            raise RuntimeError("replayed adjustment charged twice")
        checked = service.batch_status("batch-1")
        if not checked["ready"] or checked["settlement"]["net_yuan"] != "0":
            raise RuntimeError("clawback did not balance the batch")
        frozen = service.releases.get(release["release_id"])
        if frozen["content"]["mart"]["rows"][0]["deposit_kg"] != "10":
            raise RuntimeError("published month was retroactively modified")
        august = service.month_report("2026-08")
        if august["rows"][0]["adjustment_yuan"] != "-0.8":
            raise RuntimeError("clawback did not land in the processing month")
        flow = service.payment_flow("batch-1")
        if flow["net_paid_yuan"] != "0" or flow["entries"][-1]["original_day"] != "2026-07-15":
            raise RuntimeError("payment flow lost traceability")
        return {"result": "ok", "normalized_fact_ids": [row["fact_id"] for row in outcomes],
                "duplicate_created": duplicate["created"], "batch_ready": checked["ready"],
                "deposit_kg": checked["deposit_kg"], "release_id": release["release_id"],
                "release_fingerprint": frozen["fingerprint"],
                "revoked_kg": checked["revoked_deposit_kg"],
                "clawback_month": august["rows"][0]["month"],
                "clawback_yuan": august["rows"][0]["adjustment_yuan"],
                "frozen_july_deposit_kg": frozen["content"]["mart"]["rows"][0]["deposit_kg"],
                "net_paid_yuan": flow["net_paid_yuan"],
                "adjustment_replay_created": replay["created"]}


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, sort_keys=True))
