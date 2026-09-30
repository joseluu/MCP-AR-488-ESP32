# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AR-488-ESP32 is a GPIB/IEEE-488 interface PCB for the Tektronix TDS784A oscilloscope. Schematic and PCB layout are edited directly in KiCad 9 with AI assistance via the MCP server. (Earlier revisions of the schematic were generated from Python via circuit-synth; that workflow has been retired.)

The architecture (hybrid GPIO, transceiver choices, power path, MCP23017-at-3.3V rationale) is documented in `README.md`. Read it before changing the schematic — most "why is X like that?" answers are there.

## Repo Layout

- `AR488_ESP32/` — KiCad project (.kicad_sch, .kicad_pro, .kicad_pcb, netlist)
- `AR488_ESP32/libs/` — Custom symbols (`AR488_custom.kicad_sym`), footprints (`AR488_custom.pretty/`), 3D models (`AR488_custom.3dshapes/`)
- `AR488_ESP32/Elecrow_manufacturing_v*/` — Gerber output for fab, named by manufacturing revision
- `firmware/` — ESP32 firmware (in progress)
- `docs/` — Project notes (`a_faire.md` for TODO, `TECHNICAL_INFORMATIONS.md`, board photo)
- `scope/` — The `scope` Claude Code skill, versioned here (see "Oscilloscope MCP Server" below)
- `.mcp.json` — KiCad MCP server config (paths are absolute and machine-specific; do not commit changes that aren't portable for this user)

## First-Time Setup

```bash
uv sync
```

## KiCad MCP Server (PCB editor)

The MCP server (mixelpixx/KiCAD-MCP-Server) allows Claude to read and modify the PCB.

### Backend behavior

- **IPC backend** (read ops): `get_board_info`, `get_component_pads`, `get_nets_list` work in real-time via KiCad's IPC API
- **SWIG backend** (write ops): `route_pad_to_pad`, `move_component`, etc. write to the .kicad_pcb file directly. The user must **File > Revert** in KiCad to see changes.
- Check the `_backend` and `_realtime` fields in MCP responses to know which backend handled the call
- Always call `open_project` with the .kicad_pcb path before using SWIG write operations

### Useful MCP tools

| Tool | Use for |
|------|---------|
| `get_board_info` | Check connection, backend, component/track counts |
| `get_component_pads` | Get pad positions, nets, sizes for a component |
| `get_nets_list` | List all nets with net codes |
| `get_design_rules` | Check track width, clearance, via sizes |
| `route_pad_to_pad` | Route a trace between two component pads |
| `get_component_list` | List all components on the board |
| `run_drc` | Run design rule check |
| `move_component` | Move a component to new coordinates |

### Workaround for unreliable SWIG writes

If `route_pad_to_pad` reports success but the trace doesn't appear after revert, write the segment directly into the .kicad_pcb file:

```python
# Add a (segment ...) block before the last closing paren in the .kicad_pcb
# Use pad positions from get_component_pads, net code from get_nets_list
```

## Oscilloscope MCP Server (tek-tds784a)

A Tektronix TDS784A is reachable through this board's own GPIB gateway. The `tek-tds784a` MCP server (`host_software/mcp_server/`) exposes 25 tools (setup state, acquisition, vertical/horizontal/trigger, measurements, waveform, screen capture) over SCPI/GPIB. It's registered in this repo's `.mcp.json` (env: `AR488_HOST`, `AR488_ADDR`, `AR488_TIMEOUT_MS`) — sessions started in/under this project get the tools automatically; other projects on the same machine don't unless the `.mcp.json` entry is copied over.

See the `scope` skill (versioned in this repo at `scope/SKILL.md`, linked into `~/.claude/skills/scope` — see README "Claude Code integration") for the full tool surface, measurement-stats decision tree, and quick recipes.

## Net Classes

Power nets (`/DC_7-12V`, `/LDO_IN`, `GND`, `+5V`, `+3V3`) are assigned to the **Power** net class in the .kicad_pro file: 0.6mm track width (3x default), 0.3mm clearance, 0.8mm via diameter.

## Versioning

Two distinct version numbers:
- **Schematic rev** — `rev "X.Y"` in the title block, edited directly in KiCad. Bump on each design change and commit.
- **Manufacturing rev** — directory name `AR488_ESP32/Elecrow_manufacturing_vX.Y/` (and matching `.zip`). Bump only when sending a new fab batch.

These can drift (e.g., schematic v0.4 with manufacturing v1.1) — that's expected.

## Custom 3D Models

VRML (.wrl) models are in `AR488_ESP32/libs/AR488_custom.3dshapes/`. Models use meters internally (VRML standard). The footprint `(model ...)` entry needs a scale factor to convert to KiCad's mm. Empirically determined scale: **~394** (not 1000 as expected — likely due to KiCad's internal VRML import scaling). Apply the same scale to all three axes.

Rotation `-90` on X axis is typically needed to align VRML Y-up with KiCad's coordinate system.

## Slash Commands

- `/find-symbol` — Search KiCad symbol libraries
- `/find-footprint` — Search KiCad footprint libraries
