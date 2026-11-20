# libvirt-autoreclaim

Daemon that reclaims unused memory from libvirt QEMU guests by adjusting guest
memory through balloon or virtio-mem devices. An adaptive EWMA/PD controller
balances guest reserves against host memory pressure, with slow reclaim and
rapid guest memory growth.

## VM Requirements

Monitoring requires `auto_reclaim: true` in the VM's effective configuration.
Automatic memory adjustments require an enabled balloon or at least one
virtio-mem device. A balloon device normally defines a stats period from 1 to
5 seconds:

```xml
<devices>
  <memballoon autodeflate='on'>
    <stats period='5'/>
  </memballoon>
</devices>
```

For memory control and `status`, the daemon queries QEMU Guest Agent first.
It reads `MemTotal`, `MemAvailable`, `AnonPages`,
`Active(file)`, `Inactive(file)`, `SwapTotal`, and `SwapFree` in a single
`guest-file-read` of the guest's `/proc/meminfo` per poll when the VM has a
QEMU Guest Agent channel. The XML must define the
`org.qemu.guest_agent.0` Unix channel and the agent must be running and
reachable. With a valid guest sample, the daemon uses `MemTotal` for balloon
guests without querying balloon statistics. If the agent times out or cannot
read meminfo (for example, on Windows), it falls back to fresh balloon
statistics when available. Each agent command has a one-second timeout.
When no valid balloon stats
period is configured, the daemon polls at the default 5-second interval.
Without a usable guest agent, a missing, non-numeric, zero, or greater-than-5
period produces a warning and the VM is not monitored. `autodeflate='on'` is
recommended, but optional; its absence produces a warning only.
The daemon also warns when a balloon device has no `stats period` even if
QEMU Guest Agent is available as the primary statistics source.

A VM without a balloon device is also monitored when its XML defines both a
SPICE channel (`type='spicevmc'`) and the QEMU Guest Agent channel above. In
this case, the VM appears as `read-only` in `status`, which reads its memory
statistics from the agent. The daemon does not poll it, adjust its memory,
or include it when choosing the polling interval.

When a VM has virtio-mem devices but no balloon device, the QEMU Guest Agent
channel is required to provide guest memory statistics. The daemon then adjusts
memory by updating the virtio-mem device requests live; no SPICE channel is
required. If both devices are enabled, the daemon adjusts only virtio-mem.
When virtio-mem sizes are available, `Actual` is the fixed RAM plus the
currently plugged virtio-mem. Guest Agent `Usable` is capped at this allocation.
`Usable` can still come from fresh balloon statistics when the agent is unavailable.
If the balloon has been adjusted separately, those values may not
describe the same guest memory allocation. Prefer disabling the balloon with
`<memballoon model='none'/>` and using QEMU Guest Agent for statistics on
virtio-mem guests.

The daemon uses `libvirt-python` with one persistent connection to
`qemu:///system`. The polling interval is the minimum XML stats period among
active monitored VMs, using libvirt's event loop. Inactive VMs do not affect
the interval, and stats periods on a `<memballoon model='none'>` device are
ignored. Discovery, definition changes, and removal of VMs recalculate the
interval and update the running timer without restarting the daemon. With no
active monitored VMs, the interval is 5 seconds. For large fleets using the
agent, prefer a 3- to 5-second interval to limit polling overhead.
Each VM is controlled only when its own statistics have updated. Guest Agent
samples use monotonic collection time, so two samples in the same wall-clock
second are still distinct. Guest
`/proc/pressure/memory` is read only after a successful Guest Agent meminfo
read, when available memory is below twice the effective reserve or the PD
controller computes a negative correction.

After an initial inventory, libvirt lifecycle events discover newly defined
or started VMs and recheck their monitoring requirements without restarting
the daemon. Stopped, crashed, or removed VMs have their tracking state cleared;
restarting a VM starts fresh manual-increase tracking. Running `virsh undefine`
on an active, monitored VM does not stop monitoring it: it continues running
as a transient domain. Whenever a VM starts, the daemon reloads the
configuration file and rechecks monitoring for all VMs. The updated settings
also apply to guests that are already running. If the file cannot be read or
is invalid, the daemon logs an error and keeps the previous valid configuration.
Monitoring messages list the initial monitored VMs, then only changes:
`Monitoring <name>` when a VM enters monitoring and `Stopped monitoring <name>`
when it leaves. Configuration reloads and repeated lifecycle events do not
repeat these messages for unchanged VMs.

