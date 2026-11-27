"""Stage a downloaded, unpacked GDMS export for one existing located event.

Usage: python scripts/import_gdms.py EVENT_ID WAVEFORM.mseed RESPONSE.xml
Run from the configured project root; the running observatory consumes it on its
next GDMS tick. No webhook or new event is created by this command.
"""

import argparse
import shutil
import uuid

from tighnari_seisloc.config import Settings
from tighnari_seisloc.services.store import EventStore
from tighnari_seisloc.acquisition.fdsn import save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event_id")
    parser.add_argument("waveform")
    parser.add_argument(
        "inventory", help="StationXML with full response, not station coordinates only"
    )
    args = parser.parse_args()
    settings = Settings.read()
    store = EventStore(settings.database)
    event = store.get(args.event_id)
    if (
        not event
        or event.get("mode") != "live"
        or event.get("status") != "located"
        or event.get("summary_only")
    ):
        parser.error("需要現有、已定位、仍保留詳細資料的 live 事件（可無規模）")
    from pathlib import Path

    if (
        Path(args.event_id).name != args.event_id
        or args.event_id in (".", "..")
        or "\\" in args.event_id
    ):
        parser.error("事件 ID 不可包含路徑")
    target = settings.root / "runtime/gdms/inbox" / args.event_id
    target.mkdir(parents=True, exist_ok=True)
    # Publish immutable filenames first, then atomically switch the ready manifest.
    # A worker already reading the old manifest can finish without mixing exports.
    tag = uuid.uuid4().hex
    wave_name, xml_name = f"waveforms-{tag}.mseed", f"inventory-{tag}.xml"
    shutil.copyfile(args.waveform, target / wave_name)
    shutil.copyfile(args.inventory, target / xml_name)
    save_json(
        target / "manifest.json",
        dict(files=[dict(waveform=wave_name, inventory=xml_name)]),
    )
    with store.connect() as db:
        # Import can be run before the first server start; tick will create/enqueue jobs.
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gdms_jobs'"
        ).fetchone():
            db.execute(
                "UPDATE gdms_jobs SET state='pending',retry_at=0 WHERE event_id=?", (args.event_id,)
            )
    print("GDMS 匯出資料已排入既有事件補算佇列")


if __name__ == "__main__":
    main()
