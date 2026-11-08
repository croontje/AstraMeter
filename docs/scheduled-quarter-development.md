# Scheduled charging and quarter demand: development status

The extension is connected to the Python and ESPHome CT002/CT003 control loops
on branch `feature/scheduled-quarter-shaving`. It is an integrated
AstraMeter change intended for an upstream PR, not a separate battery-control
script. The fork is [croontje/AstraMeter](https://github.com/croontje/AstraMeter).
No installation, live battery control or upstream PR has been made.

See [configuration and behavior](ct002.md#scheduled-charging-and-fixed-quarter-demand)
for the working options. Both features default off.

## Implemented

- Configurable local charging window, initially 00:00–06:00 Europe/Brussels,
  including daylight-saving changes and windows crossing midnight.
- Fixed-quarter import target, using the existing peak-shaving threshold.
  Charging yields to the import budget and can turn into discharge.
- Existing per-battery allocation, limits, manual controls and phase detection.
  Outside the window, batteries retain energy for peaks and absorb solar surplus.
- No SOC polling, 80% target or software reserve. The batteries enforce their
  own SOC limits. There is no additional Marstek API connection.
- HomeWizard v1 quarter/import metadata from the same JSON HTTP response as
  whole-house power. No additional meter poll. The counter must show 30 seconds
  of consistent growth; a restart never assumes zero earlier quarter import.
- Fallback to instantaneous limiting on missing/stale/invalid quarter metadata.
  Missing grid data requests zero power from automatic batteries using their
  fresh CT reports, without feeding a fabricated grid value into the balancer.
- INI, add-on options/translations, config generator, dashboard configuration,
  ESPHome schema, runtime status and documentation. Python includes timezone
  data for installations without an operating-system timezone database.

The shared policy returns both an aggregate battery target and its correction
relative to reported battery output. The existing CT controller receives the
correction: CT messages are not absolute battery setpoints. The 800 W default
night-charge request is aggregate; solar charging can use the existing
per-battery limits. A 10-second response allowance reduces the quarter budget.

## Validation (2026-11-08)

- Ruff formatting/lint and Mypy for the Linux production target pass.
- 160 targeted tests pass, covering the integrated controller, telemetry
  wrappers, config wiring and existing peak-shaving/limit behavior.
- Compiled C++ comparison: 1000 policy vectors and 1192 sequential quarter
  tracker samples match Python, including gaps, counter resets and clock jumps.
- Real CT UDP simulation with the firmware battery model: charges, discharges
  during sustained cooking, and stops scheduled charging after the window.
  Additional simulations verify full/empty battery limits.
- Full steering evaluation: 47 scenarios × 5 seeds compared with unchanged
  upstream. No do-no-harm guardrail regressions; aggregate metrics are almost
  identical (small timing-related differences remain).
- `npm run check` passes, including generated configuration, TypeScript and
  matching Python/ESPHome dashboard assets (27.0 KiB gzip). The generator
  normalizes Windows CSS line endings before compression for Linux parity.
- Full Python suite attempted: 1578 passed, 175 skipped before stopping at
  20 failures. All 20 also fail on a separate, unchanged upstream checkout in
  this environment. These include Windows-incompatible shell/signal/network
  tests and two existing saturation assertions; the complete suite is not green.
- The 160 targeted tests also pass on the GitHub Linux runner, followed by
  all 10 tests in `dashboard.spec.ts` against the real simulated CT stack.
  The earlier local browser attempt was blocked by Windows `signal.SIGUSR1`.
- The amd64 Docker image builds successfully. An isolated container with
  networking disabled loads both policies and the Brussels timezone, then
  starts the service and returns a healthy dashboard health check.
- ESP32 ESP-IDF firmware compiles successfully with both features, time/sensor
  wiring and the dashboard enabled (`test.energy.esp32-idf.yaml`). The final
  build includes the meter-failure stop path.
- Wider component suite attempted: 152 passed, 31 skipped, two existing
  Windows code-generation failures and one host-build error. CMake selects
  MSVC, which rejects the suite's GCC `-Wextra` flag. An additional ESPHome
  host end-to-end attempt fails on the missing POSIX `sys/ioctl.h` header.
  These host suites still require a Linux run; the ESP32 build above passes.

Reproduce the main targeted checks from the repository root:

```sh
uv sync --extra dev
uv run pytest src/astrameter/ct002/energy_policy_test.py tests/components/ct002/test_energy_policy_parity.py tests/test_scheduled_quarter.py tests/test_ct002_peakshaving.py tests/test_balancer_power_limits.py tests/test_ct_settings_wiring.py tests/test_addon_golden_settings.py
cd web && npm ci && npm run check
```

## Remaining release checks and limits

The fork's `Fork quarter-night image` workflow runs on pushes to this feature
branch. It checks the targeted Python/C++ tests and dashboard in Linux, builds
an amd64 container, and smoke-tests it without networking before publishing
`ghcr.io/croontje/astrameter:quarter-night` plus a full-commit `sha-...` tag.
The image is a test build, not an upstream release. The workflow never connects
to or deploys on unRAID. Changes to that server require the owner's separate
permission. The first successful build is
[run 42509810422](https://github.com/croontje/AstraMeter/actions/runs/42509810422),
for commit `c34916e8272f2bcb09bbe2296e5c65442e2aa969`. Its published digest is
`sha256:93f712fbc92c30737544ad4c2d166b7f769ef19628be70b7995cfa33d21e4862`.
Anonymous registry access to the image has been verified.

The full upstream suite, remaining host component suites and a supervised
hardware trial remain release checks. The Linux dashboard tests and container
build/start checks above are complete. No live installation was altered to
obtain test results.

Quarter telemetry is currently supported by the Python HomeWizard v1 JSON HTTP
adapter with `$.active_power_w`. Other sources use instantaneous fallback;
firmware can wire equivalent quarter/import sensors manually. Unscaled meter
power and matching import counters are required. Unsupported telemetry is not
silently estimated from a process-start baseline.

The controller cannot guarantee a billed peak when a battery is empty, power
limited or unreachable, or when future loads exceed remaining capacity. A
complete loss of CT communication depends on the battery's own timeout. Summer
self-consumption switching remains outside this change.

Upstream base: `develop` at `3d86ebe0a62809099013c1b08a8c51cfe902cd97`.
