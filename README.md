# libvirt-autoballoon

Daemon that reclaims unused memory from libvirt QEMU guests by adjusting their
balloon devices. An adaptive EWMA/PD controller balances guest reserves against
host memory pressure, with slow reclaim and rapid guest memory growth.

## VM Requirements

A VM is monitored only when its effective configuration has `balloon: true`
and its libvirt XML defines a balloon stats period from 1 to 5 seconds:

```
<devices>
	<memballoon autodeflate='on'>
		<stats period='5'/>
	</memballoon>
</devices>
```

The `stats` element and its `period` attribute are required for monitoring. A
missing, non-numeric, zero, or greater-than-5 period produces a warning and the
VM is not monitored. `autodeflate='on'` is recommended, but optional; its
absence produces a warning only.

The daemon uses `libvirt-python` with one persistent connection to
`qemu:///system`. The polling interval is the minimum XML stats period among
monitored VMs, using libvirt's event loop. Discovery, definition changes, and
removal of VMs recalculate the interval and update the running timer without
restarting the daemon. With no monitored VMs, the interval is 5 seconds.
Each VM is controlled only when its own statistics have updated.

After an initial inventory, libvirt lifecycle events discover newly defined
or started VMs and recheck their monitoring requirements without restarting
the daemon. Stopped, crashed, or removed VMs have their tracking state cleared;
restarting a VM starts fresh manual-increase tracking. Undefining a running VM
does not stop monitoring it. Whenever a VM starts, the daemon reloads the
configuration file and rechecks monitoring for all VMs. The updated settings
also apply to guests that are already running. If the file cannot be read or
is invalid, the daemon logs an error and keeps the previous valid configuration.

## Configuration

The configuration file defaults to `/etc/libvirt/autoballoon.json`. Select a
different file with `-c` or `--config`:

```
libvirt-autoballoon --config /path/to/autoballoon.json start
libvirt-autoballoon -c /path/to/autoballoon.json status
```

The `default` section supplies values for every VM. A VM entry overrides those
values. If `balloon` is omitted from both places, it defaults to `false`.

```json
{
	"general": {
		"psi_threshold": 5.0,
		"host_min_free_ratio": 0.2
	},
	"default": {
		"balloon": true,
		"memory_weight": 100,
		"keep_free_kb": 524288,
		"keep_free_ratio": 0.1,
		"ewma_alpha": 0.3,
		"reclaim_ratio": 0.015,
		"pd_kp": 0.5,
		"pd_kd": 1.0,
		"manual_increase_grace_seconds": 600
	},
	"vms": [
		{"name": "guest-a", "balloon": false},
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

When `general` or an option is
omitted, the documented built-in defaults apply.

`manual_increase_grace_seconds` postpones reductions after the daemon detects
that a VM's current memory increased without a request from the daemon. Set it
per VM (or in `default`) to the desired number of seconds; `3600` gives one
hour. During that time the daemon can still increase memory if the VM needs it.
It defaults to `0` (no delay) and must be a non-negative integer. The daemon
starts tracking at launch, so increases made before it starts are not detected.
Detection uses fresh balloon memory samples, allowing for virtio's 4 KiB page
rounding and the daemon's outstanding requests. It is not proof of an operator
action: an increase not attributable to the daemon, including guest autodeflate,
can also start the grace period. Logs call these increases "unattributed".
Configuration changes take effect when the next VM starts, or after restarting
the service.

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
resets the filter. Lifecycle transitions and configuration changes reset
controller history, without discarding existing manual-increase grace periods.
The status command's `Keep_Usable` column shows the effective reserve.
Its `Weight` column shows the effective `memory_weight`, including inheritance
from `default` or the built-in default of 100. Higher weights mean higher
priority when protecting memory from reclaim; the weight is dimensionless.

### Slow reclaim, rapid growth

Reclaim is allowed only when both the instantaneous and smoothed usable
memory exceed the reserve. It never consumes that instantaneous reserve.
The derivative term can brake reclaim but cannot turn it into growth while
the smoothed reserve is satisfied, unless an emergency growth signal is present.

If instantaneous usable memory is below the reserve, or used memory grows
faster than 5% of current RAM per second, growth bypasses EWMA smoothing.
The controller requests at least 25% of current RAM, or enough to cover the
reserve deficit and another polling interval of positive consumption trend,
whichever is larger. Ordinary positive PD corrections are capped at 50% of
current RAM per sample. All requests are capped at the VM's maximum memory.
Progress toward an outstanding growth request does not trigger manual-increase
grace periods. Ordinary adjustments wait until the previous balloon request
completes, rather than reversing or replacing a request still in flight.
Emergency growth can supersede an outstanding request with a higher target.
Uncompleted requests expire after 30 seconds, with a warning, so an unresponsive
guest does not block future adjustments indefinitely. The daemon may retry on
the next usable sample, subject to the normal pressure and grace-period rules.
Expiration is checked even when guest statistics are stale.

The minimum RAM target accounts for both the absolute reserve and the configured
ratio, capped at the VM's maximum:

```
Floor = min(maximum_ram, int(2 * keep_free_kb / max(0.1, 1 - keep_free_ratio)))
```

The denominator is bounded below by 0.1, including for ratios near or equal to
1. A VM below this floor can be grown to it even when its instantaneous reserve
is sufficient. The proportional reserve still uses current RAM, not the VM's
historical or maximum RAM. A VM whose maximum is too small cannot maintain the
configured reserve.
Targets are capped at the VM's maximum and rounded down to a multiple of 4 KiB.
Adjustments smaller than 1 MiB are then skipped based on the absolute difference
in KiB. Logs display MiB with three decimal places; the same page-aligned KiB
target is sent to libvirt and recorded in pending requests.
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
all reclaim.
Reclaim increases continuously as available RAM falls below that safety buffer,
or PSI rises toward 5%. Both thresholds are configurable in `general`.
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

Each reclaim is bounded by the negative PD correction, the safely reclaimable
surplus, and:

```
actual * reclaim_ratio * Elapsed * Pressure * idle_ratio * min(2.0, 100.0 / memory_weight)
```

`Elapsed` is the time in seconds between this VM's fresh guest-statistics
samples, bounded below by one second. The first sample uses the VM's XML stats
period. This keeps the base reclaim rate independent of the polling interval
and of other VMs' stats periods.

`reclaim_ratio` is now a per-second rate, rather than a per-sample fraction.
The new default `0.015` preserves the old default's base rate at a 2-second
stats period. Divide an existing explicit value by 2 to preserve its base rate
at that period (for example, change `0.03` to `0.015`).

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

```
~# make install
~# systemctl enable libvirt-autoballoon
~# systemctl start libvirt-autoballoon
```
