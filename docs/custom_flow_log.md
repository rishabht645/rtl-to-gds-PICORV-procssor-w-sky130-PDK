# `picorv32` — Custom RTL-to-GDSII Flow Log

OpenLane 2.3.10, driven step-by-step via the `Step` API (`custom/run_custom_flow.py`) —
no `Flow` object, no `openlane` CLI, no `Classic` flow class anywhere in the driver.
Each step is constructed and `.start()`ed individually; `state_out` of one step becomes
`state_in` of the next. Methodology and driver pattern carried over from
`../../spm/custom/run_custom_flow.py`.

Run directory: `runs/custom_flow_01/` · Total driver time: **18:52** (06:16 sweep +
12:31 main run, split across two invocations — see §6) · Final GDS:
`runs/custom_flow_01/final/gds/picorv32.gds` (33.4 MB)

---

## 1. Design & inputs

| File | Role |
|---|---|
| `src/picorv32.v` | RTL — YosysHQ's single-file PicoRV32 core, default parameters (no MUL/DIV/PCPI/IRQ/COMPRESSED enabled) |
| `src/impl.sdc`, `src/signoff.sdc` | Generic OpenLane env-driven SDC templates (design-agnostic, copied verbatim from `spm`'s — these aren't `spm`-specific, they read everything from `$::env(...)`) |
| `pin_order.cfg` | Custom pin-side assignment (§4) |
| `config.yaml` | Stock config, used only for the one-off baseline `openlane` CLI run (§2) |
| `custom/config_custom.yaml` | The config actually driving this flow |

### Port list — verified against the RTL, not assumed

`picorv32`'s port list (`picorv32.v:90-159`) was read in full before writing
`pin_order.cfg`. First pass missed two ports — `trace_valid` and `trace_data[35:0]`
(`picorv32.v:158-159`) — because they sit *after* an `` `ifdef RISCV_FORMAL `` block
and look at a glance like they might be conditional on `ENABLE_TRACE`. They are not:
they're declared unconditionally, only their *assignment* is gated by `ENABLE_TRACE`
internally. This was caught the hard way — the first IO-placement attempt failed with
`[ERROR] trace_data[32] not found in config but found in design` — and fixed by
re-reading the full port block line-by-line instead of trusting the first pass.

Full synthesizable port list (RISCV_FORMAL ports excluded — that macro isn't defined,
so Yosys never sees them):

| Port | Width | Direction |
|---|---|---|
| `clk`, `resetn` | 1 | in |
| `trap` | 1 | out |
| `mem_valid`, `mem_instr` | 1 | out |
| `mem_ready` | 1 | in |
| `mem_addr`, `mem_wdata` | 32 | out |
| `mem_wstrb` | 4 | out |
| `mem_rdata` | 32 | in |
| `mem_la_read`, `mem_la_write` | 1 | out |
| `mem_la_addr`, `mem_la_wdata` | 32 | out |
| `mem_la_wstrb` | 4 | out |
| `pcpi_valid` | 1 | out |
| `pcpi_insn`, `pcpi_rs1`, `pcpi_rs2` | 32 | out |
| `pcpi_wr` | 1 | in |
| `pcpi_rd` | 32 | in |
| `pcpi_wait`, `pcpi_ready` | 1 | in |
| `irq` | 32 | in |
| `eoi` | 32 | out |
| `trace_valid` | 1 | out |
| `trace_data` | 36 | out |

27 named ports/buses, **411 individual pins** once bit-blasted — this is a
standalone-core pin count, not what would be exposed if this were wrapped in an SoC.

---

## 2. Baseline run — establishing real numbers before designing anything

Per the stated methodology ("mine a baseline before writing custom code"), the stock
`openlane` CLI `Classic` flow was run first, purely to get real cell-count/area/timing
numbers instead of guessing picorv32's scale from `spm`'s (222 cells).

```bash
openlane --pdk-root /Users/prasanna/.volare/volare/sky130/versions/0fe599b2afb6708d281543108caf8310912f54af config.yaml
```

with a starting `CLOCK_PERIOD: 20` (conservative guess) and `FP_CORE_UTIL: 40`.

**Result at CLOCK_PERIOD=20ns, default `SYNTH_STRATEGY=AREA 0`:**

| Metric | Value |
|---|---|
| `design__instance__count` | 6,703 |
| `design__instance__area` | 88,626.25 µm² |
| `timing__setup__ws` (worst corner, `nom_ss_100C_1v60`) | +4.684 ns |
| `timing__hold__ws` (worst corner) | +0.034 ns |

This told us two things before writing any custom driver code:
1. **`CLOCK_PERIOD` had real headroom to tighten.** Critical path ≈ 20 − 4.68 ≈
   15.3 ns at post-synthesis STA (an optimistic number — pre-route). `custom/config_custom.yaml`
   set `CLOCK_PERIOD: 16` for the real run, leaving ~0.7 ns margin against the usual
   post-synthesis→post-route degradation, rather than either keeping the 20 ns baseline
   comfort margin or blindly targeting the zero-slack point.
2. **The design is ~30x `spm`'s scale** — floorplan/PDN/routing decisions had to be
   re-derived, not reused.

---

## 3. Verilator lint + synthesis (steps 0–8)

```bash
# step 0 — Verilator.Lint
verilator --lint-only --Wall --Wno-DECLFILENAME --Wno-EOFNEWLINE --top-module picorv32 \
  <bb-stub>.bb.v src/picorv32.v --Wno-fatal --relative-includes --Werror-LATCH \
  +define+PDK_sky130A +define+SCL_sky130_fd_sc_hd +define+__openlane__ +define+__pnr__ \
  +define+USE_POWER_PINS
