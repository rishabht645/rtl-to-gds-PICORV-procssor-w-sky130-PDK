#!/usr/bin/env python3
"""
Custom step-by-step RTL-to-GDSII driver for the `picorv32` design (OpenLane 2.3.10).

Drives OpenLane at the *Step* level -- each step is constructed and .start()ed
individually and the state_out of one is handed to the next as state_in. There
is no Flow object anywhere in this file; nothing calls Classic.

Modeled directly on ../../spm/custom/run_custom_flow.py (same Step-API driver
pattern, same checkpoint/resume mechanism). What it adds on top of the stock
sequence, scoped to what picorv32 actually needs (no NDR/clock-routing
experiment -- that was an spm-specific investigation, not reused here):

  A. a Yosys synthesis strategy sweep, with the winner carried into floorplan
  B. a floorplan sized from FP_CORE_UTIL=40, relative sizing with an absolute
     fallback computed from the actual post-synthesis instance area
  C. a disk-space guard before every step -- this machine's disk filled up
     mid-run once already (ENOSPC crash during CTS/ResizerTimingPostCTS on
     the baseline flow); halting cleanly with a resumable checkpoint beats a
     step dying mid-write

After every step it dumps the state to custom_flow_state/<NN>-<step-id>.json
and appends a record to custom_flow_run.jsonl, so any step can be re-entered
with --resume-from N.

Usage (must run inside the OpenLane nix-shell -- see run.sh):
    python3 run_custom_flow.py                     # full run
    python3 run_custom_flow.py --resume-from 34    # re-enter at index 34
    python3 run_custom_flow.py --list              # show the step plan
    python3 run_custom_flow.py --sweep-only        # just the synthesis sweep
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import traceback

from openlane.common import Toolbox
from openlane.config import Config, universal_flow_config_variables
from openlane.state import DesignFormat, State
from openlane.steps import Step

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CUSTOM_DIR = os.path.dirname(os.path.abspath(__file__))
DESIGN_DIR = os.path.dirname(CUSTOM_DIR)
CONFIG_PATH = os.path.join(CUSTOM_DIR, "config_custom.yaml")

RUN_TAG = os.environ.get("CUSTOM_RUN_TAG", "custom_flow_01")
RUN_DIR = os.path.join(DESIGN_DIR, "runs", RUN_TAG)
STATE_DIR = os.path.join(RUN_DIR, "custom_flow_state")
SWEEP_DIR = os.path.join(RUN_DIR, "synth_sweep")
JSONL_LOG = os.path.join(RUN_DIR, "custom_flow_run.jsonl")
META_PATH = os.path.join(RUN_DIR, "custom_flow_meta.json")

DEFAULT_PDK_ROOT = (
    "/Users/prasanna/.volare/volare/sky130/versions/"
    "0fe599b2afb6708d281543108caf8310912f54af"
)

# Minimum free bytes on the filesystem holding RUN_DIR before starting a step.
# The disk on this machine has been observed to run to ~0 bytes free mid-flow;
# 1.5 GiB gives routing/RCX/streamout room to write without a bare ENOSPC
# crash. Override with CUSTOM_MIN_FREE_BYTES if the real headroom differs.
MIN_FREE_BYTES = int(os.environ.get("CUSTOM_MIN_FREE_BYTES", 1_500_000_000))

# ---------------------------------------------------------------------------
# Feature A: synthesis sweep grid.
# SYNTH_STRATEGY literals verified in pyosys.py:344-354 on this machine's
# openlane2 checkout. SYNTH_HIERARCHY_MODE in pyosys.py:416.
# ---------------------------------------------------------------------------
SYNTH_STRATEGIES = [
    "AREA 0", "AREA 1", "AREA 2", "AREA 3",
    "DELAY 0", "DELAY 1", "DELAY 2", "DELAY 3", "DELAY 4",
]
SYNTH_HIER_MODES = ["flatten", "deferred_flatten"]


# ===========================================================================
# Step plan
# ===========================================================================
def build_step_plan():
    """
    Mirrors the *effective* order the baseline `openlane` Classic run actually
    executed (confirmed twice against runs/RUN_*/flow.log on this design
    before this driver was written). Steps Classic gates off by default
    (OpenROAD.RepairDesignPostGRT, Odb.HeuristicDiodeInsertion,
    OpenROAD.ResizerTimingPostGRT, Yosys.EQY) are omitted rather than
    included, for the same reason spm's driver omits them: driving steps
    directly has no gating layer, so including them would silently add work
    the baseline never did.
    """
    P = []

    def step(step_id, **overrides):
        P.append({"kind": "step", "id": step_id, "overrides": overrides})

    def custom(name, fn, **kw):
        P.append({"kind": "custom", "id": name, "fn": fn, "kw": kw})

    # --- lint + synthesis ---------------------------------------------------
    step("Verilator.Lint")
    step("Checker.LintTimingConstructs")
    step("Checker.LintErrors")
    step("Checker.LintWarnings")
    step("Yosys.JsonHeader")
    custom("Custom.SynthesisSweep", do_synthesis_sweep)   # feature A
    step("Checker.YosysUnmappedCells")
    step("Checker.YosysSynthChecks")
    step("Checker.NetlistAssignStatements")

    # --- pre-PnR checks -----------------------------------------------------
    step("OpenROAD.CheckSDCFiles")
    step("OpenROAD.CheckMacroInstances")
    step("OpenROAD.STAPrePNR")

    # --- floorplan (feature B) ----------------------------------------------
    custom("Custom.Floorplan", do_floorplan)
    custom("Custom.FloorplanSummary", do_floorplan_summary)

    # --- macro/PDN/tap --------------------------------------------------------
    step("Odb.CheckMacroAntennaProperties")
    step("Odb.SetPowerConnections")
    step("Odb.ManualMacroPlacement")
    step("OpenROAD.CutRows")
    step("OpenROAD.TapEndcapInsertion")
    step("Odb.AddPDNObstructions")
    step("OpenROAD.GeneratePDN")
    step("Odb.RemovePDNObstructions")
    step("Odb.AddRoutingObstructions")

    # --- placement + IO -------------------------------------------------------
    step("OpenROAD.GlobalPlacementSkipIO")
    step("OpenROAD.IOPlacement")        # self-skips: FP_PIN_ORDER_CFG is set
    step("Odb.CustomIOPlacement")       # our pin_order.cfg
    custom("Custom.PinReport", do_pin_report)
    step("Odb.ApplyDEFTemplate")
    step("OpenROAD.GlobalPlacement")
    step("Odb.WriteVerilogHeader")
    step("Checker.PowerGridViolations")
    step("OpenROAD.STAMidPNR")
    step("OpenROAD.RepairDesignPostGPL")
    step("Odb.ManualGlobalPlacement")
    step("OpenROAD.DetailedPlacement")

    # --- CTS ------------------------------------------------------------------
    step("OpenROAD.CTS")
    step("OpenROAD.STAMidPNR")
    step("OpenROAD.ResizerTimingPostCTS")
    step("OpenROAD.STAMidPNR")

    # --- routing ----------------------------------------------------------------
    step("OpenROAD.GlobalRouting")
    step("OpenROAD.CheckAntennas")
    step("Odb.DiodesOnPorts")
    step("OpenROAD.RepairAntennas")
    step("OpenROAD.STAMidPNR")
    step("OpenROAD.DetailedRouting")
    step("Odb.RemoveRoutingObstructions")
    step("OpenROAD.CheckAntennas")

    step("Checker.TrDRC")
    step("Odb.ReportDisconnectedPins")
    step("Checker.DisconnectedPins")
    step("Odb.ReportWireLength")
    step("Checker.WireLength")

    # --- fill, extraction, signoff STA ------------------------------------------
    step("OpenROAD.FillInsertion")
    step("Odb.CellFrequencyTables")
    step("OpenROAD.RCX")
    step("OpenROAD.STAPostPNR")
    step("OpenROAD.IRDropReport")

    # --- streamout + physical verification ---------------------------------------
    step("Magic.StreamOut")
    step("KLayout.StreamOut")
    step("Magic.WriteLEF")
    step("Odb.CheckDesignAntennaProperties")
    step("KLayout.XOR")
    step("Checker.XOR")
    step("Magic.DRC")
    step("KLayout.DRC")
    step("Checker.MagicDRC")
    step("Checker.KLayoutDRC")
    step("Magic.SpiceExtraction")
    step("Checker.IllegalOverlap")
    step("Netgen.LVS")
    step("Checker.LVS")

    # --- final timing checkers -----------------------------------------------------
    step("Checker.SetupViolations")
    step("Checker.HoldViolations")
    step("Checker.MaxSlewViolations")
    step("Checker.MaxCapViolations")
    step("Misc.ReportManufacturability")

    custom("Custom.SaveViews", do_save_views)

    return P


# ===========================================================================
# Context
# ===========================================================================
class Ctx:
    def __init__(self, config, toolbox, args):
        self.config = config
        self.toolbox = toolbox
        self.args = args
        self.meta = {}
        self.index = 0

    def load_meta(self):
        if os.path.exists(META_PATH):
            with open(META_PATH) as f:
                self.meta = json.load(f)

    def save_meta(self):
        with open(META_PATH, "w") as f:
            json.dump(self.meta, f, indent=2, default=str)

    def step_dir(self, index, step_id):
        return os.path.join(RUN_DIR, f"{index:02d}-{slug(step_id)}")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def fmt_dur(sec):
    return f"{int(sec // 60):02d}:{sec % 60:06.3f}"


def read_commands_file(step_dir):
    p = os.path.join(step_dir, "COMMANDS")
    if os.path.isfile(p):
        return open(p).read().strip()
    return ""


def free_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def check_disk_space(step_id):
    """
    Raise a clear, actionable error BEFORE a step runs if free space is below
    MIN_FREE_BYTES, instead of letting the step crash mid-write with a bare
    ENOSPC (which is what happened to the stock `openlane` CLI baseline run
    on this machine during OpenROAD.ResizerTimingPostCTS).
    """
    free = free_bytes(RUN_DIR)
    if free < MIN_FREE_BYTES:
        raise RuntimeError(
            f"Only {free / 1e9:.3f} GiB free on the filesystem holding "
            f"{RUN_DIR!r} (need >= {MIN_FREE_BYTES / 1e9:.3f} GiB before "
            f"'{step_id}'). Free up disk space, then resume with "
            f"--resume-from N (see --list for the index of '{step_id}')."
        )


# ===========================================================================
# Feature A -- synthesis strategy sweep
# ===========================================================================
def do_synthesis_sweep(ctx, state, **_):
    Synthesis = Step.factory.get("Yosys.Synthesis")
    STAPrePNR = Step.factory.get("OpenROAD.STAPrePNR")

    os.makedirs(SWEEP_DIR, exist_ok=True)
    results = []

    combos = [(s, h) for h in SYNTH_HIER_MODES for s in SYNTH_STRATEGIES]
    print(f"\n=== Feature A: synthesis sweep over {len(combos)} configurations ===\n")

    for i, (strategy, hier) in enumerate(combos):
        check_disk_space(f"Custom.SynthesisSweep[{strategy}/{hier}]")
        tag = f"{slug(strategy)}-{hier}"
        row = {"strategy": strategy, "hierarchy": hier}
        try:
            syn_dir = os.path.join(SWEEP_DIR, f"{i:02d}-{tag}-synth")
            syn = Synthesis(
                config=ctx.config,
                state_in=state,
                SYNTH_STRATEGY=strategy,
                SYNTH_HIERARCHY_MODE=hier,
                _config_quiet=True,
            )
            t0 = time.time()
            syn_state = syn.start(toolbox=ctx.toolbox, step_dir=syn_dir)
            row["synth_runtime_s"] = round(time.time() - t0, 3)

            m = syn_state.metrics
            row["cells"] = int(m.get("design__instance__count", 0))
            row["area"] = float(m.get("design__instance__area", 0) or 0)

            sta_dir = os.path.join(SWEEP_DIR, f"{i:02d}-{tag}-sta")
            sta = STAPrePNR(config=ctx.config, state_in=syn_state, _config_quiet=True)
            sta_state = sta.start(toolbox=ctx.toolbox, step_dir=sta_dir)

            sm = sta_state.metrics
            ws = sm.get("timing__setup__ws")
            hs = sm.get("timing__hold__ws")
            row["setup_ws"] = float(ws) if ws is not None else None
            row["hold_ws"] = float(hs) if hs is not None else None
            row["state_path"] = os.path.join(syn_dir, "state_out.json")
            row["ok"] = True

            # Delete the (large) STA sub-run once its metrics are captured;
            # only the synthesis netlist state needs to survive to be carried
            # forward by the winner. Disk is scarce on this machine.
            shutil.rmtree(sta_dir, ignore_errors=True)
        except Exception as e:
            row["ok"] = False
            row["error"] = f"{type(e).__name__}: {e}"
            print(f"  [sweep] {strategy:8s} / {hier:16s} FAILED: {row['error']}")
        results.append(row)
        if row.get("ok"):
            print(
                f"  [sweep] {strategy:8s} / {hier:16s} "
                f"cells={row['cells']:5d} area={row['area']:10.2f} "
                f"setup_ws={row['setup_ws']}"
            )

    good = [r for r in results if r.get("ok") and r.get("setup_ws") is not None]
    if not good:
        raise RuntimeError("synthesis sweep produced no usable result")

    good.sort(key=lambda r: (-r["setup_ws"], r["area"]))
    winner = good[0]
    print_sweep_table(results, winner)

    ctx.meta["sweep"] = results
    ctx.meta["sweep_winner"] = {
        "strategy": winner["strategy"],
        "hierarchy": winner["hierarchy"],
        "cells": winner["cells"],
        "area": winner["area"],
        "setup_ws": winner["setup_ws"],
        "hold_ws": winner["hold_ws"],
    }
    ctx.save_meta()

    with open(winner["state_path"]) as f:
        winner_state = State.loads(f.read())

    # Remove every losing sweep sub-run's directory -- keep only the winner's
    # synth dir (already referenced by winner["state_path"]) to save space.
    winner_dir = os.path.dirname(winner["state_path"])
    for entry in os.listdir(SWEEP_DIR):
        p = os.path.join(SWEEP_DIR, entry)
        if p != winner_dir and os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)

    return winner_state


def print_sweep_table(results, winner):
    print("\n" + "=" * 96)
    print("SYNTHESIS STRATEGY SWEEP")
    print("=" * 96)
    hdr = f"{'strategy':10s} {'hierarchy':17s} {'cells':>7s} {'area(um2)':>12s} {'setup WS(ns)':>13s} {'hold WS(ns)':>12s}"
    print(hdr)
    print("-" * 96)
    for r in results:
        if not r.get("ok"):
            print(f"{r['strategy']:10s} {r['hierarchy']:17s}  FAILED: {r.get('error','')}")
            continue
        mark = "  <-- WINNER" if r is winner else ""
        print(
            f"{r['strategy']:10s} {r['hierarchy']:17s} {r['cells']:7d} "
            f"{r['area']:12.2f} "
            f"{(r['setup_ws'] if r['setup_ws'] is not None else float('nan')):13.4f} "
            f"{(r['hold_ws'] if r['hold_ws'] is not None else float('nan')):12.4f}{mark}"
        )
    print("-" * 96)
    print(
        f"WINNER: SYNTH_STRATEGY={winner['strategy']!r} "
        f"SYNTH_HIERARCHY_MODE={winner['hierarchy']!r}  "
        f"(ranked by worst setup slack, area as tiebreaker)"
    )
    print("=" * 96 + "\n")


# ===========================================================================
# Feature B -- floorplan with relative sizing, absolute fallback
# ===========================================================================
def compute_die_area(instance_area_um2, util=0.40):
    """
    Absolute-sizing fallback geometry, same method spm's driver used (site
    unithd 0.46 x 2.72um, LEFT/RIGHT_MARGIN_MULT=12 sites,
    BOTTOM/TOP_MARGIN_MULT=4 sites -- floorplan.tcl:36-39,58-72). The 1.15
    headroom factor is spm's measured PnR growth (post-PnR / post-synth area
    = 1.1143, rounded up); picorv32 hasn't completed a full PnR run yet to
    measure its own factor, so this is carried over as a starting estimate,
    not a verified picorv32 number.
    """
    site_w, site_h = 0.46, 2.72
    lr_margin = 12 * site_w
    bt_margin = 4 * site_h

    headroom = 1.15
    planned = instance_area_um2 * headroom
    core_area_needed = planned / util
    side = core_area_needed ** 0.5

    core_w = round((side / site_w) + 0.5) * site_w
    core_h = round((side / site_h) + 0.5) * site_h
    rows = int(round(core_h / site_h))

    die_w = core_w + 2 * lr_margin
    die_h = core_h + 2 * bt_margin

    w = {
        "instance_area_um2": instance_area_um2,
        "headroom_factor": headroom,
        "planned_area_um2": round(planned, 2),
        "target_util": util,
        "core_area_needed_um2": round(core_area_needed, 2),
        "core_w_um": round(core_w, 4),
        "core_h_um": round(core_h, 4),
        "rows": rows,
        "core_area_um2": round(core_w * core_h, 2),
        "die_w_um": round(die_w, 4),
        "die_h_um": round(die_h, 4),
        "die_area_um2": round(die_w * die_h, 2),
    }
    die = [0, 0, round(die_w, 4), round(die_h, 4)]
    return die, w


def do_floorplan(ctx, state, **_):
    check_disk_space("Custom.Floorplan")
    Floorplan = Step.factory.get("OpenROAD.Floorplan")
    idx = ctx.index
    inst_area = float(state.metrics.get("design__instance__area", 0) or 0)

    die, workings = compute_die_area(inst_area, util=0.40)
    ctx.meta["die_area_workings"] = workings
    ctx.meta["die_area_absolute_fallback"] = die

    print("\n=== Feature B: floorplan sizing ===")
    print(f"  post-synth instance area: {inst_area:.2f} um^2")
    print(f"  primary   : FP_SIZING=relative, FP_CORE_UTIL=40")
    print(f"  fallback  : FP_SIZING=absolute, DIE_AREA={die}")

    try:
        fp = Floorplan(config=ctx.config, state_in=state)
        out = fp.start(toolbox=ctx.toolbox, step_dir=ctx.step_dir(idx, "OpenROAD.Floorplan"))
        ctx.meta["floorplan_mode"] = "relative"
        print("  -> relative sizing succeeded\n")
        return out
    except Exception as e:
        print(f"\n  !! relative sizing FAILED: {type(e).__name__}: {e}")
        print(f"  !! falling back to FP_SIZING=absolute with DIE_AREA={die}\n")
        ctx.meta["floorplan_mode"] = "absolute"
        ctx.meta["floorplan_fallback_reason"] = f"{type(e).__name__}: {e}"
        ctx.save_meta()
        fp = Floorplan(
            config=ctx.config,
            state_in=state,
            FP_SIZING="absolute",
            DIE_AREA=die,
        )
        return fp.start(
            toolbox=ctx.toolbox,
            step_dir=ctx.step_dir(idx, "OpenROAD.Floorplan") + "-absolute",
        )


def do_floorplan_summary(ctx, state, **_):
    m = state.metrics
    die_bbox = m.get("design__die__bbox")
    core_bbox = m.get("design__core__bbox")

    rows = None
    if core_bbox:
        try:
            x0, y0, x1, y1 = [float(v) for v in str(core_bbox).split()]
            rows = int(round((y1 - y0) / 2.72))
        except Exception:
            pass

    summary = {
        "die_bbox_um": str(die_bbox),
        "core_bbox_um": str(core_bbox),
        "die_area_um2": float(m.get("design__die__area", 0) or 0),
        "core_area_um2": float(m.get("design__core__area", 0) or 0),
        "instance_area_um2": float(m.get("design__instance__area", 0) or 0),
        "utilization": float(m.get("design__instance__utilization", 0) or 0),
        "rows": rows,
        "mode": ctx.meta.get("floorplan_mode"),
    }
    ctx.meta["floorplan_summary"] = summary
    ctx.save_meta()

    print("\n" + "=" * 70)
    print("FLOORPLAN SUMMARY")
    print("=" * 70)
    for k, v in summary.items():
        print(f"  {k:22s} : {v}")
    print("=" * 70 + "\n")
    return state


def do_pin_report(ctx, state, **_):
    check_disk_space("Custom.PinReport")
    def_path = state.get(DesignFormat.DEF)
    if def_path is None:
        print("  (no DEF in state; skipping pin report)")
        return state
    rows = parse_pin_sides(str(def_path))
    ctx.meta["pin_assignment_counts"] = {}
    for r in rows:
        ctx.meta["pin_assignment_counts"][r["side"]] = (
            ctx.meta["pin_assignment_counts"].get(r["side"], 0) + 1
        )
    ctx.save_meta()

    counts = ctx.meta["pin_assignment_counts"]
    print("\n" + "=" * 50)
    print(f"PIN -> SIDE ASSIGNMENT: {len(rows)} pins total, {counts}")
    print("=" * 50 + "\n")
    return state


def parse_pin_sides(def_path):
    txt = open(def_path).read()
    dbu = 1000
    m = re.search(r"^UNITS DISTANCE MICRONS (\d+)", txt, re.M)
    if m:
        dbu = int(m.group(1))
    m = re.search(r"^DIEAREA\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)", txt, re.M)
    if not m:
        return []
    dx0, dy0, dx1, dy1 = [int(v) for v in m.groups()]

    m = re.search(r"^PINS \d+ ;$(.*?)^END PINS", txt, re.M | re.S)
    if not m:
        return []
    body = m.group(1)

    rows = []
    for entry in re.split(r"\n\s*-\s+", body):
        entry = entry.strip()
        if not entry:
            continue
        name = entry.split()[0]
        pm = re.search(r"\+\s*(?:PLACED|FIXED|COVER)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)", entry)
        if not pm:
            continue
        x, y = int(pm.group(1)), int(pm.group(2))
        d = {"W": abs(x - dx0), "E": abs(dx1 - x), "S": abs(y - dy0), "N": abs(dy1 - y)}
        side = min(d, key=d.get)
        rows.append({"pin": name, "side": side, "x": x / dbu, "y": y / dbu})
    return rows


def do_save_views(ctx, state, **_):
    """
    `final/` is produced by the FLOW layer (Flow.start() calling
    state.save_snapshot()), not by any Step -- driving steps directly skips
    it entirely unless called explicitly, same as spm's driver documented.
    """
    final_path = os.path.join(RUN_DIR, "final")
    state.save_snapshot(final_path)

    n = sum(len(files) for _r, _d, files in os.walk(final_path))
    print("\n" + "=" * 70)
    print("FINAL VIEWS")
    print(f"  {final_path}  ({n} files)")
    print("=" * 70 + "\n")

    ctx.meta["final_views"] = final_path
    ctx.save_meta()
    return state


# ===========================================================================
# Driver
# ===========================================================================
def resolve_config(plan):
    by_name = {v.name: v for v in universal_flow_config_variables}
    for entry in plan:
        if entry["kind"] != "step":
            continue
        cls = Step.factory.get(entry["id"])
        for v in cls.config_vars:
            by_name[v.name] = v

    pdk_root = os.environ.get("PDK_ROOT") or DEFAULT_PDK_ROOT
    if not os.path.isdir(os.path.join(pdk_root, "sky130A")):
        raise RuntimeError(
            f"PDK_ROOT={pdk_root!r} does not contain a sky130A directory. "
            "Set the PDK_ROOT environment variable to the directory holding sky130A/."
        )
    config, design_dir = Config.load(
        config_in=CONFIG_PATH,
        flow_config_vars=list(by_name.values()),
        design_dir=DESIGN_DIR,
        pdk_root=pdk_root,
    )
    return config


def checkpoint_path(index, step_id):
    return os.path.join(STATE_DIR, f"{index:02d}-{slug(step_id)}.json")


def append_jsonl(record):
    with open(JSONL_LOG, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resume-from", type=int, default=0, metavar="N")
    ap.add_argument("--stop-after", type=int, default=None, metavar="N")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--sweep-only", action="store_true")
    ap.add_argument("--force-continue", action="store_true")
    args = ap.parse_args()

    plan = build_step_plan()

    if args.list:
        print(f"{'idx':>4}  {'custom':6}  step id")
        print("-" * 60)
        for i, e in enumerate(plan):
            print(f"{i:4d}  {'YES' if e['kind']=='custom' else '   ':6}  {e['id']}")
        return 0

    fresh = args.resume_from == 0
    if fresh and os.path.exists(RUN_DIR):
        print(f"!! {RUN_DIR} already exists.")
        print("!! Refusing to clobber it. Delete it, or set CUSTOM_RUN_TAG to a new tag.")
        return 1

    for d in (RUN_DIR, STATE_DIR, os.path.join(RUN_DIR, "tmp")):
        os.makedirs(d, exist_ok=True)

    print(f"design dir : {DESIGN_DIR}")
    print(f"run dir    : {RUN_DIR}")
    print(f"config     : {CONFIG_PATH}")
    print(f"min free   : {MIN_FREE_BYTES/1e9:.3f} GiB (CUSTOM_MIN_FREE_BYTES)")
    print(f"free now   : {free_bytes(DESIGN_DIR)/1e9:.3f} GiB")

    config = resolve_config(plan)
    print(f"PDK        : {config['PDK']} @ {config['PDK_ROOT']}")
    print(f"SCL        : {config['STD_CELL_LIBRARY']}")
    print(f"CLOCK_PERIOD: {config['CLOCK_PERIOD']} ns")

    with open(os.path.join(RUN_DIR, "resolved_config.json"), "w") as f:
        f.write(json.dumps(config.to_raw_dict(), default=str, indent=2))

    toolbox = Toolbox(os.path.join(RUN_DIR, "tmp"))
    ctx = Ctx(config, toolbox, args)
    ctx.load_meta()

    if args.resume_from > 0:
        prev = args.resume_from - 1
        cp = checkpoint_path(prev, plan[prev]["id"])
        if not os.path.exists(cp):
            print(f"!! no checkpoint for step {prev} at {cp}")
            return 1
        print(f"resuming from checkpoint: {cp}")
        with open(cp) as f:
            state = State.loads(f.read())
    else:
        state = State()

    start = args.resume_from
    end = args.stop_after + 1 if args.stop_after is not None else len(plan)
    if args.sweep_only:
        end = next(i for i, e in enumerate(plan) if e["id"] == "Custom.SynthesisSweep") + 1

    overall_t0 = time.time()
    for i in range(start, end):
        entry = plan[i]
        ctx.index = i
        step_id = entry["id"]
        is_custom = entry["kind"] == "custom"
        sdir = ctx.step_dir(i, step_id)

        try:
            check_disk_space(step_id)
        except RuntimeError as e:
            print(f"\n{'!'*78}\nDISK SPACE GUARD TRIPPED before step {i} ({step_id})\n{'!'*78}")
            print(str(e))
            return 1

        print(f"\n[{i:02d}/{len(plan)-1}] {'CUSTOM ' if is_custom else ''}{step_id}")
        t0 = time.time()
        rec = {"index": i, "step_id": step_id, "custom": is_custom}

        try:
            if is_custom:
                state = entry["fn"](ctx, state, **entry.get("kw", {}))
                rec["command"] = f"(python) {entry['fn'].__name__}()"
            else:
                cls = Step.factory.get(step_id)
                step = cls(config=config, state_in=state, **entry["overrides"])
                state = step.start(toolbox=toolbox, step_dir=sdir)
                rec["command"] = read_commands_file(sdir) or "(no subprocess)"
            rec["result"] = "ok"
        except Exception as e:
            rec["result"] = f"FAILED: {type(e).__name__}: {e}"
            rec["runtime_s"] = round(time.time() - t0, 3)
            append_jsonl(rec)
            ctx.save_meta()
            print(f"\n{'!'*78}\nSTEP {i} ({step_id}) FAILED\n{'!'*78}")
            print(f"{type(e).__name__}: {e}")
            traceback.print_exc()
            report_failure_context(step_id, sdir)
            if step_id.startswith("Checker.") and not args.force_continue:
                print(f"\nCHECKER failure. Resume with: python3 run_custom_flow.py --resume-from {i}")
            return 1

        rec["runtime_s"] = round(time.time() - t0, 3)
        rec["runtime"] = fmt_dur(rec["runtime_s"])

        cp = checkpoint_path(i, step_id)
        with open(cp, "w") as f:
            f.write(state.dumps())
        rec["checkpoint"] = cp
        append_jsonl(rec)
        print(f"      done in {rec['runtime']}  ->  {os.path.relpath(cp, RUN_DIR)}  "
              f"(free: {free_bytes(RUN_DIR)/1e9:.2f} GiB)")

    ctx.save_meta()
    print(f"\nAll steps completed in {fmt_dur(time.time() - overall_t0)}.")
    print(f"Run directory: {RUN_DIR}")
    return 0


def report_failure_context(step_id, step_dir):
    print("\n--- where to look ---")
    if not os.path.isdir(step_dir):
        print(f"  (step dir {step_dir} was never created)")
        return
    print(f"  step dir: {step_dir}")
    for root, _dirs, files in os.walk(step_dir):
        for fn in sorted(files):
            if fn.endswith((".log", ".rpt", ".drc", ".json")) and fn != "config.json":
                print(f"    {os.path.join(root, fn)}")


if __name__ == "__main__":
    sys.exit(main())
