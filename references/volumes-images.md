# Volumes, disk images, RAM disks

All commands below were run without sudo on 2026-11-22 (macOS 27.2.1) on user-created disk images. Adding volumes to the internal container (`disk3`) works the same way but changes the boot disk: ask the user first.

## APFS volumes inside a container

Volumes in a container share free space. Each volume has its own case-sensitivity, encryption, quota and reserve.

```bash
diskutil apfs list                                   # containers, volumes, roles
diskutil apfs addVolume CONTAINER "Case-sensitive APFS" Dev -quota 50g   # case-sensitive, capped at 50 GB
diskutil apfs addVolume CONTAINER APFS Scratch -reserve 10g              # guaranteed 10 GB
diskutil apfs setQuota VOLUME 80g ; diskutil apfs setReserve VOLUME 5g
diskutil apfs deleteVolume VOLUME                    # destroys data
```

- A case-sensitive volume inside a case-insensitive container works (verified: `touch Foo foo` gives two files). `CONTAINER` is the synthesized `diskN` shown as "APFS Container Reference", not the physical `diskNs1` partition (error -69630 otherwise).
- Quotas are enforced: writing 400 MB into a 300 MB-quota volume stopped at 286 MB with ENOSPC.
- Clones do not cross volumes, even in the same container: `cp -c` between them produced a full copy. Put a case-sensitive dev volume to use for the whole project (source, worktrees, caches), not half of it.
- Use cases: Linux-origin repos needing case sensitivity; a quota-capped volume so a runaway agent or build cannot fill the boot disk; a separate volume to `diskutil apfs eraseVolume` for a clean slate.

## Sparse disk images (portable, deletable, no partition changes)

```bash
hdiutil create -size 50g -fs APFS -type SPARSE -volname devimg dev.sparseimage   # grows on demand
# -type SPARSEBUNDLE: band files, better for backups/sync; -fs "Case-sensitive APFS" for case-sensitive
diskutil image attach --nobrowse dev.sparseimage          # new form; hdiutil attach is deprecated but works
diskutil eject /Volumes/devimg
hdiutil compact dev.sparseimage                           # reclaim space after deletions (image detached)
```

- A 2 GB sparse image starts at ~22 MB; it grows with writes and does not shrink until `hdiutil compact` (276 MB reclaimed in the test).
- The image is a separate volume: clones between it and the host volume are full copies.
- Additional volumes can be added to the image's container with `diskutil apfs addVolume` (as above).

### Shadow files: disposable writable overlay

```bash
hdiutil attach -nobrowse -shadow /path/run1.shadow golden.sparseimage   # writes go to run1.shadow
# ... run the agent / tests ...
diskutil eject /Volumes/devimg; rm /path/run1.shadow                    # golden image unchanged
```

Verified: writes with `-shadow` land in the shadow file and the base image is unchanged. **`diskutil image attach ... --shadow PATH` did not create a shadow file and wrote into the base image** on macOS 27.2.1; use `hdiutil attach -shadow` until that is fixed, and verify with `ls -l` on the shadow path before trusting it.

## RAM disks

```bash
DEV=$(hdiutil attach -nomount ram://2097152 | awk '{print $1}')   # 2097152 x 512-byte sectors = 1 GiB
diskutil erasevolume APFS ramdev "$DEV"                           # mounts at /Volumes/ramdev
diskutil eject "$DEV"                                             # contents are gone
# newer form: diskutil image attach --noMount ram://1g
```

- Contents vanish on eject or reboot; memory is wired for the disk's size.
- Separate volume, so no cloning from the main disk. Typical gains for builds on Apple-silicon SSDs are small; measure before adopting. Useful for test suites hammering temp files, or to keep scratch writes off the SSD.

## Sources

- diskutil addVolume / quota / reserve: https://eclecticlight.co/2024/04/22/apfs-command-tools/ , https://eclecticlight.co/2024/04/02/apfs-containers-and-volumes/ , https://support.apple.com/guide/disk-utility/dskua9e6a110
- RAM disks: https://eshop.macsales.com/blog/46348-how-to-create-and-use-a-ram-disk-with-your-mac-warnings-included/ , https://zummenix.github.io/posts/2023/derived-data-ram-disk