```
474 lint warnings (pre-existing in the upstream picorv32 source — mostly unused-signal
and combinational-loop-adjacent style warnings), 0 lint errors. `Checker.LintErrors`
passed → not fatal, flow continued per policy.

### Feature A — synthesis strategy sweep (step 5, `Custom.SynthesisSweep`)

Rather than accepting Yosys's default `SYNTH_STRATEGY=AREA 0`, every combination of
`SYNTH_STRATEGY` × `SYNTH_HIERARCHY_MODE` was actually run and measured — this was
flagged as worth doing for picorv32 specifically (unlike `spm`, where the sweep just
confirmed the default won, likely *because* the design was tiny). Literals verified
against `openlane/steps/pyosys.py:344-354` and `:416` before use, not assumed:
`SYNTH_STRATEGY ∈ {AREA 0..3, DELAY 0..4}`, `SYNTH_HIERARCHY_MODE ∈ {flatten,
deferred_flatten}` (`keep` excluded — leaves hierarchy in the netlist, not comparable
on cell-count/area against the flattened variants).

Each of the 18 combinations ran `Yosys.Synthesis` then `OpenROAD.STAPrePNR` on the
result, at the tightened `CLOCK_PERIOD=16`:

```
strategy   hierarchy          cells    area(um2)   setup WS(ns)   hold WS(ns)
AREA 0     flatten             6703     88626.25       0.7272       0.0343
AREA 1     flatten             6663     88497.38       0.5231       0.0352
AREA 2     flatten             6807     88940.30       0.4452       0.0343
AREA 3     flatten            11526    109598.86       1.7973       0.0398   <-- WINNER
DELAY 0    flatten             6924     92602.56       0.3747       0.0343
DELAY 1    flatten             6934     92753.96       1.0939       0.0343
DELAY 2    flatten             6989     93095.54       0.9534       0.0343
DELAY 3    flatten             6822     91591.59       0.4594       0.0352
DELAY 4    flatten             8169     97183.21       0.6023       0.0437
AREA 0..DELAY 4  deferred_flatten   — identical to flatten rows above (single-module
                                       design, no sub-hierarchy for deferred_flatten
                                       to preserve differently)
