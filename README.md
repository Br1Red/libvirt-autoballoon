# libvirt-autoballoon

Daemon that reclaims unused memory from libvirt QEMU guests by adjusting their
balloon devices.

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

The daemon takes its VM inventory at startup. A VM must already be defined in
libvirt when the daemon starts; it may be powered off and will be monitored when
it is later started. Restart the daemon to include VMs defined after startup.

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
	"default": {
		"balloon": false,
		"keep_free_kb": 524288,
		"threshold": 0.5
	},
	"vms": [
		{"name": "guest-a", "balloon": true},
		{"name": "guest-b", "balloon": true, "keep_free_kb": 1048576, "threshold": 0.25}
	]
}
```

`keep_free_kb` sets the minimum usable memory to retain, as an integer number
of KiB. It must be at least `262144` KiB (256 MiB); smaller values are invalid.
It defaults to 512 MiB (`524288` KiB), regardless of the
VM's total memory. A per-VM value overrides the default.

`threshold` is a finite number greater than zero and defaults to `0.5`. For a
`keep_free_kb` value of $K$ and threshold $T$, the daemon acts when usable memory
falls below $K$ or rises above $K(1 + 2T)$. It then targets usable memory of
$K(1 + T)$. For example, with the default `threshold` of `0.5`, the action
range is below $K$ or above $2K$, and the target is $1.5K$. A per-VM value
overrides the default.

The RAM target is at least $2K$, but is capped at the VM's maximum memory. If
that maximum is below $2K$, the daemon uses the maximum available value and
cannot maintain the configured reserve.

# Installation

```
~# make install
~# systemctl enable libvirt-autoballoon
~# systemctl start libvirt-autoballoon
```