## Converting VM memory XML

`virtiomem_convert` converts libvirt domain XML between `virtio-balloon` and
`virtio-mem`. It can read and write XML files, or update a VM directly through
`virsh`. The script requires Python 3.7 or later and uses the Python standard
library to process XML. `virsh` is required only for direct VM operation.

The conversion modes are:

- `to-virtiomem`: sets a fixed base memory and configures a `virtio-mem` device,
  NUMA topology, and maximum memory. The base defaults to `1GiB` and is rounded
  down to a multiple of 2 MiB. The generated NUMA cell excludes vCPU IDs
  explicitly marked `enabled='no'`. If NUMA cells already exist, the converter
  preserves all but the first cell's memory allocation and assigns the
  remaining base memory to the first cell. It rejects a base that leaves no
  memory for that cell. By default, the balloon is disabled with
  `model='none'`; `-k` (`--keep-balloon`) preserves its current model (or
  creates a `virtio` balloon if one is absent).
- `to-balloon`: removes `virtio-mem` devices and restores the VM's memory
  configuration for `virtio-balloon`. When NUMA cells are present, their memory
  defines the fixed base, so any virtio-mem capacity already included in the
  top-level `<memory>` is not counted twice. If the balloon model is `none`,
  it is changed to `virtio`.

Convert an XML file and save the result:

```bash
virtiomem_convert to-virtiomem vm.xml vm-virtiomem.xml --base-memory 2GiB
```

With `to-virtiomem`, use `--initial-memory 3GiB` (or `-i 3GiB`) to start with
3 GiB of total guest memory instead of the input XML's `<currentMemory>` (or
`<memory>` if it is absent). The value must be between the fixed base and the
selected maximum memory. If it is not a multiple of 2 MiB, the converter
rounds it down and reports the requested and effective values on standard
error. In this direction, the option requires a domain without a `virtio-mem`
device.

With `to-balloon`, `--initial-memory` sets `<currentMemory>` directly, without
2 MiB rounding or changing the calculated `<memory>`. The value must be
positive and no greater than the resulting `<memory>`.

Use `--max-memory SIZE` (or `-m SIZE`) to override the maximum total guest
memory in either direction. With `to-virtiomem`, it replaces the input
`<memory>` as the maximum. The order of `--base-memory` and `--max-memory` on
the command line does not matter: the converter rounds down the base first,
then rounds down the capacity above that base to whole 2 MiB blocks. For
example, with `--base-memory 1024M --max-memory 2049M`, the resulting
`<maxMemory>` is 2048 MiB: 1024 MiB of base memory plus 1024 MiB of virtio-mem
capacity. With `to-balloon`, `--max-memory` sets the output `<memory>` directly
without rounding. If the initial memory would exceed the requested maximum,
also specify a lower `--initial-memory`.

To keep the existing balloon model during conversion:

```bash
virtiomem_convert to-virtiomem vm.xml vm-virtiomem.xml -k
```

The output XML is written atomically, so the input and output paths may be the
same. Replacing an existing file preserves its permission bits; a new file
uses the system's normal file creation permissions.

Preview the changes without writing a file with `-n` (`--dry-run`).
Use `-L` (`--color`) with `auto`, `always`, or `never`:

```bash
virtiomem_convert to-virtiomem vm.xml -n -L always
```

To update a VM directly with `-V` (`--vm`), it must be shut off, persistent,
and have no snapshots. Preview first, then omit `-n` to apply the conversion.
`--connect` optionally selects the libvirt URI (otherwise `virsh` uses its
default):

```bash
virtiomem_convert to-virtiomem -V guest-a --base-memory 2GiB -n
virtiomem_convert to-virtiomem --vm guest-a --base-memory 2GiB
virtiomem_convert to-balloon --vm guest-a
```

Use `virtiomem_convert --help` for all options and `man 1 virtiomem_convert`
for the manual.

## Configuration

The configuration file defaults to `/etc/libvirt/autoreclaim.json`. Select a
different file with `-c` or `--config`:

```
libvirt-autoreclaim --config /path/to/autoreclaim.json start
libvirt-autoreclaim -c /path/to/autoreclaim.json status
```

