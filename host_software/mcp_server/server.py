"""FastMCP server exposing TDS784A oscilloscope tools over the AR-488-ESP32.

Tool surface follows ../MCP_Tools_for_TDS784A_measurements_revised.md.
Helpers (drain_errors, atomic ops) live here too — they are short and
only this module uses them, per the implementation plan.
"""
import asyncio
import base64
import io
import json
import re
import statistics
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from request_gpib import (
    capture_channel,
    collect_metadata,
    decode_samples,
    iso_stamp,
    parse_ieee_block,
    parse_wfmpre,
    query_value,
)

from .client import GpibClient

_INSTRUCTIONS = """\
TDS784A acquisition model: the scope stores a fixed 50 points per DIVISION,
and records >500 pts span MORE than the 10 visible divisions (menu reads
"15000 points in 300divs"). Sample interval = (s/div)/50 — NOT
(10*s/div)/record_length (wrong by 10-30x). To target a resolution, set
s/div = 50 * dt_wanted (e.g. 40ns/sample -> 2us/div); record_length only
extends the captured duration. Screen shows only 500 pts — never judge a
record by the screen. Always confirm actual dt from the returned preamble
XINCR/time_s. SAMPLE vs HIRES does not change this rate rule; use SAMPLE
for logic decoding. HORIZONTAL:TRIGGER:POSITION positions the trigger in
the FULL record (PT_OFF), not on screen.

get_waveform ARMS A NEW acquisition and destroys any frozen capture — to
read an already-captured record non-destructively (or for bulk transfers
that exceed tool output limits), use host_software/request_gpib.py
--waveform instead (reads DATA/CURVE only, writes CSV).

SCPI gotchas (v4.1e): send ONE command per raw_scpi call (compound ';:'
chains throw header error 110); SELECT:CHn ON is required before reading a
channel (else error 2241 / capture failed); prefer DC trigger coupling for
slow signals (AC false-triggers on HF ripple); error 531 means the
DATA:START/STOP window exceeds record_length.
"""

mcp = FastMCP("tek-tds784a", instructions=_INSTRUCTIONS)
client = GpibClient()


# ---------------------------------------------------------------- helpers

_NUMERIC_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _strip_header(text: str) -> str:
    """Tek replies are either bare values or ':HEADER:NAME VALUE' — return
    just the value substring. Only strips a leading ':...HEADER ' prefix
    when present, so values with internal spaces (e.g. *IDN? returning
    'TEKTRONIX,TDS 784A,...') are preserved."""
    if text is None:
        return ""
    text = text.strip()
    if text.startswith(":"):
        sp = text.find(" ")
        if sp >= 0:
            text = text[sp + 1:]
    return text.rstrip(";").strip()


def _to_float(text: str) -> Optional[float]:
    if text is None:
        return None
    m = _NUMERIC_RE.search(text)
    return float(m.group()) if m else None


def _ok(meta: dict) -> bool:
    return bool(meta and meta.get("ok"))


def _err(meta: dict) -> str:
    return (meta or {}).get("error", "unknown error")


async def _query(command: str, timeout_ms: Optional[int] = None) -> dict:
    """Run a query and return the raw meta dict from the gateway."""
    meta, _ = await client.request("query", command, timeout_ms=timeout_ms)
    return meta


async def _write(command: str, timeout_ms: Optional[int] = None) -> dict:
    meta, _ = await client.request("write", command, timeout_ms=timeout_ms)
    return meta


async def _binary_query(command: str, timeout_ms: Optional[int] = None):
    return await client.request("binary_query", command, timeout_ms=timeout_ms)


async def drain_errors() -> dict:
    """Run *ESR? then ALLEv? to drain the scope's event queue.

    Returns {esr: int, errors: [{code, message}]}. ESR=0 with no events is
    the clean case. Every state-changing tool drains and surfaces this in
    its response (alongside the success payload, not as an exception)."""
    esr_meta = await _query("*ESR?")
    esr_text = _strip_header(esr_meta.get("data", "")) if _ok(esr_meta) else ""
    try:
        esr = int(esr_text) if esr_text else 0
    except ValueError:
        esr = 0

    errs: list[dict] = []
    ev_meta = await _query("ALLEv?")
    if _ok(ev_meta):
        data = ev_meta.get("data", "").strip()
        # Strip a leading ":ALLEV " header if present.
        if data.startswith(":"):
            sp = data.find(" ")
            if sp >= 0:
                data = data[sp + 1:]
        # Format: <code>,"<msg>"[;<code>,"<msg>"...]
        # Messages may contain ',' and ';' so a naive split breaks. Walk the
        # string respecting double-quoted runs.
        for code, msg in _split_allev(data):
            # The "no events" sentinel: code==0, msg matches "*queue empty*".
            if code == 0 and "queue empty" in msg.lower():
                continue
            errs.append({"code": code, "message": msg})
    return {"esr": esr, "errors": errs}


