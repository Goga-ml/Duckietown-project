# Yield Sign — Status & Handoff

Short handoff for whoever picks up the yield-sign work. The **behaviour is fully
implemented and tested**; the only open item is **recognition** — the physical
yield sign's AprilTag ID is not in the mapping yet, so the (working) behaviour
never gets triggered on the real bot (glados).

---

## 1. What a yield should do (the spec we built to)

> "Slow down just a little when it sees a yield sign, then continue at normal
> speed once it passes the yield sign."

So: **not** a stop, **not** a right-of-way wait — just ease off the throttle
while the sign is in range, then return to cruise once we've driven past it.

---

## 2. What's DONE (implemented + unit-tested)

A dedicated `YIELDING` state was added to the behaviour state machine.

| Piece | Location |
|-------|----------|
| `YIELDING` state + `_h_yielding()` handler | [`packages/state_machine.py`](packages/state_machine.py) |
| Yield rerouted off the old stop/right-of-way path (in `_h_driving`) | [`packages/state_machine.py`](packages/state_machine.py) |
| `yield_slow_scale` tunable (default `0.7`) | `BehaviorConfig` + `config/traffic_signs_config.yaml` → `behavior:` |
| `_mark_handled(await_line=False)` so a yield doesn't block later signs | [`packages/state_machine.py`](packages/state_machine.py) |
| Unit test (Stage 5b) for the new behaviour | [`packages/test_state_machine.py`](packages/test_state_machine.py) `test_stage5_yield_slows_then_resumes()` |
| `YIELDING` excluded from the post-state lane-PD reset (no steering blip) | the 3 servers (see §6) |
| `YIELDING` overlay colour (amber) | server fallback + `servers/traffic_signs/visualization.py` |

**Behaviour, precisely** (`_h_yielding`):
1. Yield tag comes into range → enter `YIELDING`, lane-follow at
   `yield_slow_scale` (0.7 = ~30 % slower). Never stops.