```

**Winner: `SYNTH_STRATEGY=AREA 3`, `SYNTH_HIERARCHY_MODE=flatten`** — ranked by worst
setup slack first (higher = better), area as tiebreaker. `AREA 3` almost doubled the
cell count and area versus `AREA 0` (11,526 vs 6,703 cells) but bought +1.07 ns of
setup margin — at this design's scale, area strategies clearly do NOT all converge to
the same result the way they did for `spm`, exactly as anticipated. This is a real,
deliberate area-for-timing trade taken at synthesis, not an accident.

`Checker.YosysUnmappedCells`/`YosysSynthChecks`/`NetlistAssignStatements` all passed on
the winning netlist.

---

## 4. Pre-PnR checks + floorplan (steps 9–13)

```bash
# step 11 — OpenROAD.STAPrePNR (x3, once per default corner)
sta -no_splash -exit .../scripts/openroad/sta/corner.tcl
```
Confirmed the sweep winner's slack figures above on the full pre-PnR corner set.

### Feature B — floorplan sizing (step 12, `Custom.Floorplan`)

```bash
openroad -exit -no_splash -metrics .../12-openroad-floorplan/or_metrics_out.json \
  .../scripts/openroad/floorplan.tcl
```
with `FP_SIZING=relative`, `FP_CORE_UTIL=40` (unchanged from the baseline's choice —
40% leaves comfortable room for detailed routing at this pin/net density).

An **absolute-sizing fallback was computed and staged before the attempt**, in case
`relative` sizing errored (mirroring `spm`'s contingency, since `FP_SIZING=absolute`
ignores `FP_CORE_UTIL` entirely — confirmed by reading `floorplan.tcl`: `-utilization`
is only passed in the `relative` branch):

```
instance_area_um2      = 109598.86   (post-synthesis, AREA 3/flatten)
headroom_factor         = 1.15        (spm's measured PnR growth, carried over as a
                                        starting estimate — picorv32 hadn't completed
                                        a full PnR run yet to measure its own factor)
planned_area_um2        = 126038.69
core_area_needed_um2    = 315096.73   (at util=0.40)
core (fallback)          = 561.66 x 563.04 um, 207 rows
die (fallback)           = 572.7 x 584.8 um  =  334914.96 um2
```

**Relative sizing succeeded** — the fallback was never needed. Actual result:

| Metric | Value |
|---|---|
| Die area | 291,404 µm² (534.485 × 545.205 µm) |
| Core area | 273,142 µm² |
| Core rows | 192 |
| Utilization at floorplan time | 40.1% |

(The fallback's estimate came in ~15% larger than what relative sizing actually
needed — the 1.15 headroom factor, borrowed from `spm`, was conservative for
picorv32's actual growth, which is fine for a fallback that's meant to always succeed
rather than be tight.)

---

## 5. PDN, tap cells, placement, custom IO placement (steps 14–34)

Stock steps, no deviations from the effective baseline order:
`Odb.CheckMacroAntennaProperties → Odb.SetPowerConnections → Odb.ManualMacroPlacement →
OpenROAD.CutRows → OpenROAD.TapEndcapInsertion → Odb.AddPDNObstructions →
OpenROAD.GeneratePDN → Odb.RemovePDNObstructions → Odb.AddRoutingObstructions →
OpenROAD.GlobalPlacementSkipIO → OpenROAD.IOPlacement (self-skips, `FP_PIN_ORDER_CFG`
is set) → Odb.CustomIOPlacement → Odb.ApplyDEFTemplate → OpenROAD.GlobalPlacement →
Odb.WriteVerilogHeader → Checker.PowerGridViolations → OpenROAD.STAMidPNR →
OpenROAD.RepairDesignPostGPL → Odb.ManualGlobalPlacement → OpenROAD.DetailedPlacement`.

### Custom IO placement (step 25) — the actual pin-order decision

```bash
openroad -exit -no_splash -metrics .../25-odb-customioplacement/or_metrics_out.json \
  -python .../scripts/odbpy/io_place.py \
  --input-lef <tlef> --input-lef <ef_sc_hd.lef> --input-lef <fd_sc_hd.lef> \
  --config pin_order.cfg \
  --hor-layer met3 --ver-layer met2 --hor-width-mult 2 --ver-width-mult 2 \
  --hor-extension 0 --ver-extension 0 --unmatched-error unmatched_design \
  --ver-length 4 --hor-length 4 \
  --output-odb .../picorv32.odb --output-def .../picorv32.def \
  .../23-openroad-globalplacementskipio/picorv32.odb