def _split_allev(data: str):
    """Yield (code:int, message:str) pairs from an ALLEv? payload like
    `0,"msg";12,"another, with comma; and semicolon"`."""
    i = 0
    n = len(data)
    while i < n:
        # Skip leading whitespace and separators.
        while i < n and data[i] in " ;\t\r\n":
            i += 1
        if i >= n:
            break
        # Read the integer code up to the next ','.
        j = i
        while j < n and data[j] != ",":
            j += 1
        code_str = data[i:j].strip()
        try:
            code = int(code_str)
        except ValueError:
            # Unparseable — skip to next ';'.
            sc = data.find(";", j)
            i = sc + 1 if sc >= 0 else n
            continue
        i = j + 1  # past the comma
        # Expect an opening quote.
        while i < n and data[i] in " \t":
            i += 1
        if i >= n or data[i] != '"':
            # Bare value (no quoted message) — read to next ';'.
            sc = data.find(";", i)
            msg = data[i:sc if sc >= 0 else n].strip()
            yield code, msg
            i = sc + 1 if sc >= 0 else n
            continue
        # Read quoted string. Tek's manual doesn't define an escape; the
        # closing quote is the first " followed by ';' or end-of-string.
        i += 1  # past opening "
        msg_start = i
        while i < n:
            if data[i] == '"' and (i + 1 == n or data[i + 1] in " ;\t\r\n"):
                break
            i += 1
        msg = data[msg_start:i]
        yield code, msg
        if i < n and data[i] == '"':
            i += 1


async def _opc_wait(timeout_s: float) -> dict:
    """Poll *OPC? until it returns 1 or timeout. Returns {ok, elapsed_s}."""
    deadline = time.monotonic() + timeout_s
    started = time.monotonic()
    poll_timeout_ms = max(client.timeout_ms, 5000)
    while True:
        meta = await _query("*OPC?", timeout_ms=poll_timeout_ms)
        if _ok(meta):
            text = _strip_header(meta.get("data", ""))
            if text.startswith("1"):
                return {"ok": True, "elapsed_s": time.monotonic() - started}
        if time.monotonic() >= deadline:
            return {"ok": False, "elapsed_s": time.monotonic() - started}
        await asyncio.sleep(0.05)


async def _arm_single_sequence(timeout_s: float) -> dict:
    """Sequence: ACQUIRE:STOPAFTER SEQUENCE; ACQUIRE:STATE RUN; *OPC?."""
    for cmd in ("ACQUIRE:STOPAFTER SEQUENCE", "ACQUIRE:STATE RUN"):
        m = await _write(cmd)
        if not _ok(m):
            return {"ok": False, "error": _err(m)}
    return await _opc_wait(timeout_s)


def _ch_token(ch: int | str) -> str:
    """Normalize 1/2/3/4 or 'CH1' to 'CH1'."""
    if isinstance(ch, int):
        return f"CH{ch}"
    s = str(ch).upper().strip()
    if s.startswith("CH"):
        return s
    return f"CH{s}"


# -------------------------------------------------------------- 1. Plumbing


@mcp.tool()
async def raw_scpi(
    command: str,
    expect_reply: Optional[bool] = None,
    binary: bool = False,
    timeout_ms: Optional[int] = None,
) -> dict:
    """Send an arbitrary SCPI command. Escape hatch for anything not exposed
    as a higher-level tool.

    expect_reply: None → autodetect from '?' in command (matches CLI behavior).
                  True → query (binary if `binary=True`).
                  False → write (drains errors).
    """
    if expect_reply is None:
        expect_reply = "?" in command
    if expect_reply:
        action = "binary_query" if binary else "query"
        meta, payload = await client.request(action, command, timeout_ms=timeout_ms)
        out: dict[str, Any] = {"ok": _ok(meta), "raw": meta}
        if not _ok(meta):
            out["error"] = _err(meta)
            return out
        if binary and payload is not None:
            body = parse_ieee_block(payload)
            out["bytes_len"] = len(body)
            out["bytes_b64"] = base64.b64encode(body).decode("ascii")
        else:
            out["data"] = meta.get("data", "")
        return out
    else:
        meta = await _write(command, timeout_ms=timeout_ms)
        return {
            "ok": _ok(meta),
            "error": None if _ok(meta) else _err(meta),
            "errors_after": await drain_errors(),
        }


@mcp.tool()
async def gateway_firmware_version() -> dict:
    """Return the AR-488-ESP32 gateway firmware version (no GPIB traffic)."""
    meta, _ = await client.request("version", "")
    if not _ok(meta):
        return {"ok": False, "error": _err(meta)}
    return {"ok": True, "version": meta.get("version", "")}


@mcp.tool()
async def verify_instrument_identity() -> dict:
    """Return *IDN? parsed into {vendor, model, serial, firmware}."""
    meta = await _query("*IDN?")
    if not _ok(meta):
        return {"ok": False, "error": _err(meta)}
    raw = _strip_header(meta.get("data", ""))
    parts = [p.strip() for p in raw.split(",")]
    while len(parts) < 4:
        parts.append("")
    return {
        "ok": True,
        "vendor": parts[0],
        "model": parts[1],
        "serial": parts[2],
        "firmware": parts[3],
        "raw": raw,
    }


@mcp.tool()
async def get_errors() -> dict:
    """Drain the scope's error queue (*ESR? + ALLEv?). Read-only — no other
    tool drains errors implicitly when called."""
    return await drain_errors()


@mcp.tool()
async def wait_operation_complete(timeout_s: float = 10.0) -> dict:
    """Poll *OPC? until it returns 1 or `timeout_s` elapses."""
    return await _opc_wait(timeout_s)