2. While the tag is still visible & close → hold the reduced speed.
3. Tag drops out of view/range (we've passed it) → after `lost_grace_s` (0.5 s)
   return to `DRIVING` at `cruise_scale`.
4. An obstacle dead ahead still overrides to `OBSTACLE_STOP`, as everywhere.

**Verification (no robot needed):**
```bash
python -m tasks.traffic_signs.packages.test_state_machine
```
Stage 5b asserts: enters `YIELDING`, slows but keeps moving (0.400 → 0.280),
does **not** stop for a robot on the right, resumes cruise after passing, and
never touches `STOPPED` / `WAITING_FOR_RIGHT_OF_WAY`. Currently **all checks
pass**.

`yield_slow_scale` is tunable in `config/traffic_signs_config.yaml`:
`1.0` = no slowdown, lower = slower.

---

## 3. The OPEN ISSUE: the yield tag isn't recognized

Tag-ID → sign meaning is resolved by [`packages/sign_lookup.py`](packages/sign_lookup.py)
(`build_lookup`), overlaid with the `signs:` block in
`config/traffic_signs_config.yaml`. Right now that block only has the stop signs:

```yaml
signs:
  20: stop
  24: stop
```

Everything else falls back to the **example** defaults in `sign_lookup.py`
(`DEFAULT_TAG_IDS`), which assume yield = tags **32 / 33**. If glados's yield
sign uses a different AprilTag ID, it resolves to `sign_type = None`.

And an unknown sign is **filtered out before the state machine sees it** —
[`detection_activity.py` → `select_active_sign()`](packages/detection_activity.py#L66-L69)
keeps only `d.sign_type is not None`. So an unmapped yield tag produces **no
active sign at all**, and the `YIELDING` behaviour never fires. That's why "it
does not react at all."

> Note: this assumes the tag is **detected but unmapped**. The stop (20/24) and
> intersection (11) tags decode fine, so that's the likely case — but confirm
> (see below) before assuming. If the tag isn't decoding at all, neither fix
> below helps; that'd be a detector/lighting problem instead.

---

## 4. How to finish it — pick ONE

### Option A — map the real tag ID  (RECOMMENDED, ~2 min, bulletproof)

The unmapped tag's ID already appears in `GET /status` → `detections` (as
`sign_type: null` with its `tag_id`) even though it's filtered out of the active
sign. So:

1. On the bot, run the task, point the camera at the yield sign.
2. Read the `tag_id` from `/status` (or `python scripts/grab_frame.py <host>`,
   which prints the `detections` list).
3. Add it to `config/traffic_signs_config.yaml`:
   ```yaml
   signs:
     20: stop
     24: stop
     <ID>: yield     # <- the number you just read
   ```
   (`config/` ships to the bot on every `python launch.py --run`.)

Done. No code change, no standing assumptions.

### Option B — treat any unrecognized sign as a yield  (fallback the team floated)

Rationale: "yield is the only unmapped sign, and a yield slowdown is harmless,
so just trigger it for anything unknown."

**Trade-offs / review:**
- ✅ Yield is the most benign reaction (slows, never stops/turns) → a false
  trigger is low-cost.
- ✅ Size/distance filters (≥18 px, ≤1.5 m) run *before* selection, so it won't
  fire on tiny/far noise — only a plausible, close tag.
- ⚠️ Permanently couples "any unmapped tag" → "yield." A new sign, another
  unmapped tag, or a rare AprilTag misread will all make the bot slow down.
- ⚠️ Fragile: the moment more than one sign is unmapped, this is wrong.

**If you implement it, do it as a reversible config switch, not a hardcode.**
Suggested shape:
- Add `unknown_sign_as: ""` to `config/traffic_signs_config.yaml` (default off).
- In `select_active_sign()` ([`detection_activity.py`](packages/detection_activity.py#L61-L87)),
  when a detection has `sign_type is None` and the option is set, surface it with
  `sign_type = unknown_sign_as` (e.g. `"yield"`) instead of dropping it — but
  keep **known** signs as higher priority (only fall back to an unknown tag when
  no known sign is in view), so a real stop/intersection is never overridden.
- Leave the state machine untouched: it already handles `sign_type == "yield"`.

This keeps the contract in INTERFACE.md intact (`active["sign_type"]` stays a
valid vocabulary string) and lets you flip it off the instant the real ID is
mapped (Option A).

---

## 5. Recommendation

Do **Option A**. It's faster than implementing Option B and has no standing
assumptions. Only reach for Option B if reading the ID off `/status` turns out to
be impractical — and even then, ship it behind the `unknown_sign_as` toggle so
it's easy to retire.

---

## 6. File map (everything yield touches)

```
config/traffic_signs_config.yaml          signs: block (mapping) + behavior.yield_slow_scale
tasks/traffic_signs/packages/
  sign_lookup.py                           tag-id -> sign_type (DEFAULT_TAG_IDS + config override)
  detection_activity.py                    select_active_sign() — drops unknown tags (the filter to change for Option B)
  state_machine.py                         YIELDING state + _h_yielding() + yield_slow_scale + _mark_handled(await_line)
  test_state_machine.py                    test_stage5_yield_slows_then_resumes()
  traffic_signs_server.py                  on-bot server (ships via --run); YIELDING import + reset-exclusion + overlay colour
servers/traffic_signs/
  real_server.py, virtual_server.py        local-dev servers (ship via git checkout on the bot); same YIELDING edits
  visualization.py                         YIELDING overlay colour
```

**Deploy note:** `config/` and `tasks/traffic_signs/packages/` ship to glados via
`python launch.py --run --bot <host> --task traffic_signs`. The `servers/`
files are not packaged by `--run` — they reach the bot via its repo checkout.
So Options A and B above (config + `packages/`) both deploy via `--run` alone.
