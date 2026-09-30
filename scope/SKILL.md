---
name: scope
description: Drive the Tektronix TDS784A oscilloscope through the AR-488-ESP32 GPIB gateway via the `tek-tds784a` MCP server. Use when the user mentions TDS784, oscilloscope, scope measurements, waveform capture, or screen hardcopy.
---

# scope — TDS784A control via MCP

The `tek-tds784a` MCP server exposes 29 tools across 7 groups. SI units everywhere (V, s, Hz, Ω). All write tools drain `*ESR?`+`ALLEv?` and surface any non-zero result in `errors_after`.

## Tool surface (Phase 1)

**Plumbing**: `raw_scpi`, `verify_instrument_identity`, `get_errors`, `wait_operation_complete`.

**Setup state**: `get_setup_state`, `set_setup_state(confirm=…)`, `save_internal(slot)`, `recall_internal(slot)`, `factory_reset(confirm=…)`. Slots are 1..10.

**Acquisition**: `autoset`, `set_acquisition_mode(SAMPLE|PEAKDETECT|HIRES|ENVELOPE|AVERAGE)`, `set_average_count(2..10000)`, `set_acquisition_state(RUN|STOP)`, `arm_single_and_wait(timeout_s)`.

**V / H / Trigger**: `set_vertical(ch, …)`, `set_horizontal(scale_s, position_pct, record_length)`, `set_trigger_edge(source, level_v, slope, coupling, holdoff_s, mode)`, `get_acquisition_setup(channels=…)`.

**Measurements**: `measure(ch, kind, source2)`, `measure_snapshot(ch)`, `set_measurement_ref_levels(method, high, mid, low, mid2, units)`, `measure_with_acq_stats`, `measure_with_polled_stats`.

**Waveform**: `get_waveform(channels, start_idx, end_idx, width=2)` — atomic single-shot multi-channel (ARMS a new acquisition). `read_frozen_waveform(channels, …)` — non-destructive read of the CURRENT record (defaults to full record length; writes CSV to `~/scope_captures/`, returns path + per-channel summaries; inlines arrays only ≤1000 pts). `arm_single()` / `check_triggered()` — non-blocking arm + poll pair for event-detection loops where actions must be interleaved while the scope waits.

**Screen**: `get_screen(layout, palette)` — returns inline PNG + JSON metadata.

## Acquisition model — READ THIS before setting timebase/record length

