"""GDMS export ingestion; never guess calibration, credentials, or a download API.

A ready manifest is the atomic handoff from HTTP downloads or manual exports.
"""

import json
from io import BytesIO
from pathlib import Path

from .fdsn import calibrated_records, checksum


class GdmsUnavailable(RuntimeError):
    pass


def event_inventory(path, start, end):
    """Select complete channel responses before ObsPy expands the network archive.

    GDMS exports all instrument epochs; loading every historical response into the
    live locator wastes memory. The original file is retained and hashed unchanged.
    """
    from lxml import etree
    from obspy import read_inventory, UTCDateTime

    parser = etree.iterparse(
        str(path), tag=("{*}Station", "{*}Channel"), resolve_entities=False, no_network=True
    )
    for _, element in parser:
        parent = element.getparent()
        if etree.QName(element).localname == "Channel":
            if parent is None or etree.QName(parent).localname != "Station":
                continue
            first = element.get("startDate")
            last = element.get("endDate")
            if (
                not element.get("code", "").startswith(("BH", "HH", "EH", "HN"))
                or (first and float(UTCDateTime(first)) > end)
                or (last and float(UTCDateTime(last)) < start)
            ):
                parent.remove(element)
        elif parent is not None and element.find("{*}Channel") is None:
            parent.remove(element)
    selected = etree.tostring(parser.root, encoding="UTF-8", xml_declaration=True)
    element = parent = parser = None
    return read_inventory(BytesIO(selected), format="STATIONXML")


def acquire_gdms(start, end, inbox):
    from obspy import read, UTCDateTime

    inbox = Path(inbox).resolve()
    ready = inbox / "manifest.json"
    if not ready.is_file():
        raise GdmsUnavailable("等待 GDMS 匯出資料")
    manifest = json.loads(ready.read_text(encoding="utf-8"))
    records, provenance, errors = [], [], []
    inventories = {}
    for entry in manifest["files"]:
        paths = []
        for key in ("waveform", "inventory"):
            path = (inbox / entry[key]).resolve()
            if not path.is_relative_to(inbox):
                raise ValueError("GDMS export must stay inside event inbox")
            paths.append(path)
        wave, xml = paths
        wave_hash, xml_hash = checksum(wave), checksum(xml)
        stream = read(str(wave))
        stream.trim(UTCDateTime(start - 2), UTCDateTime(end + 2))
        if xml_hash not in inventories:
            inventories[xml_hash] = event_inventory(xml, start, end)
        found, rejected = calibrated_records(stream, inventories[xml_hash], "gdms")
        records.extend(r for r in found if r.start < end and r.end > start)
        errors.extend(rejected)
        if checksum(wave) != wave_hash or checksum(xml) != xml_hash:
            raise GdmsUnavailable("GDMS 匯出檔案正在更新，稍後重試")
        provenance.append(dict(waveform_sha256=wave_hash, inventory_sha256=xml_hash))
    if not records:
        raise GdmsUnavailable("GDMS 波形缺少有效響應或未涵蓋事件時間")
    return records, dict(
        source="GDMS / CWA",
        station_count=len({r.id for r in records}),
        files=provenance,
        errors=errors,
    )
