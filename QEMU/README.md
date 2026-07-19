# QEMU Image Structure

`base.qcow2` is the base environment without any language-specific installations.

It contains the main operating system files and **should not be used directly**. Instead, create an **overlay** from the base image and use that overlay.

If this directory does not contain any `.qcow2` files, you need to run `ImpaCtODE.py` or `LanguageSupport.py` to download the base and language support files.<br>
The download and extract process is automatic, but you can download files from github manually.

---

## What is an Overlay?

An overlay is a QCOW2 image that references another QCOW2 image (its backing file).

Only the **differences** from the backing image are stored, so the overlay remains small. For example, installing a language runtime only increases the overlay by roughly the size of the installed files instead of duplicating the entire base image.

This is much more storage-efficient than copying `base.qcow2` for every environment.<br>
Currently, the overlay backing image's path is relative, so moving `base.qcow2` will prevent the overlay from booting since it cannot find it anymore.

---

## Creating a Language Overlay

Make sure your current working directory is this directory.

### Windows

```bat
Binaries\Windows\qemu-img.exe create ^
-f qcow2 ^
-b ../base.qcow2 ^
-F qcow2 ^
Overlays\NEW-OVERLAY.qcow2
```

### Linux

```bash
Binaries/Linux/bin/qemu-img create \
-f qcow2 \
-b ../base.qcow2 \
-F qcow2 \
Overlays/NEW-OVERLAY.qcow2
```

> [!NOTE]
> On Linux, you may need to make `qemu-img` executable before using it:
>
> ```bash
> chmod +x Binaries/Linux/bin/qemu-img
> ```

---

## Configuring the Overlay

After creating your language overlay, update `config.py` and set the corresponding language's `overlay_path` to the newly created image.

---

## Disposable Runtime Overlays

For every execution, Python automatically creates **another overlay** on top of the language overlay.

This results in a three-layer image hierarchy:

```text
base.qcow2
    │
    ├── Language Overlay
    │       │
    │       └── Disposable Runtime Overlay
```

This approach provides:

- Storage efficiency
- Safe isolation
- Easy cleanup after execution
- Support for multiple concurrent sandboxes

The disposable overlay is deleted after the run, while the language overlay remains unchanged.

---

# Launching QEMU

## Windows

These manual commands use `mon:stdio` for interactive debugging. The bot uses
a separate loopback TCP character device for its guest serial channel, so the
QEMU monitor cannot consume or alter command bytes.

```bat
Binaries\Windows\qemu-system-x86_64.exe ^
-m 1024 ^
-smp 2 ^
-drive file=NEW-OVERLAY.qcow2,if=virtio,format=qcow2 ^
-device virtio-net-pci,netdev=net0 ^
-netdev user,id=net0 ^
-serial mon:stdio ^
-display none ^
-no-reboot ^
-accel whpx
```

## Linux

```bash
Binaries/Linux/bin/qemu-system-x86_64 \
-m 1024 \
-smp 2 \
-drive file=NEW-OVERLAY.qcow2,if=virtio,format=qcow2 \
-device virtio-net-pci,netdev=net0 \
-netdev user,id=net0 \
-serial mon:stdio \
-display none \
-no-reboot \
-accel kvm
```

### Default Configuration

| Setting | Value |
|---------|------:|
| Memory | 1 GB |
| CPU Cores | 2 |
| Disk | VirtIO QCOW2 |
| Network | User-mode NAT |
| Console | Dedicated TCP serial channel (bot) / `mon:stdio` (manual debugging) |
| Display | None |
| Reboot | Disabled |
| Acceleration | WHPX (Windows) / KVM (Linux) |

> [!NOTE]
> If hardware acceleration is unavailable, either remove the `-accel` argument or replace it with:
>
> ```text
> -accel tcg
> ```
>
> TCG uses software emulation and is considerably slower.

---

# Creating a Disposable Overlay Manually

Normally this is handled automatically by Python.

### Windows

```bat
Binaries\Windows\qemu-img.exe create ^
-f qcow2 ^
-b ../NEW-OVERLAY.qcow2 ^
-F qcow2 ^
Overlays\SequenceDisposal\run-temp-0001.qcow2
```

### Linux

```bash
Binaries/Linux/bin/qemu-img create \
-f qcow2 \
-b ../NEW-OVERLAY.qcow2 \
-F qcow2 \
Overlays/SequenceDisposal/run-temp-0001.qcow2
```

The resulting image is temporary and can safely be deleted after the sandbox finishes.