The TDS784A stores a fixed **50 points per division**, and records longer
than 500 points span MORE than the 10 visible screen divisions (the Record
Length menu literally reads "5000 points in 100divs", "15000 points in
300divs", "50000 points in 1000divs"). Consequences:

- **Sample interval = (s/div) / 50.** NOT `(10 × s/div) / record_length` —
  that naive formula is wrong by ×10 at 5000 pts, ×30 at 15000 pts.
- To hit a target resolution, set the timebase: `s/div = 50 × dt_wanted`.
  E.g. 40 ns/sample (25 MS/s) → 2 µs/div. Record length then only extends
  the captured DURATION: 15000 pts × 40 ns = 600 µs.
- The screen always shows 500 points (10 div × 50); the rest of the record
  is off-screen (zoom or GPIB readout only). Don't judge a capture by the
  screen — read the record.
- Acquisition mode (SAMPLE vs HIRES) does NOT change this rate rule; both
  follow (s/div)/50 at these settings. Use SAMPLE for logic/UART decoding.
- Always confirm the ACTUAL interval from the returned preamble (`XINCR`)
  or `time_s` — never assume it from the settings you sent.
- `HORIZONTAL:TRIGGER:POSITION <pct>` positions the trigger within the FULL
  record (`PT_OFF` in the preamble), not within the screen.

## Reading waveforms without destroying a frozen acquisition

`get_waveform` ARMS A NEW single-sequence acquisition — it discards whatever
is currently frozen on screen. For non-destructive readout of an already
captured record, use **`read_frozen_waveform(channels=[...])`**: reads
DATA/CURVE only, defaults to the FULL record (queries RECORDLENGTH — no
window-mismatch 531 errors), saves a CSV (`time_s`, `voltage_v_chN`, raw +
metadata) to `~/scope_captures/` and returns its path plus per-channel
summaries; sample arrays are inlined only for small windows (≤1000 pts), so
big records never blow the tool-output size limit. If the acquisition is
still RUNNING, channels are read sequentially (different trigger events) —
STOP first when cross-channel consistency matters.

Fallback / shell alternative (same plumbing, e.g. for scripted experiments):

    <AR-488-ESP32>/.venv/Scripts/python.exe \
      <AR-488-ESP32>/host_software/request_gpib.py <ip> \
      --waveform --source CH1,CH2 --points 15000 --width 2 --name mylabel

Avoid `raw_scpi("CURVE?")` for bulk reads: ASCII replies get truncated by
the MCP transport on long records.

## Event-detection loops (waiting for a rare trigger)

Use the non-blocking pair instead of the blocking `arm_single_and_wait`
when you must interleave other actions (e.g. commanding the device under
test) while the scope waits:

1. `arm_single()` — STOPAFTER SEQUENCE + STATE RUN, returns immediately.
2. …do other things (change DUT state, wait)…
3. `check_triggered()` — poll; `triggered: true` means the sequence
   completed and the record is frozen.
4. `read_frozen_waveform(...)` to fetch it without re-arming.

## SCPI gotchas (v4.1e firmware via AR-488 gateway)

- **SCPI chaining (`;` / `;:`) works for SHORT compounds only.** The arming
  pair `ACQUIRE:STOPAFTER SEQUENCE;:ACQUIRE:STATE RUN` is proven reliable,
  but a long chain (~250 chars, full pulse-trigger setup) fails with header
  error 110 ("unexpected header termination"). Suspected cause: the AR-488
  gateway's input-buffer limit (~128 chars historically), NOT a scope
  limitation — IEEE-488.2 chaining is standard and the scope accepts it.
  Untested consequence: `set_setup_state` replays multi-KB SET? strings in
  one write and may hit the same wall. Until the limit is bench-tested
  (binary-search the failing length; or raise the gateway firmware buffer),
  keep chains under ~100 chars or send one command per call.
- **`SELECT:CHn ON` before reading a channel** — waveform reads of a
  non-displayed channel fail ("capture failed for CHn", error 2241).
- **Trigger coupling DC, not AC, for slow-moving sources** (e.g. an LDR):
  AC coupling boosts fast edges and false-triggers on HF ripple.
- "Data stop > record length" (531) means the requested DATA:START/STOP
  window exceeds the current record — match it to record_length.
- Pulse-width trigger recipe (e.g. catch a DMX break = negative pulse
  100–400 µs on D+): send individually `TRIGGER:MAIN:TYPE PULSE`,
  `TRIGGER:MAIN:PULSE:CLASS WIDTH`, `TRIGGER:MAIN:PULSE:SOURCE CHn`,
  `TRIGGER:MAIN:PULSE:WIDTH:POLARITY NEGATIVE`,
  `TRIGGER:MAIN:PULSE:WIDTH:LOWLIMIT 100E-6`,
  `TRIGGER:MAIN:PULSE:WIDTH:HIGHLIMIT 400E-6`,
  `TRIGGER:MAIN:PULSE:WIDTH:WHEN WITHIN`, `TRIGGER:MAIN:LEVEL <v>`.
  Arm with `ACQUIRE:STOPAFTER SEQUENCE;:ACQUIRE:STATE RUN`, poll
  `ACQUIRE:STATE?` (0 = done). Width triggers fire at the END of the
  qualifying pulse — trigger position ≥50% keeps the whole pulse in-record.

## When to use which stats tool

The TDS784A firmware has **no** measurement-statistics SCPI group. Two alternatives, with different semantics:

- `measure_with_acq_stats(mode='AVERAGE', count=N)` — fast. Sets `ACQUIRE:MODE AVERAGE` with N acquisitions and takes one measurement of the smoothed waveform. **Returns a single value (central tendency). Cannot give stddev or distribution.** Right answer for steady signals where you want a noise-free amplitude/frequency reading.
- `measure_with_polled_stats(n=N)` — slow (N round-trips). Loops `arm_single_and_wait` then reads `MEASUREMENT:IMMED:VALUE?` N times in SAMPLE mode. **Aggregates mean/std/min/max/p5/p95 client-side.** Right answer for jitter, drift, distribution questions.

If the user asks for stddev or "how much does it vary?" — use polled stats. If they say "average" or "filter the noise out" — use acq stats.

## Confirm-required tools

These refuse without `confirm=True`:
- `set_setup_state(setup_string, confirm=True)` — pushes a setup string back. Big blind state change.
- `factory_reset(confirm=True)` — wipes ALL NVRAM setups. Most disruptive.

`*RST` is intentionally not exposed: `set_setup_state` covers "restore a known good", `factory_reset` covers the panic button.

## Atomic capture

`get_waveform(channels=[CH1, CH2])` performs one single-sequence acquisition and reads all channels from that record. CH1 and CH2 are guaranteed to share the same trigger event. Don't try to compose this from `arm_single_and_wait` + per-channel reads — between two trigger events the acquisition can roll.

## Escape hatch

`raw_scpi(command, expect_reply=None, binary=False, timeout_ms=None)` — anything not exposed. `expect_reply=None` autodetects from `?`. Pulse / glitch / runt / logic triggers, MATH/FFT, REF slots, persistence, gating: all `raw_scpi` until Phase 2.

## Connection

Server reads env: `AR488_HOST` (required, set in `.mcp.json`), `AR488_ADDR` (default 1), `AR488_TIMEOUT_MS` (default 2000). One persistent WebSocket per server lifetime — if it dies, restart the MCP server. No reconnect.

## Quick recipes

- **Verify scope reachable**: `verify_instrument_identity` — should return `{vendor: TEKTRONIX, model: TDS 784A, …}`.
- **Save context, experiment, restore**:
  1. `state = get_setup_state()`
  2. mess with vertical/horizontal/trigger
  3. `set_setup_state(state.setup_string, confirm=True)`
- **Clean amplitude reading on a steady sine**: `set_acquisition_mode(AVERAGE)`, `set_average_count(64)`, `measure_with_acq_stats(ch, kind='AMPL')`.
- **Period jitter**: `measure_with_polled_stats(ch, kind='PERIOD', n=64)` — read `std`.
- **See the screen**: `get_screen()` — Claude renders the PNG inline.