# --------------------------------------------------------- 2. Setup state


@mcp.tool()
async def get_setup_state() -> dict:
    """Capture the full scope state via HEADER ON + SET?.

    Returns {setup_string, idn, captured_at}. The setup_string can be sent
    back verbatim through `set_setup_state`."""
    # SET? can be large and slow; allow generous timeout.
    set_timeout = max(client.timeout_ms, 15000)
    hdr = await _write("HEADER ON")
    if not _ok(hdr):
        return {"ok": False, "error": _err(hdr)}
    meta = await _query("SET?", timeout_ms=set_timeout)
    if not _ok(meta):
        return {"ok": False, "error": _err(meta)}
    setup_string = meta.get("data", "")
    idn = await query_value(client.ws, client.addr, client.timeout_ms, "*IDN?")
    return {
        "ok": True,
        "setup_string": setup_string,
        "idn": idn,
        "captured_at": iso_stamp(datetime.now()),
    }


@mcp.tool()
async def set_setup_state(setup_string: str, confirm: bool = False) -> dict:
    """Restore a setup captured by `get_setup_state`.

    Sends the string verbatim, then *OPC?, then drains errors.
    REFUSES unless confirm=True (large blind state change)."""
    if not confirm:
        return {
            "ok": False,
            "error": "refused: set_setup_state requires confirm=True",
        }
    # Generous timeout: arbitrary setup + OPC can take a few seconds.
    meta = await _write(setup_string, timeout_ms=max(client.timeout_ms, 15000))
    if not _ok(meta):
        return {"ok": False, "error": _err(meta), "errors_after": await drain_errors()}
    opc = await _opc_wait(15.0)
    return {"ok": opc["ok"], "opc": opc, "errors_after": await drain_errors()}


@mcp.tool()
async def save_internal(slot: int) -> dict:
    """*SAV <slot> — save current setup to internal NVRAM slot 1..10."""
    if not 1 <= slot <= 10:
        return {"ok": False, "error": "slot must be in 1..10"}
    meta = await _write(f"*SAV {slot}")
    return {
        "ok": _ok(meta),
        "error": None if _ok(meta) else _err(meta),
        "errors_after": await drain_errors(),
    }


@mcp.tool()
async def recall_internal(slot: int) -> dict:
    """*RCL <slot> — recall a previously saved setup."""
    if not 1 <= slot <= 10:
        return {"ok": False, "error": "slot must be in 1..10"}
    meta = await _write(f"*RCL {slot}")
    return {
        "ok": _ok(meta),
        "error": None if _ok(meta) else _err(meta),
        "errors_after": await drain_errors(),
    }


@mcp.tool()
async def factory_reset(confirm: bool = False) -> dict:
    """FACtory — wipe ALL NVRAM setups back to factory defaults.
    Most disruptive tool. REFUSES unless confirm=True."""
    if not confirm:
        return {
            "ok": False,
            "error": "refused: factory_reset requires confirm=True",
        }
    meta = await _write("FACtory", timeout_ms=max(client.timeout_ms, 15000))
    return {
        "ok": _ok(meta),
        "error": None if _ok(meta) else _err(meta),
        "errors_after": await drain_errors(),
    }


# -------------------------------------------------------- 3. Acquisition


@mcp.tool()
async def autoset(timeout_s: float = 15.0) -> dict:
    """AUTOSet EXECute — auto-scale to the current signal."""
    meta = await _write("AUTOSet EXECute")
    if not _ok(meta):
        return {"ok": False, "error": _err(meta), "errors_after": await drain_errors()}
    opc = await _opc_wait(timeout_s)
    return {"ok": opc["ok"], "opc": opc, "errors_after": await drain_errors()}


_ACQ_MODES = {"SAMPLE", "PEAKDETECT", "HIRES", "ENVELOPE", "AVERAGE"}


@mcp.tool()
async def set_acquisition_mode(mode: str) -> dict:
    """ACQUIRE:MODE <mode>. mode ∈ {SAMPLE, PEAKDETECT, HIRES, ENVELOPE, AVERAGE}."""
    m = mode.upper()
    if m not in _ACQ_MODES:
        return {"ok": False, "error": f"mode must be one of {sorted(_ACQ_MODES)}"}
    meta = await _write(f"ACQUIRE:MODE {m}")
    return {
        "ok": _ok(meta),
        "error": None if _ok(meta) else _err(meta),
        "errors_after": await drain_errors(),
    }


@mcp.tool()
async def set_average_count(n: int) -> dict:
    """ACQUIRE:NUMAVG <n>. n ∈ {2,4,8,...,10000} per the TDS784A manual."""
    valid = {2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 10000}
    if n not in valid:
        return {"ok": False, "error": f"n must be one of {sorted(valid)}"}
    meta = await _write(f"ACQUIRE:NUMAVG {n}")
    return {
        "ok": _ok(meta),
        "error": None if _ok(meta) else _err(meta),
        "errors_after": await drain_errors(),
    }


@mcp.tool()
async def set_acquisition_state(state: str) -> dict:
    """ACQUIRE:STATE RUN | STOP."""
    s = state.upper()
    if s not in {"RUN", "STOP"}:
        return {"ok": False, "error": "state must be RUN or STOP"}
    meta = await _write(f"ACQUIRE:STATE {s}")
    return {
        "ok": _ok(meta),
        "error": None if _ok(meta) else _err(meta),
        "errors_after": await drain_errors(),
    }