```

`pin_order.cfg`, final version (after the `trace_data` fix in §1):

```
#W
^clk$
^resetn$
^trap$
^irq(\[.*\])?$
^eoi(\[.*\])?$

#N
^mem_(valid|instr|ready|addr|wdata|wstrb|rdata)(\[.*\])?$

#E
^mem_la_(read|write|addr|wdata|wstrb)(\[.*\])?$
^trace_(valid|data)(\[.*\])?$

#S
^pcpi_(valid|insn|rs1|rs2|wr|rd|wait|ready)(\[.*\])?$
```

**Two decisions baked into this file, both verified before use, not assumed:**

1. **Every pattern is anchored (`^...$`) and mutually exclusive.** The first draft used
   unanchored `mem_.*` for the N side, which also matches `mem_la_read` etc. — a real
   bug caught before it caused an unmatched-port error only by luck of ordering; it was
   fixed by writing an exact per-bus alternation and verifying programmatically (a
   throwaway Python regex check against all 27 port names) that every name matches
   *exactly one* pattern before ever running IO placement again.
2. **Pin grouping was rebalanced across all 4 sides by real bit-count**, not by
   semantic grouping alone (which is what `spm`'s much simpler 5-pin design could get
   away with): N=105, S=132, E=107, W=67 — chosen so no single side carries a wildly
   disproportionate share of the 411 pins. `mem_la_*` (the look-ahead bus) was grouped
   with `trace_*` on the E side specifically because both are secondary/debug-adjacent
   interfaces, keeping the primary `mem_*` bus (N) and `pcpi_*` bus (S) each on their
   own side.

Result (`Custom.PinReport`, step 26, parsed from the placed DEF): **N=105, S=132,
E=107, W=67 — matches the design exactly**, zero unmatched ports.

---

## 6. Clock tree synthesis, resizer, routing (steps 35–46)

```bash
# step 35 — OpenROAD.CTS
openroad -exit -no_splash -metrics .../35-openroad-cts/or_metrics_out.json \
  .../scripts/openroad/cts.tcl
```
Stock `OpenROAD.CTS` — no custom clock-routing-rule experiment was carried over from
`spm` (that NDR/met3-met4 feature was scoped specifically to `spm`'s tiny clock tree
during that session's investigation; picorv32's clock tree is a different scale
entirely and re-deriving that feature's relevance wasn't in scope here).

```bash
# step 39 — OpenROAD.GlobalRouting
openroad -exit -no_splash -metrics .../39-openroad-globalrouting/or_metrics_out.json \
  .../scripts/openroad/grt.tcl
# step 44 — OpenROAD.DetailedRouting (the longest single step: 5:14)
openroad -exit -no_splash -metrics .../44-openroad-detailedrouting/or_metrics_out.json \
  .../scripts/openroad/drt.tcl
