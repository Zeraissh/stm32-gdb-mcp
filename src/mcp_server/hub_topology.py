"""Work out which hub channel each debug probe is plugged into.

USB topology cannot answer this. ``openocd_config`` only records a probe's
``location`` when it has NO serial number, and virtually every ST-Link has one --
so the field that would encode a physical port is absent in the common case. Nor
is there any documented correspondence between SmartUSBHub channel N and
downstream OS port N; that depends on internal wiring order.

So the mapping is discovered empirically: disconnect one channel's USB data
lines, ask ``detect_probe`` what disappeared, put it back. Data lines rather than
power, deliberately -- on most ST-Link rigs the target is powered from the same
VBUS, and discovery must not reboot a neighbouring board that is mid-experiment.

The result is written into config, which is then the source of truth. This module
only bootstraps it.
"""

from __future__ import annotations

from typing import Any


def probe_key(probe: dict) -> str | None:
    """A stable identity for one detected probe.

    Same fallback chain as ``openocd_config._deduplicate_probes``: serial when the
    probe has one, else the OS instance id, else the USB location. Two identical
    unserialised probes therefore still separate by physical port on Windows.
    """
    for field in ("serial", "instance_id", "location"):
        value = probe.get(field)
        if isinstance(value, str) and value.strip():
            return f"{field}:{value.strip()}"
    return None


def probe_keys(detection: dict) -> set[str]:
    keys = set()
    for probe in (detection or {}).get("probes", []) or []:
        key = probe_key(probe)
        if key:
            keys.add(key)
    return keys


def indexed_probes(detection: dict) -> dict[str, dict]:
    return {
        key: probe
        for probe in (detection or {}).get("probes", []) or []
        if (key := probe_key(probe))
    }


def map_from_config(hub_spec: dict | None) -> dict[int, dict]:
    """Normalize a config ``hub.map`` block into {channel: {serial?, label?}}."""
    from .hub import _normalize_map

    return _normalize_map((hub_spec or {}).get("map"))


def usb_location_hint(probe: dict) -> str | None:
    """Corroborating only -- never authoritative. See the module docstring."""
    location = probe.get("location") or probe.get("instance_id")
    return location if isinstance(location, str) and location.strip() else None


def discover_by_toggle(hub: Any, detect: Any, channels: tuple[int, ...],
                       sleep: Any, settle_sec: float = 1.0) -> dict:
    """Map channels to probes by dropping each channel's data lines in turn.

    ``detect`` is called once per channel plus once for the baseline. That is the
    expensive part -- on Windows each call walks the whole USB device tree at
    roughly 8.5 s -- which is why this is opt-in rather than something a session
    start does for you.

    Returns ``{"map", "ambiguous", "unmapped_probes", "baseline", "detect_calls"}``.
    A channel where more than one probe vanished is reported as ambiguous rather
    than guessed at.
    """
    baseline_detection = detect()
    baseline = probe_keys(baseline_detection)
    indexed = indexed_probes(baseline_detection)
    detect_calls = 1

    discovered: dict[int, dict] = {}
    ambiguous: list[dict] = []
    matched: set[str] = set()

    for channel in channels:
        try:
            hub.data(channel, "off", confirm=True)
        except Exception as exc:  # noqa: BLE001 - a channel we cannot toggle is just unmapped
            ambiguous.append({"channel": channel, "reason": f"could not disconnect: {exc}"})
            continue

        try:
            sleep(settle_sec)
            present = probe_keys(detect())
            detect_calls += 1
        finally:
            # Always restore, even if detection blew up: leaving a rack with a
            # data line down is worse than failing to map it.
            try:
                hub.data(channel, "on", confirm=True)
            except Exception:  # noqa: BLE001 - reported via the entry below
                ambiguous.append({"channel": channel, "reason": "could not reconnect data line"})
        sleep(settle_sec)

        vanished = sorted(baseline - present)
        if len(vanished) == 1:
            key = vanished[0]
            matched.add(key)
            probe = indexed.get(key, {})
            entry: dict = {}
            if probe.get("serial"):
                entry["serial"] = probe["serial"]
            if probe.get("type"):
                entry["probe"] = probe["type"]
            hint = usb_location_hint(probe)
            if hint:
                entry["usb_hint"] = hint
            entry["key"] = key
            discovered[channel] = entry
        elif len(vanished) > 1:
            ambiguous.append({
                "channel": channel,
                "reason": f"{len(vanished)} probes vanished at once; cannot attribute",
                "probes": vanished,
            })

    return {
        "map": discovered,
        "ambiguous": ambiguous,
        "unmapped_probes": sorted(baseline - matched),
        "baseline": sorted(baseline),
        "detect_calls": detect_calls,
    }