@mcp.tool()
async def arm_single_and_wait(timeout_s: float = 30.0) -> dict:
    """Atomic single-sequence acquire. Sets STOPAFTER SEQUENCE, RUN, polls
    *OPC?. Returns when the scope reports OPC, or errors on timeout.

    This is the building block to guarantee fresh data underneath any
    measurement / waveform read."""
    res = await _arm_single_sequence(timeout_s)
    res["errors_after"] = await drain_errors()
    return res


# ---------------------------------------------- 4. Vertical / horizontal / trigger


_COUPLINGS = {"AC", "DC", "GND"}
_BANDWIDTHS = {"FULL", "TWENTY", "HUNDRED", "TWOFIFTY"}
_IMPEDANCES = {"FIFTY", "MEG"}


@mcp.tool()
async def set_vertical(
    ch: int,
    scale_v: Optional[float] = None,
    position_div: Optional[float] = None,
    offset_v: Optional[float] = None,
    coupling: Optional[str] = None,
    bandwidth: Optional[str] = None,
    impedance: Optional[str] = None,
    probe_atten: Optional[float] = None,
    units: Optional[str] = None,
    deskew_s: Optional[float] = None,
    invert: Optional[bool] = None,
) -> dict:
    """Send only the per-channel sub-commands whose argument is provided.

    Note: position_div is the trace's vertical position in divisions
    (visual), offset_v is a DC bias subtracted from the input (electrical).
    For most use cases you want position_div."""
    chtok = _ch_token(ch)
    cmds: list[str] = []
    if scale_v is not None:
        cmds.append(f"{chtok}:SCALE {scale_v:.9g}")
    if position_div is not None:
        cmds.append(f"{chtok}:POSITION {position_div:.9g}")
    if offset_v is not None:
        cmds.append(f"{chtok}:OFFSET {offset_v:.9g}")
    if coupling is not None:
        c = coupling.upper()
        if c not in _COUPLINGS:
            return {"ok": False, "error": f"coupling must be one of {sorted(_COUPLINGS)}"}
        cmds.append(f"{chtok}:COUPLING {c}")
    if bandwidth is not None:
        b = bandwidth.upper()
        if b not in _BANDWIDTHS:
            return {"ok": False, "error": f"bandwidth must be one of {sorted(_BANDWIDTHS)}"}
        cmds.append(f"{chtok}:BANDWIDTH {b}")
    if impedance is not None:
        i = impedance.upper()
        if i not in _IMPEDANCES:
            return {"ok": False, "error": f"impedance must be one of {sorted(_IMPEDANCES)}"}
        cmds.append(f"{chtok}:IMPEDANCE {i}")
    if probe_atten is not None:
        cmds.append(f"{chtok}:PROBE {probe_atten:.9g}")
    if units is not None:
        cmds.append(f'{chtok}:UNITS "{units}"')
    if deskew_s is not None:
        cmds.append(f"{chtok}:DESKEW {deskew_s:.9g}")
    if invert is not None:
        cmds.append(f"{chtok}:INVERT {'ON' if invert else 'OFF'}")

    if not cmds:
        return {"ok": False, "error": "no parameters provided"}

    sent = []
    for cmd in cmds:
        m = await _write(cmd)
        sent.append({"cmd": cmd, "ok": _ok(m), "error": None if _ok(m) else _err(m)})
        if not _ok(m):
            break
    all_ok = all(s["ok"] for s in sent)
    return {"ok": all_ok, "sent": sent, "errors_after": await drain_errors()}


@mcp.tool()
async def set_horizontal(
    scale_s: Optional[float] = None,
    position_pct: Optional[float] = None,
    record_length: Optional[int] = None,
) -> dict:
    """HORIZONTAL:MAIN:SCALE / TRIGGER:POSITION / RECORDLENGTH."""
    cmds: list[str] = []
    if scale_s is not None:
        cmds.append(f"HORIZONTAL:MAIN:SCALE {scale_s:.9g}")
    if position_pct is not None:
        cmds.append(f"HORIZONTAL:TRIGGER:POSITION {position_pct:.9g}")
    if record_length is not None:
        cmds.append(f"HORIZONTAL:RECORDLENGTH {record_length}")
    if not cmds:
        return {"ok": False, "error": "no parameters provided"}
    sent = []
    for cmd in cmds:
        m = await _write(cmd)
        sent.append({"cmd": cmd, "ok": _ok(m), "error": None if _ok(m) else _err(m)})
        if not _ok(m):
            break
    return {
        "ok": all(s["ok"] for s in sent),
        "sent": sent,
        "errors_after": await drain_errors(),
    }


_SLOPES = {"RISE", "FALL"}
_TRIG_COUPLINGS = {"AC", "DC", "HFREJ", "LFREJ", "NOISEREJ"}
_TRIG_MODES = {"AUTO", "NORMAL"}