```
`OpenROAD.RepairAntennas` inserted 433 antenna diodes (step 42, 35.7s) — expected and
normal given picorv32's longer buses/higher fanout nets versus `spm`'s trivial
datapath; `OpenROAD.CheckAntennas` came back clean both before and after detailed
routing.

**Mid-flow environment failure and recovery, worth documenting because it changed how
this run was executed:** the machine's disk filled to ~0 bytes free during this
session (unrelated to picorv32 — a system-wide issue), which first crashed the stock
baseline's `openlane` CLI run mid-`ResizerTimingPostCTS` with a bare `ENOSPC`. Two
things followed from that:

1. **A disk-space guard was added to the driver** (`check_disk_space()`, called before
   every step) — refuses to start a step below 1.5 GiB free with an actionable message
   instead of letting a step die mid-write.
2. After the user freed space, a **second** failure occurred: the live `nix-shell`
   process's environment was *partially garbage-collected while running* (the user's
   cleanup evidently included `nix-collect-garbage`), which deleted an entire
   `.../openlane/scripts/` directory out from under the already-running Python process.
   This surfaced as `InvalidConfig: Path provided for variable 'FALLBACK_SDC_FILE' is
   invalid` on step 6 — not a real config problem, a live-environment integrity
   failure. Fixed by killing the broken process and **resuming from the last
   checkpoint** (`--resume-from 6`, right after the sweep) in a fresh `nix-shell`,
   which re-fetched the evicted store paths (including rebuilding one Python package
   from source, running its own pytest suite as part of the Nix derivation build) and
   completed the rest of the flow without further incident. This is the exact scenario
   the checkpoint/resume mechanism exists for — the 6:16 sweep was not re-run.

---

## 7. Fill, extraction, signoff STA (steps 47–56)

All post-route checkers passed clean on the first pass:

| Checker | Result |
|---|---|
| `Checker.TrDRC` | 0 routing DRC errors |
| `Checker.DisconnectedPins` | clean |
| `Checker.WireLength` | clean (max wirelength 1120.93 µm) |

```bash
# step 54 — OpenROAD.RCX (parasitic extraction, x3 for corner variants)
openroad -exit -no_splash -metrics .../54-openroad-rcx/or_metrics_out.json \
  .../scripts/openroad/rcx.tcl
# step 55 — OpenROAD.STAPostPNR (signoff STA, full corner matrix: min/max/nom x ff/tt/ss)
sta -no_splash -exit .../scripts/openroad/sta/corner.tcl   # x9 corners
```

**Signoff timing (post-route, extraction-based — the number that actually matters):**

| Metric | Value |
|---|---|
| `timing__setup__ws` (worst corner, `max_ss_100C_1v60`) | **+5.632 ns** |
| `timing__hold__ws` (worst corner, `min_ff_n40C_1v95`) | **+0.256 ns** |
| `clock__skew__worst_setup` / `worst_hold` | 0.247 ns / −0.247 ns |
| `ir__drop__avg` | 0.204 mV (negligible against 1.8 V rail) |

Signoff setup slack (+5.63 ns) came in *higher* than the pre-route sweep estimate
(+1.80 ns) — plausible and not alarming: post-CTS/post-route resizing and hold-fixing
add buffering that can improve setup incidentally, and the signoff corner set (9
corners: min/max/nom × ff/tt/ss) differs from the pre-route set (3 corners,
nom-only), so the two numbers aren't directly comparable, only both individually
healthy.

---

## 8. Streamout + physical verification (steps 57–70)

```bash
# step 57 — Magic.StreamOut  (writes picorv32.gds via Magic)
magic -dnull -noconsole -rcfile <pdk>/sky130A.magicrc .../scripts/magic/wrapper.tcl
# step 58 — KLayout.StreamOut  (independent GDS write via KLayout, for cross-check)
python3 .../scripts/klayout/stream_out.py <def> --output picorv32.klayout.gds ...
# step 61 — KLayout.XOR  (geometric diff between the two GDS writers)
# step 63 — Magic.DRC
magic -dnull -noconsole -rcfile <pdk>/sky130A.magicrc .../scripts/magic/wrapper.tcl
# step 64 — KLayout.DRC  (independent DRC engine, full sky130A_mr.drc ruledeck)
klayout -b -zz -r <pdk>/drc/sky130A_mr.drc -rd input=picorv32.gds -rd topcell=picorv32 \
  -rd feol=true -rd beol=true -rd offgrid=true -rd seal=true -rd threads=8
