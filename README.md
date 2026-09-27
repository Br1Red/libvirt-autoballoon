# libvirt-autoballoon
libvirt-autoballoon: daemon to autoballoon guest memory by virsh on host with libvirt

That script just detect all running libvirt-qemu guests on localhost  
and try balloon unused memory from guests to host

Proof of concept

The configuration file defaults to `/etc/libvirt/autoballoon.json`. Use
`-c` or `--config` to specify another path:

```
libvirt-autoballoon --config /path/to/autoballoon.json start
libvirt-autoballoon -c /path/to/autoballoon.json status
```

Values in the `default` section are inherited by each VM; values set on a VM
override them. If `balloon` is missing from both places, ballooning is disabled.
If `keep_free_kb` is not set in either place, the daemon keeps its automatic
threshold of 512 MiB, independent of the VM's total memory.

# Installation

```
~# make install
~# systemctl enable libvirt-autoballoon
~# systemctl start libvirt-autoballoon
```