`status` displays an aligned table of active monitored VMs, including `Backend`,
memory values in MiB, and the effective memory weight. `Backend` combines the
statistics source (`agent`, `balloon`, or `unavailable`) and the memory method
(`balloon`, `virtio-mem`, or `read-only`) as `source/method`. For example,
`agent/virtio-mem` uses QEMU Guest Agent statistics and adjusts memory via
virtio-mem. `balloon/balloon` is displayed simply as `balloon`. Column
widths adapt to domain names and values. Connection, configuration-loading,
and monitoring messages are hidden by default. An unresponsive or disconnected
QEMU Guest Agent is reported only with `--debug`; if no other statistics are
available, `Backend` uses `unavailable` as the source. Other warnings and
errors remain visible. The `start`
command retains informational logs.
Informational messages have no level prefix; other log levels retain their
prefixes (`DEBUG`, `WARNING`, `ERROR`).

- `Floor`: effective reclaim floor in MiB, including the reserve, fixed
  virtio-mem base RAM, and guest anonymous pages and active file cache when
  available. This is a reclaim limit, not a growth target; read-only VMs show
  `n/a`.
- `PSI`: guest memory `some avg10`, reported as a percentage. `status` reads
  it once per VM whose `Backend` starts with `agent/`; it shows `n/a` if
  unavailable. This does not change PSI polling in the daemon.
- `Swap_Used`: guest `SwapTotal - SwapFree` in MiB from the same meminfo sample,
  or `n/a` if the counters are unavailable.

The `default` section supplies values for every VM. A VM entry overrides those
values. If `auto_reclaim` is omitted from both places, it defaults to `false`.

```json
{
  "general": {
    "psi_threshold": 5.0,
    "host_min_free_ratio": 0.2
  },
  "default": {
    "auto_reclaim": true,
    "memory_weight": 100,
    "keep_free_kb": 524288,
    "keep_free_ratio": 0.1,
    "idle_reclaim_threshold": 0.65,
    "ewma_alpha": 0.3,
    "reclaim_ratio": 0.015,
    "pd_kp": 0.5,
    "pd_kd": 1.0,
    "manual_increase_grace_seconds": 600
  },
  "vms": [
    {"name": "guest-a", "auto_reclaim": false},
    {"name": "guest-b", "keep_free_kb": 1048576, "memory_weight": 500}
  ]
}
```

`keep_free_kb` sets the minimum usable memory to retain, as an integer number
of KiB. It must be at least `262144` KiB (256 MiB); smaller values are invalid.
It defaults to 512 MiB (`524288` KiB), regardless of the
VM's total memory. A per-VM value overrides the default.

`memory_weight` is an integer from 1 to 1000, defaulting to `100`. It can be
set in `default` and overridden per VM. Higher weights protect critical VMs
from reclaim; lower weights make secondary VMs yield sooner and faster.
Weights do not reduce the reserve or floor and do not limit emergency growth.

The following finite numeric settings can also be overridden per VM:

| Setting | Default | Range | Meaning |
| --- | --- | --- | --- |
| `keep_free_ratio` | `0.1` | 0 to 1 | Proportional usable-memory reserve |
| `idle_reclaim_threshold` | `0.65` | 0 to 1 | Surplus fraction of current RAM above which reclaim uses full pressure |
| `ewma_alpha` | `0.3` | Greater than 0, at most 1 | Weight of the newest working-set sample |
| `reclaim_ratio` | `0.015` | Greater than 0, at most 0.05 | Base fraction of current RAM reclaimed per second, before weight scaling |
| `pd_kp` | `0.5` | Greater than 0, at most 1 | Proportional gain |
| `pd_kd` | `1.0` | 0 to 10 seconds | Derivative gain; 0 disables derivative braking |

These finite numeric settings apply to the whole host and are accepted only in
the optional `general` section:

| Setting | Default | Range | Meaning |
| --- | --- | --- | --- |
| `psi_threshold` | `5.0` | Greater than 0, at most 100 | PSI `some avg10` percentage at which reclaim reaches full pressure |
| `host_min_free_ratio` | `0.2` | Greater than 0, at most 1 | Host available-memory safety buffer as a fraction of total RAM |

When `general` or an option is omitted, the documented built-in defaults apply.

`manual_increase_grace_seconds` postpones reductions after the daemon detects
that a VM's current memory increased without a request from the daemon. Set it
per VM (or in `default`) to the desired number of seconds; `3600` gives one
hour. During that time the daemon can still increase memory if the VM needs it.
It defaults to `0` (no delay) and must be a non-negative integer. The daemon
starts tracking at launch, so increases made before it starts are not detected.
Detection uses fresh memory samples, allowing for virtio's 4 KiB page
rounding and the daemon's outstanding requests. It is not proof of an operator
action: an increase not attributable to the daemon, including guest autodeflate,
can also start the grace period. Logs call these increases "unattributed".
Configuration changes take effect when the next VM starts, or after restarting
the service.