@mcp.tool()
async def set_trigger_edge(
    source: Optional[str] = None,
    level_v: Optional[float] = None,
    slope: Optional[str] = None,
    coupling: Optional[str] = None,
    holdoff_s: Optional[float] = None,
    mode: Optional[str] = None,
) -> dict:
    """Configure an edge trigger. Sends TRIGGER:MAIN:TYPE EDGE first, then
    only the sub-fields whose argument is provided.

    v1 supports edge triggers only. Use raw_scpi for pulse/glitch/runt/logic."""
    cmds: list[str] = ["TRIGGER:MAIN:TYPE EDGE"]
    if source is not None:
        cmds.append(f"TRIGGER:MAIN:EDGE:SOURCE {_ch_token(source)}")
    if level_v is not None:
        cmds.append(f"TRIGGER:MAIN:LEVEL {level_v:.9g}")
    if slope is not None:
        s = slope.upper()
        if s not in _SLOPES:
            return {"ok": False, "error": f"slope must be one of {sorted(_SLOPES)}"}
        cmds.append(f"TRIGGER:MAIN:EDGE:SLOPE {s}")
    if coupling is not None:
        c = coupling.upper()
        if c not in _TRIG_COUPLINGS:
            return {"ok": False, "error": f"coupling must be one of {sorted(_TRIG_COUPLINGS)}"}
        cmds.append(f"TRIGGER:MAIN:EDGE:COUPLING {c}")
    if holdoff_s is not None:
        cmds.append(f"TRIGGER:MAIN:HOLDOFF:VALUE {holdoff_s:.9g}")
    if mode is not None:
        mo = mode.upper()
        if mo not in _TRIG_MODES:
            return {"ok": False, "error": f"mode must be one of {sorted(_TRIG_MODES)}"}
        cmds.append(f"TRIGGER:MAIN:MODE {mo}")
    sent = []
    for cmd in cmds:
        m = await _write(cmd)
        sent.append({"cmd": cmd, "ok": _ok(m), "error": None if _ok(m) else _err(m)})
        if not _ok(m):
            break
    return {
        "ok": all(s["ok"] for s in sent),
        "sent": sent,
        "errors_after": await drain_errors(),
    }


@mcp.tool()
async def get_acquisition_setup(channels: Optional[list[str]] = None) -> dict:
    """Read-only setup snapshot via collect_metadata: horizontal, trigger,
    acquire, per-channel state. Cheaper alternative to a full SET? when
    you just want context."""
    if not channels:
        channels = ["CH1", "CH2", "CH3", "CH4"]
    chans = [_ch_token(c) for c in channels]
    args = SimpleNamespace(addr=client.addr, timeout=client.timeout_ms)
    meta = await client.with_ws(collect_metadata, args, chans, datetime.now())
    return {"ok": True, "setup": meta}


# --------------------------------------------------------- 5. Measurements


async def _measure_immed(ch: int | str, kind: str, source2: Optional[str]) -> dict:
    chtok = _ch_token(ch)
    cmds = [
        f"MEASUREMENT:IMMED:TYPE {kind.upper()}",
        f"MEASUREMENT:IMMED:SOURCE1 {chtok}",
    ]
    if source2 is not None:
        cmds.append(f"MEASUREMENT:IMMED:SOURCE2 {_ch_token(source2)}")
    for cmd in cmds:
        m = await _write(cmd)
        if not _ok(m):
            return {"ok": False, "error": f"{cmd}: {_err(m)}"}
    val_meta = await _query("MEASUREMENT:IMMED:VALUE?")
    if not _ok(val_meta):
        return {"ok": False, "error": _err(val_meta)}
    value = _to_float(_strip_header(val_meta.get("data", "")))
    units_meta = await _query("MEASUREMENT:IMMED:UNITS?")
    unit = _strip_header(units_meta.get("data", "")) if _ok(units_meta) else ""
    unit = unit.strip('"')
    return {"ok": True, "value": value, "unit": unit}


@mcp.tool()
async def measure(
    ch: int,
    kind: str,
    source2: Optional[str] = None,
    gating: bool = False,
) -> dict:
    """Immediate measurement via MEASUREMENT:IMMED. Does NOT disturb the
    on-screen MEAS1..4 slots.

    kind: AMPL, FREQ, PERIOD, RISE, FALL, PK2PK, MEAN, RMS, CRMS, DUTY,
    HIGH, LOW, MAX, MIN, AREA, CAREA, CMEAN, BURST, NDUTY, NOVERSHOOT,
    NWIDTH, PDUTY, POVERSHOOT, PWIDTH, PHASE, DELAY (delay-type takes source2).

    gating: False → MEASUREMENT:GATING OFF (measure full record).
            True  → MEASUREMENT:GATING ON  (measure between vertical cursors).
    """
    g = await _write(f"MEASUREMENT:GATING {'ON' if gating else 'OFF'}")
    if not _ok(g):
        return {"ok": False, "error": _err(g), "errors_after": await drain_errors()}
    res = await _measure_immed(ch, kind, source2)
    res["kind"] = kind.upper()
    res["source"] = _ch_token(ch)
    res["source2"] = _ch_token(source2) if source2 else None
    res["gating"] = "ON" if gating else "OFF"
    res["errors_after"] = await drain_errors()
    return res


_SNAPSHOT_KINDS = (
    "PERIOD", "FREQ", "PWIDTH", "NWIDTH", "BURST", "RISE", "FALL",
    "PDUTY", "NDUTY", "POVERSHOOT", "NOVERSHOOT",
    "HIGH", "LOW", "MAXIMUM", "MINIMUM", "AMPLITUDE", "PK2PK",
    "MEAN", "CMEAN", "RMS", "CRMS", "AREA", "CAREA",
)


