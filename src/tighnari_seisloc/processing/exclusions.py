"""Manually excluded channels (TIGHNARI_EXCLUDED_CHANNELS), applied to every data path.

Rules are ``NET.STA.LOC.CHA`` separated by commas or whitespace; ``*`` and ``?`` are
wildcards per field and an empty location is written as ``NET.STA..CHA``.

* A two-letter channel (``TW.KNM.10.EH``) excludes the whole instrument.
* A Z component (``TW.ETLH.00.HHZ``) also excludes the whole record for picking,
  location, polarity, magnitude and display, which are all anchored on Z. Waveform
  inversions (CMT/CAP) still use the remaining horizontal traces.
* An N/E (or 1/2) component (``TW.PNG.00.HHN``) blanks only that column, so Z picks
  and polarities remain, while the local magnitude rejects the incomplete station.
"""

from dataclasses import replace
from fnmatch import fnmatchcase
import logging
import re

import numpy as np

LOG = logging.getLogger(__name__)
FIELD = r"[A-Za-z0-9*?]"
RULE = re.compile(
    rf"^({FIELD}{{1,8}})\.({FIELD}{{1,8}})\.({FIELD}{{0,4}})\.({FIELD}{{2}})([ZNE12*?])?$"
)
# Rotated records are Z, N, E; raw 1/2 horizontals map onto N/E.
AXIS = {"Z": 0, "N": 1, "1": 1, "E": 2, "2": 2}
ALL = frozenset("ZNE")

_rules = ()


def parse(text):
    """Return (rules, invalid entries) from the setting's text."""
    rules, invalid = [], []
    for entry in re.split(r"[\s,;]+", text or ""):
        if not entry:
            continue
        match = RULE.match(entry.strip())
        if match is None:
            invalid.append(entry)
            continue
        net, sta, loc, family, component = match.groups()
        rules.append((net.upper(), sta.upper(), loc.upper(), family.upper(), component))
    return tuple(rules), invalid


def configure(text):
    """Install the process-wide rules; invalid entries are logged and ignored."""
    global _rules
    _rules, invalid = parse(text)
    if invalid:
        LOG.warning("Ignored invalid TIGHNARI_EXCLUDED_CHANNELS entries: %s", ", ".join(invalid))
    if _rules:
        LOG.info("Excluding %d channel rule(s)", len(_rules))
    return _rules


def rules():
    return _rules


def excluded_components(record_id, active=None):
    """Excluded rotated components ({'Z','N','E'} subset) of ``NET.STA.LOC.FAM``."""
    parts = record_id.split(".")
    if len(parts) != 4:
        return frozenset()
    net, sta, loc, family = (p.upper() for p in parts)
    found = set()
    for r_net, r_sta, r_loc, r_family, component in _rules if active is None else active:
        if not (
            fnmatchcase(net, r_net)
            and fnmatchcase(sta, r_sta)
            and fnmatchcase(loc, r_loc)
            and fnmatchcase(family, r_family)
        ):
            continue
        if component in (None, "*", "?"):
            return ALL
        found.add("ZNE"[AXIS[component]])
    return frozenset(found)


def apply_records(records, active=None):
    """Drop or blank excluded data in calibrated Records (columns Z, N, E)."""
    kept = []
    for record in records:
        components = excluded_components(record.id, active)
        if not components:
            kept.append(record)
            continue
        if "Z" in components:
            continue
        velocity = np.array(record.velocity, dtype=float, copy=True)
        for name in components:
            velocity[:, AXIS[name]] = np.nan
        metadata = dict(record.metadata, excluded_components=sorted(components))
        kept.append(replace(record, velocity=velocity, metadata=metadata))
    return kept


def filter_stream(stream, active=None):
    """Remove excluded traces from an ObsPy Stream in place and return it."""
    for trace in list(stream):
        component = trace.stats.channel[-1:].upper()
        family = trace.stats.channel[:2]
        record_id = ".".join(
            (trace.stats.network, trace.stats.station, trace.stats.location, family)
        )
        components = excluded_components(record_id, active)
        axis = AXIS.get(component)
        if components and (components == ALL or axis is None or "ZNE"[axis] in components):
            stream.remove(trace)
    return stream