## Configuration tuning

Start with the example configuration, measure a representative workload
including peak demand, and change one parameter at a time. Put host-wide
settings in `general`, shared VM settings in `default`, and workload-specific
overrides in `vms`. The suggestions below are starting points, not guarantees
against host or guest OOM.

### 1. Choose reserves and priorities first

- **`keep_free_kb`** provides an absolute reserve for small VMs and sudden bursts.
  Start with 512 MiB (`524288` KiB). The minimum of 256 MiB (`262144` KiB)
  can suit measured, lightweight Linux micro-VMs or routers; consider 1 GiB
  (`1048576` KiB) or more for Windows guests, heavy application servers, or
  large allocation bursts. Do not lower it just to maximize reclaimed RAM.
- **`keep_free_ratio`** adds a reserve proportional to current allocation.
  Start at `0.1`; consider `0.15` to `0.20` for latency-sensitive, bursty,
  or I/O-heavy workloads such as databases and file servers. Extra headroom
  can help reduce cache churn, but is not a dedicated page-cache reservation.
  The effective reserve is the larger of the absolute and proportional values,
  not their sum. Increasing either setting also raises the minimum RAM floor.
  Check that the VM's maximum RAM leaves enough room for both its working set
  and reserve.
- **`memory_weight`** expresses reclaim priority, not a memory entitlement.
  Use `100` for standard web, proxy, or DNS services, `300` to `800` for
  critical production guests and databases, and `30` to `50` for development,
  CI, or batch workloads. Weight `500` gives one fifth of
  the normal reclaim rate; weights below `50` do not increase the rate beyond
  the twofold cap, although they still affect processing order. Emergency
  growth remains independent of weight.
- **`auto_reclaim`** can be set to `false` for workloads that should not be managed
  by this daemon. Their memory must still fit within the host's capacity.

This illustrative configuration combines a balanced shared profile with a
protected database and a more reclaimable CI runner. These are tuning examples,
not new built-in defaults; choose the host buffer and guest reserves from
measurements before deploying:

```json
{
  "general": {
    "psi_threshold": 5.0,
    "host_min_free_ratio": 0.12
  },
  "default": {
    "auto_reclaim": true,
    "keep_free_kb": 524288,
    "keep_free_ratio": 0.1,
    "memory_weight": 100,
    "ewma_alpha": 0.25,
    "reclaim_ratio": 0.015,
    "pd_kp": 0.4,
    "pd_kd": 1.2,
    "manual_increase_grace_seconds": 1800
  },
  "vms": [
    {
      "name": "db-prod-01",
      "memory_weight": 500,
      "keep_free_kb": 2097152,
      "keep_free_ratio": 0.15,
      "reclaim_ratio": 0.005,
      "manual_increase_grace_seconds": 3600
    },
    {
      "name": "ci-runner-01",
      "memory_weight": 40,
      "keep_free_kb": 262144,
      "keep_free_ratio": 0.05,
      "reclaim_ratio": 0.025,
      "manual_increase_grace_seconds": 300
    }
  ]
}
```

### 2. Size the host safety buffer

- **`host_min_free_ratio`** determines when low host available memory starts
  contributing to reclaim. A larger value starts reclaim earlier and retains
  more host headroom; a smaller value allows more RAM to remain with guests.
  On smaller hosts with 16 to 32 GiB, keep the default `0.20` as an initial
  cushion for QEMU and the host kernel. On hosts with 256 to 512 GiB, that
  default corresponds to 51.2 to 102.4 GiB; consider `0.08` to `0.10` if
  measurements show the larger buffer is unnecessary. Allow for host services,
  allocation bursts, and reclaim latency before reducing it. This is a pressure
  input, not a hard reservation or a guarantee that this RAM stays unused.