@mcp.tool()
async def measure_snapshot(ch: int, gating: bool = False) -> dict:
    """All standard immediate measurements for one channel.

    On the TDS784A, `MEASUREMENT:SNAPSHOT` only triggers the on-screen
    snapshot panel; there's no SCPI query that returns every value at
    once. We fan out individual MEASUREMENT:IMMED queries for the same
    set the on-screen snapshot displays (~23 round-trips, a few seconds).

    gating: False → MEASUREMENT:GATING OFF (measure full record).
            True  → MEASUREMENT:GATING ON  (measure between vertical cursors).

    Returns {kind: {value, unit}} for each measurement that succeeded."""
    chtok = _ch_token(ch)
    g = await _write(f"MEASUREMENT:GATING {'ON' if gating else 'OFF'}")
    if not _ok(g):
        return {"ok": False, "error": _err(g)}
    # Set the IMMED source first so the on-screen overlay (next line)
    # measures the requested channel.
    src = await _write(f"MEASUREMENT:IMMED:SOURCE1 {chtok}")
    if not _ok(src):
        return {"ok": False, "error": _err(src)}
    # Trigger the on-screen Snapshot overlay so the user can watch the
    # scope's own snapshot while we fan out individual queries below.
    await _write("MEASUREMENT:SNAPSHOT")

    results: dict[str, dict] = {}
    for kind in _SNAPSHOT_KINDS:
        type_meta = await _write(f"MEASUREMENT:IMMED:TYPE {kind}")
        if not _ok(type_meta):
            continue
        val_meta = await _query("MEASUREMENT:IMMED:VALUE?")
        unit_meta = await _query("MEASUREMENT:IMMED:UNITS?")
        if not _ok(val_meta):
            continue
        v = _to_float(_strip_header(val_meta.get("data", "")))
        u = _strip_header(unit_meta.get("data", "")).strip('"') if _ok(unit_meta) else ""
        results[kind] = {"value": v, "unit": u}

    # Dismiss the on-screen overlay now that gathering is done.
    await _write("MEASUREMENT:CLEARSNAPSHOT")

    return {
        "ok": True,
        "source": chtok,
        "gating": "ON" if gating else "OFF",
        "measurements": results,
        "errors_after": await drain_errors(),
    }


_REFLEVEL_METHODS = {"ABSOLUTE", "PERCENT"}


@mcp.tool()
async def set_measurement_ref_levels(
    method: Optional[str] = None,
    high: Optional[float] = None,
    mid: Optional[float] = None,
    low: Optional[float] = None,
    mid2: Optional[float] = None,
    units: Optional[str] = None,
) -> dict:
    """MEASUREMENT:REFLEVEL:METHOD + ABS/PERC HIGH/MID/LOW/MID2.

    method ∈ {ABSOLUTE, PERCENT}. units ∈ {V, %} — selects which group
    (ABS or PERC) the high/mid/low/mid2 are written to. If units is not
    provided, derived from method (ABS→V, PERC→%)."""
    cmds: list[str] = []
    if method is not None:
        m = method.upper()
        if m not in _REFLEVEL_METHODS:
            return {"ok": False, "error": f"method must be one of {sorted(_REFLEVEL_METHODS)}"}
        cmds.append(f"MEASUREMENT:REFLEVEL:METHOD {m}")
        if units is None:
            units = "V" if m == "ABSOLUTE" else "%"
    if any(v is not None for v in (high, mid, low, mid2)):
        if units is None:
            return {"ok": False, "error": "units required when setting levels (V or %)"}
        group = "ABS" if units.upper() in ("V", "VOLT", "VOLTS") else "PERC"
        for name, val in (("HIGH", high), ("MID", mid), ("LOW", low), ("MID2", mid2)):
            if val is not None:
                cmds.append(f"MEASUREMENT:REFLEVEL:{group}:{name} {val:.9g}")
    if not cmds:
        return {"ok": False, "error": "no parameters provided"}
    sent = []
    for cmd in cmds:
        m = await _write(cmd)
        sent.append({"cmd": cmd, "ok": _ok(m), "error": None if _ok(m) else _err(m)})
        if not _ok(m):
            break
    return {
        "ok": all(s["ok"] for s in sent),
        "sent": sent,
        "errors_after": await drain_errors(),
    }