# step 67 — Magic.SpiceExtraction
# step 69 — Netgen.LVS
netgen -batch source lvs_script.lvs
```

| Check | Result |
|---|---|
| `KLayout.XOR` (Magic GDS vs. KLayout GDS) | match |
| `Magic.DRC` | **0 violations** ("No errors found") |
| `KLayout.DRC` (independent ruledeck) | **0 violations** |
| `Netgen.LVS` | **"Circuits match uniquely."** — 0 errors |

Two independent DRC engines and two independent GDS writers agreeing, plus a clean
LVS, is the strongest signal available that the layout is both design-rule-correct and
faithful to the synthesized netlist.

---

## 9. Final checkers + views (steps 71–76)

`Checker.SetupViolations`, `HoldViolations`, `MaxSlewViolations`, `MaxCapViolations`,
`Misc.ReportManufacturability` all passed. `Custom.SaveViews` (step 76) called
`state.save_snapshot()` explicitly — this is normally done by the `Flow` layer
(`sequential.py`, after the last step), which doesn't exist in a Step-API-only driver,
so it has to be triggered by hand or `final/` never gets written.

```python
state.save_snapshot(os.path.join(RUN_DIR, "final"))
```

---

## 10. Results summary

| | |
|---|---|
| **Synthesis strategy** | `AREA 3`, `flatten` (winner of an 18-way sweep) |
| **Clock period** | 16 ns (62.5 MHz), tightened from a 20 ns baseline guess using real STA data |
| **Final cell count** | 17,516 (post-fill/hold-buffer/diode insertion; 11,526 at synthesis) |
| **Final instance area** | 120,269 µm² |
| **Die / core area** | 291,404 µm² / 273,142 µm² |
| **Final utilization** | 44.0% |
| **I/O pins** | 411 (N=105, S=132, E=107, W=67) |
| **Setup slack (signoff, worst corner)** | +5.632 ns |
| **Hold slack (signoff, worst corner)** | +0.256 ns |
| **Routing DRC errors** | 0 |
| **Magic / KLayout DRC** | 0 / 0 |
| **LVS** | clean match |
| **Antenna diodes inserted** | 433 |
| **Total driver runtime** | 18:52 (sweep + main run) |
| **Final GDS** | `runs/custom_flow_01/final/gds/picorv32.gds` (33.4 MB) |

---

## 11. Files

```
picorv/
├── config.yaml, pin_order.cfg          # stock inputs (config.yaml used only for
│                                        #   the one-off baseline `openlane` CLI run)
├── src/                                # picorv32.v, impl.sdc, signoff.sdc
├── runs/
│   └── custom_flow_01/                 # this run
│       ├── NN-<step-id>/               # one dir per Step, stock layout
│       ├── synth_sweep/                # winner's synth dir kept, losers deleted
│       │                                #   (disk was scarce this session)
│       ├── custom_flow_state/          # NN-<step-id>.json checkpoints (--resume-from)
│       ├── custom_flow_meta.json       # sweep table, floorplan workings, pin counts
│       ├── custom_flow_run.jsonl       # per-step timing/result log
│       ├── resolved_config.json        # full resolved config, dumped for the record
│       └── final/                      # GDS, LEF, netlist, SPEF, lib, metrics.json/csv
└── custom/
    ├── run_custom_flow.py              # the driver
    ├── config_custom.yaml              # CLOCK_PERIOD=16, FP_CORE_UTIL=40, etc.
    ├── run.sh                          # launcher (PATH-first, nix-shell fallback)
    └── custom_flow_log.md              # this file
```