- **`psi_threshold`** is the PSI `some avg10` percentage at which the PSI
  contribution reaches full pressure. Lowering it from `5` makes reclaim more
  aggressive for the same stalls; raising it tolerates more contention.
  It is not an on/off threshold: nonzero PSI already contributes below it.
  Either PSI or low available memory can trigger reclaim independently.
  For latency-sensitive host services or slow storage, consider `2.0` to
  `3.0` to reach stronger reclaim at lower stall percentages. Values of
  `8.0` to `10.0` may suit throughput-oriented, high-density deployments with
  fast NVMe storage only if measured latency remains acceptable. Fast storage
  alone does not make sustained memory stalls safe.

If host stalls persist, first check whether guests have reclaimable surplus,
are blocked by grace periods, or are already at their floors. Increasing
aggressiveness cannot recover memory that guests genuinely need.

### 3. Balance reclaim speed and responsiveness

- **Stats period (VM XML)**: use 1 to 2 seconds for rapidly changing workloads,
  or up to 5 seconds for steadier ones. Polling automatically follows the
  smallest monitored period, but each guest still supplies fresh samples at
  its own period. Faster polling cannot compensate for a slow guest stats
  period. For example, use `<stats period='1'/>` for `db-prod-01` and
  `<stats period='5'/>` for a steady secondary guest. This reduces the
  secondary guest's stats refresh frequency, not the daemon's query frequency:
  with a 1-second minimum it still polls all monitored guests every second.
- **`reclaim_ratio`** is a per-second base rate. Start at `0.015`; reduce it
  to `0.005` if reclaim causes guest I/O spikes or latency. Increase gradually
  only if the host remains pressured while guests have sustained surplus.
  For example, `0.015` over 2 seconds gives a 3% base limit, then pressure,
  idle ratio, and weight scale it further. At full pressure, idle ratio `0.6`,
  and weight `100`, the default gives a limit of 0.9% per second, not a fixed
  0.9% for every VM. Consider `0.025` to `0.03` for stateless or batch guests
  only after measuring the effect; low weights can double that limit.
  Longer sample intervals permit
  larger individual steps even at the same per-second rate; shorter stats
  periods help make those steps smaller. The PD correction and available
  surplus may impose a lower limit.
- **`ewma_alpha`** controls smoothing per fresh sample. Keep `0.3` initially.
  Values around `0.15` to `0.20` smooth sawtooth or noisy workloads more, but adapt more
  slowly; values around `0.4` to `0.5` track changes faster. The same alpha
  smooths over more wall-clock time with a longer stats period. Low usable
  memory and rapid consumption can still trigger emergency growth.
- **`pd_kp` / `pd_kd`** should be tuned last. Start with `0.5` and `1.0`.
  If targets repeatedly reverse direction, try a lower `pd_kp`, such as
  `0.3`, after checking reserves and sample freshness. With a clear, low-noise
  trend, test `pd_kd` of `1.5` to `2.0` separately and keep it only if the
  measurements improve. The derivative term
  responds to the direction of change, not just its size; increasing `pd_kd`
  is not a universal cure for oscillation and can amplify noisy trends.
  Compare with a smaller value or `0` to diagnose its effect.

### 4. Set an appropriate manual-increase grace period

**`manual_increase_grace_seconds`** is useful when an operator adds RAM for a
temporary task, for example through a manual `virsh setmem`, and does not
want it reclaimed immediately. The initial configuration example's `600`
means 10 minutes. Consider `3600` (one hour) in `default` where manual
interventions are common and headroom allows it, with an override of `300`
(five minutes) for test VMs. The profile example above uses `1800` (30 minutes)
as its shared value. The built-in default is still `0`, which disables the delay.
Growth remains enabled during grace, but reclaim is postponed even when the
host is under pressure. Guest autodeflate can also trigger this grace period:
an "unattributed" increase is not necessarily a manual change. Avoid long
periods on heavily overcommitted hosts unless sufficient host headroom exists.

### 5. Verify the result under load

Use `libvirt-autoreclaim status` to inspect `Actual`, `Usable`, `Keep_Usable`,
and `Weight`. Observe host `MemAvailable` and `/proc/pressure/memory` alongside
guest swap activity, disk I/O, and application latency. Enable `--debug` on
the existing daemon invocation when investigating controller decisions; do not
run a second daemon against the same guests.

Compare `Usable` with `Keep_Usable`, rather than relying on the ratio of
`Actual` to `Keep_Usable`: allocated RAM alone does not show guest headroom.
If `Usable` repeatedly falls below `Keep_Usable` during peaks and frequent
`Grow dom:` messages follow reclaim, consider increasing that VM's
`keep_free_ratio` or reducing its `reclaim_ratio`.