@mcp.tool()
async def measure_with_acq_stats(
    ch: int,
    kind: str,
    mode: str = "AVERAGE",
    count: int = 64,
    source2: Optional[str] = None,
    timeout_s: float = 60.0,
) -> dict:
    """Acquisition-level statistics — fast, single measurement of a smoothed
    or enveloped waveform.

    mode='AVERAGE': averages `count` acquisitions (good for steady signals;
    cancels noise). Returns one value: the central tendency.
    mode='ENVELOPE': captures min/max envelope over `count` acquisitions
    and runs the measurement twice (on min and max). Worst-case excursion.

    Cannot give you stddev or distribution — use measure_with_polled_stats
    for that."""
    m = mode.upper()
    if m not in {"AVERAGE", "ENVELOPE"}:
        return {"ok": False, "error": "mode must be AVERAGE or ENVELOPE"}
    mode_cmd = await _write(f"ACQUIRE:MODE {m}")
    if not _ok(mode_cmd):
        return {"ok": False, "error": _err(mode_cmd)}
    cnt_cmd = "NUMAVG" if m == "AVERAGE" else "NUMENV"
    cnt_meta = await _write(f"ACQUIRE:{cnt_cmd} {count}")
    if not _ok(cnt_meta):
        return {"ok": False, "error": _err(cnt_meta)}

    arm = await _arm_single_sequence(timeout_s)
    if not arm.get("ok"):
        return {"ok": False, "error": "acquisition timed out", "arm": arm,
                "errors_after": await drain_errors()}

    if m == "AVERAGE":
        meas = await _measure_immed(ch, kind, source2)
        return {
            "ok": meas.get("ok", False),
            "value": meas.get("value"),
            "unit": meas.get("unit"),
            "mode": m,
            "count": count,
            "source": _ch_token(ch),
            "source2": _ch_token(source2) if source2 else None,
            "kind": kind.upper(),
            "errors_after": await drain_errors(),
        }
    else:  # ENVELOPE
        # Tek envelope mode: SOURCE selects env edge via SOURCE1 + SOURCE1:ENVELOPE?
        # Simpler approach: report the single MEASUREMENT:IMMED:VALUE? on the
        # envelope record. The proposal acknowledged "runs twice (one min, one max)"
        # but the IMMED block measures one value at a time — return that single
        # measurement and let the caller measure HIGH and LOW separately if they
        # need both extremes.
        meas = await _measure_immed(ch, kind, source2)
        return {
            "ok": meas.get("ok", False),
            "value": meas.get("value"),
            "unit": meas.get("unit"),
            "mode": m,
            "count": count,
            "source": _ch_token(ch),
            "kind": kind.upper(),
            "note": "envelope returns one measurement of the env record; "
                    "call separately with kind=HIGH and kind=LOW for both extremes.",
            "errors_after": await drain_errors(),
        }


@mcp.tool()
async def measure_with_polled_stats(
    ch: int,
    kind: str,
    n: int = 32,
    source2: Optional[str] = None,
    max_wall_s: float = 30.0,
    return_samples: bool = False,
) -> dict:
    """Polled per-acquisition statistics — N round-trips, one fresh trigger
    each. Aggregates client-side: mean, std, min, max, p5, p95.

    Use this (not measure_with_acq_stats) when you care about jitter, drift,
    or distribution of a measurement across triggers."""
    if n < 2:
        return {"ok": False, "error": "n must be >= 2"}
    # Drop into SAMPLE mode so each acquisition is independent (no smoothing).
    await _write("ACQUIRE:MODE SAMPLE")

    samples: list[float] = []
    started = time.monotonic()
    arm_timeout = max(2.0, max_wall_s / max(n, 1))
    for i in range(n):
        if time.monotonic() - started > max_wall_s:
            break
        arm = await _arm_single_sequence(arm_timeout)
        if not arm.get("ok"):
            continue
        meas = await _measure_immed(ch, kind, source2)
        v = meas.get("value")
        if v is not None and v < 9e36:  # 9.91E37 is Tek's NaN
            samples.append(v)

    elapsed = time.monotonic() - started
    if not samples:
        return {
            "ok": False,
            "error": "no valid samples acquired",
            "n": 0,
            "elapsed_s": elapsed,
            "errors_after": await drain_errors(),
        }

    samples_sorted = sorted(samples)
    def _percentile(p):
        if not samples_sorted:
            return None
        k = max(0, min(len(samples_sorted) - 1,
                       int(round((p / 100.0) * (len(samples_sorted) - 1)))))
        return samples_sorted[k]

    out = {
        "ok": True,
        "n": len(samples),
        "kind": kind.upper(),
        "source": _ch_token(ch),
        "mean": statistics.fmean(samples),
        "std": statistics.pstdev(samples) if len(samples) >= 2 else 0.0,
        "min": min(samples),
        "max": max(samples),
        "p5": _percentile(5),
        "p95": _percentile(95),
        "elapsed_s": elapsed,
        "errors_after": await drain_errors(),
    }
    if return_samples:
        out["samples"] = samples
    return out


# ----------------------------------------------------------- 6. Waveform


@mcp.tool()
async def get_waveform(
    channels: list[str],
    start_idx: Optional[int] = None,
    end_idx: Optional[int] = None,
    width: int = 2,
    chunk_bytes: int = 32768,
    timeout_s: float = 30.0,
) -> dict:
    """Atomic multi-channel single-shot capture. Arms ONE acquisition with
    STOPAFTER SEQUENCE, then reads each requested channel from that record.
    All channels share the same trigger event.

    width=2 keeps full 16-bit fidelity (vs CLI default of 1).
    Windowing: start_idx / end_idx are 1-based inclusive into the record;
    omit both for the default 5000-sample window."""
    if width not in (1, 2):
        return {"ok": False, "error": "width must be 1 or 2"}
    if not channels:
        return {"ok": False, "error": "channels list is required"}

    chans = [_ch_token(c) for c in channels]
    capture_time = datetime.now()

    # WFMPRE? returns abbreviated keys (XIN/YMU/...) when VERBOSE is OFF,
    # which request_gpib.parse_wfmpre doesn't recognize. Force VERBOSE ON
    # so the preamble parses and we can convert raw codes -> volts.
    await _write("VERBOSE ON")

    # Arm one acquisition that all channels will read from.
    arm = await _arm_single_sequence(timeout_s)
    if not arm.get("ok"):
        return {
            "ok": False,
            "error": "acquisition timed out before reading channels",
            "arm": arm,
            "errors_after": await drain_errors(),
        }

    args = SimpleNamespace(
        addr=client.addr,
        timeout=client.timeout_ms,
        width=width,
        points=5000,
        start_index=start_idx,
        end_index=end_idx,
        chunk_bytes=chunk_bytes,
    )

    metadata = await client.with_ws(collect_metadata, args, chans, capture_time)

    out_channels: dict[str, dict] = {}
    samples_per_channel = None
    for ch in chans:
        cap = await client.with_ws(capture_channel, ch, args, True)
        if cap is None:
            return {
                "ok": False,
                "error": f"capture failed for {ch}",
                "errors_after": await drain_errors(),
            }
        pre = cap.get("preamble") or {}
        # Convert raw codes → volts using the preamble's YMULT/YOFF/YZERO.
        if pre:
            ymult = pre["YMULT"]
            yoff = pre["YOFF"]
            yzero = pre["YZERO"]
            xincr = pre["XINCR"]
            xzero = pre["XZERO"]
            pt_off = pre["PT_OFF"]
            voltages = [(s - yoff) * ymult + yzero for s in cap["samples"]]
            times = [(i + 1 - pt_off) * xincr + xzero for i in range(len(voltages))]
        else:
            voltages = []
            times = []
        out_channels[ch] = {
            "voltage_v": voltages,
            "time_s": times,
            "preamble": pre,
            "raw_samples": cap["samples"] if not pre else None,
            "start_idx": cap["start_idx"],
            "end_idx": cap["end_idx"],
        }
        samples_per_channel = len(cap["samples"])

    return {
        "ok": True,
        "metadata": metadata,
        "channels": out_channels,
        "start_idx": args.start_index or 1,
        "end_idx": args.end_index or args.points,
        "samples_per_channel": samples_per_channel,
        "errors_after": await drain_errors(),
    }


# ------------------------------------------------------------ 7. Screen


def _pcx_bytes_to_png_bytes(pcx_bytes: bytes) -> bytes:
    """In-memory PCX → PNG conversion (Pillow). Mirrors pcx_bytes_to_png
    from request_gpib but returns bytes instead of writing a file."""
    from PIL import Image
    img = Image.open(io.BytesIO(pcx_bytes))
    img.load()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@mcp.tool()
async def get_screen(
    layout: str = "PORTRAIT",
    palette: str = "HARDCOPY",
) -> list:
    """Capture the scope screen as PNG. Returns an inline image content
    block (so Claude can see it) plus a JSON metadata block.

    Internally: HARDCOPY:FORMAT PCXCOLOR → fetch → decode to PNG.
    Note: TDS784A v4.1e only accepts HARDCOPY as a PALETTE value;
    other names (COLOR / MONOCHROME / INKSAVER) error with 102."""
    if layout.upper() not in {"PORTRAIT", "LANDSCAPE"}:
        return [TextContent(type="text",
                            text=json.dumps({"ok": False,
                                             "error": "layout must be PORTRAIT or LANDSCAPE"}))]
    if palette.upper() != "HARDCOPY":
        return [TextContent(type="text",
                            text=json.dumps({"ok": False,
                                             "error": "palette must be HARDCOPY (only value the TDS784A accepts)"}))]
    setup_cmds = [
        "HARDCOPY:PORT GPIB",
        "HARDCOPY:FORMAT PCXCOLOR",
        f"HARDCOPY:LAYOUT {layout.upper()}",
        f"HARDCOPY:PALETTE {palette.upper()}",
    ]
    for cmd in setup_cmds:
        m = await _write(cmd)
        if not _ok(m):
            return [TextContent(type="text",
                                text=json.dumps({"ok": False,
                                                 "error": f"setup {cmd!r}: {_err(m)}"}))]

    start_meta = await _write("HARDCOPY START")
    if not _ok(start_meta):
        return [TextContent(type="text",
                            text=json.dumps({"ok": False,
                                             "error": f"HARDCOPY START: {_err(start_meta)}"}))]

    read_timeout_ms = max(client.timeout_ms, 30000)
    meta, payload = await client.request("binary_read", "", timeout_ms=read_timeout_ms)
    if not _ok(meta) or not payload:
        return [TextContent(type="text",
                            text=json.dumps({"ok": False,
                                             "error": f"hardcopy read: {_err(meta)}"}))]

    png_bytes = _pcx_bytes_to_png_bytes(payload)
    metadata = {
        "ok": True,
        "format": "PNG",
        "bytes_len": len(png_bytes),
        "source_format": "PCXCOLOR",
        "captured_at": iso_stamp(datetime.now()),
        "errors_after": await drain_errors(),
    }
    return [
        ImageContent(
            type="image",
            data=base64.b64encode(png_bytes).decode("ascii"),
            mimeType="image/png",
        ),
        TextContent(type="text", text=json.dumps(metadata)),
    ]