Look for sustained host headroom, acceptable application latency, and targets
that settle rather than repeatedly shrink and grow. If guest swap or latency
rises during reclaim, reduce its rate or increase that VM's reserve or weight.
Resolve stale statistics, unavailable PSI, and balloon-request timeout warnings
before trying to compensate with controller gains. Apply configuration changes
by restarting the service or at the next VM start, then repeat the same workload
and compare measurements over several minutes, including bursts.

## Adaptive controller

For each fresh guest sample, `Used = actual - usable`. The controller smooths
this working-set estimate with:

```
UsedEWMA = alpha * Used + (1 - alpha) * PreviousUsedEWMA
Reserve = max(keep_free_kb, actual * keep_free_ratio)
Error = Reserve - (actual - UsedEWMA)
Elapsed = max(1, updated - previous_updated)
Derivative = (Error - PreviousError) / Elapsed
DerivativeTerm = max(-abs(Error), min(abs(Error), pd_kd * Derivative))
DeltaRAM = pd_kp * Error + DerivativeTerm
```

The first sample initializes the EWMA directly and has no derivative term.
Elapsed time comes from successive guest-statistics `last_update` timestamps,
not the polling interval, and is bounded below by one second. The derivative
contribution cannot exceed the current absolute error in either direction.
Duplicate samples are not processed again; missing
or invalid statistics are skipped, and statistics older than 15 seconds or
dated in the future produce a warning and are skipped. A backwards timestamp
resets the filter. When a VM starts, only its own controller history is reset
unless the reloaded configuration differs from the previous one. A configuration
change resets controller history for all VMs without itself discarding existing
manual-increase grace periods. Defining or resuming a VM also resets its own
controller history.
The status command's `Keep_Usable` column shows the effective reserve.
Its `Weight` column shows the effective `memory_weight`, including inheritance
from `default` or the built-in default of 100. Higher weights mean higher
priority when protecting memory from reclaim; the weight is dimensionless.

### Slow reclaim, rapid growth

Reclaim is allowed only when both the instantaneous and smoothed usable
memory exceed the reserve. It never consumes that instantaneous reserve.
The derivative term can brake reclaim but cannot turn it into growth while
the smoothed reserve is satisfied, unless an emergency growth signal is present.

Growth bypasses EWMA smoothing if instantaneous usable memory is below the
reserve, used memory grows faster than 5% of current RAM per second, guest
memory PSI `full avg10` exceeds 1%, or guest `SwapFree` falls faster than
10 MiB/s between samples.
The controller requests at least 25% of current RAM, or enough to cover the
reserve deficit and another polling interval of positive consumption trend,
whichever is larger. Ordinary positive PD corrections are capped at 50% of
current RAM per sample. Guest memory PSI `some avg10` above 10% forces an
ordinary growth request of at least 10% of current RAM instead of allowing a
negative PD correction. All requests are capped at the VM's maximum memory.
Progress toward an outstanding growth request does not trigger manual-increase
grace periods. Ordinary adjustments wait until the previous memory request
completes, rather than reversing or replacing a request still in flight.
Emergency growth can supersede an outstanding request with a higher target.
Uncompleted requests expire after 30 seconds, with a warning, so an unresponsive
guest does not block future adjustments indefinitely. The daemon may retry on
the next usable sample, subject to the normal pressure and grace-period rules.
Expiration is checked even when guest statistics are stale.

The static floor protects the reserve. A separate floor protects the guest's
anonymous pages and active file cache during reclaim only:

```
Denominator = max(0.1, 1 - keep_free_ratio)
ReserveFloor = int(2 * keep_free_kb / Denominator)
GuestFloor = ceil((AnonPages + Active(file) + keep_free_kb) / Denominator)
StaticFloor = min(maximum_ram, max(minimum_ram, ReserveFloor))
ReclaimFloor = min(maximum_ram, max(StaticFloor, GuestFloor))
```

`minimum_ram` is zero for balloon and the fixed (non-virtio-mem) RAM for
virtio-mem. The denominator is bounded below by 0.1, including for ratios near
or equal to 1. A VM below the static floor can be grown to it even when its
instantaneous reserve is sufficient, unless the proposed reclaim was vetoed.
If the guest floor reaches or exceeds current RAM, reclaim is skipped; active
file cache cannot trigger growth. The proportional reserve still uses
current RAM, not the VM's historical or maximum RAM. A VM whose maximum is too
small cannot maintain the configured reserve. `Inactive(file)` is not part of
the floor. If either guest floor metric is missing, the reserve floor still
applies. Balloon pages and virtio-mem blocks are rounded so that a guest-backed
floor is not crossed, subject to the VM's maximum.

- **Balloon:** Round the capped target down to a multiple of 4 KiB. Skip the
  adjustment if the rounded target differs from current RAM by less than 1 MiB.
- **Virtio-mem:** Split the portion above fixed RAM among devices in proportion
  to their configured capacities, then round each request to the nearest
  multiple of that device's block size (2 MiB if unspecified). Skip the
  adjustment if the combined target, including fixed RAM, differs from current
  RAM by less than the larger of 1 MiB and the smallest device block size.

These limits reduce risk; they do not guarantee that a guest will avoid OOM.

### Host pressure and reclaim priority

Once per control cycle, the daemon reads `some avg10` from
`/proc/pressure/memory` and `MemAvailable`/`MemTotal` from `/proc/meminfo`.
It must run on the hypervisor. The pressure factor is:

```
Pressure = max(min(avg10 / psi_threshold, 1),
               max(0, 1 - MemAvailable / (MemTotal * host_min_free_ratio)))
```

With the default settings, zero PSI and at least 20% host RAM available prevent
reclaim unless `usable - Reserve` is strictly greater than
`actual * idle_reclaim_threshold` (65% by default). Set this threshold in
`default` or per VM; 0 enables the pressure override for any positive surplus,
while 1 disables it. Above the threshold, the reclaim calculation uses full
pressure even when the host is not under pressure. The usual reclaim rate,
reserve, floor, and grace-period limits still apply. Otherwise, reclaim
increases continuously as available RAM
falls below that safety buffer, or PSI rises toward 5%. Both thresholds are
configurable in `general`.
An `avg10` value of `5` means 5%, not a fraction.

VMs are processed in descending yield score:

```
idle_ratio = usable / actual
weight_factor = memory_weight / 100.0
yield_score = idle_ratio / weight_factor
```

For example, idle ratio 0.4 with weight 50 scores 0.8, ahead of idle ratio
0.6 with weight 200, which scores 0.3. Zero idle memory scores zero regardless
of weight.

Guest memory PSI `some avg10` above 2%, any positive `full avg10`, or any drop in
`SwapFree` since the previous sample vetoes reclaim, including idle reclaim.
When `some avg10` is above 0.5% and at most 2%, the reclaim rate is multiplied by
`1 - some_avg10 / 2.0`. Missing guest metrics are not treated as pressure
signals; the other available limits still apply.

Each reclaim is bounded by the negative PD correction, the safely reclaimable
surplus, and the following rate limit before guest PSI damping:

```
actual * reclaim_ratio * Elapsed * Pressure * idle_ratio * min(2.0, 100.0 / memory_weight)
```

For a guest whose instantaneous `usable - Reserve` exceeds
`actual * idle_reclaim_threshold`, `Pressure` in this limit is 1 instead of
the host pressure factor.
`Elapsed` is the time in seconds between this VM's fresh guest-statistics
samples, bounded below by one second. The first sample uses the VM's XML stats
period. This keeps the base reclaim rate independent of the polling interval
and of other VMs' stats periods.

Weight 100 preserves the normal rate. Weight 500 reclaims at one fifth of that
rate, while weights of 50 or less are capped at twice the base rate. Emergency
growth due to a low reserve or rapid consumption remains independent of weight,
and all weights retain the same reserve and floor protections.

Thus idle, lower-priority guests yield first and more aggressively. If PSI or
host memory data is unavailable or invalid, an error
is logged and reclaim is disabled for that cycle. Guest growth remains enabled;
the daemon retries host statistics on the next cycle.

## Shutdown

SIGTERM (including `systemctl stop`) removes the polling timer, deregisters
lifecycle callbacks, and closes the libvirt connection before exiting with
status 143, which the supplied systemd unit treats as a successful shutdown.

# Installation

Requires Python 3 and the `libvirt-python` bindings (for example, the
`python3-libvirt` package on Debian/Ubuntu).
`make install` also installs `virtiomem_convert` in `/usr/bin` and its manual
page in `/usr/share/man/man1`. The converter requires Python 3.7 or later.
`virsh` is needed only for direct VM operation.

```
~# make install
~# systemctl enable libvirt-autoreclaim
~# systemctl start libvirt-autoreclaim
```
