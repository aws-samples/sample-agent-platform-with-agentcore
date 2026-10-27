#!/usr/bin/env python3
"""Keep the kernel's hot files resident across a Fast Runtime snapshot.

A Fast Runtime (AgentCore platform V2) boots the container once, snapshots
it after /ping turns healthy and restores every session from that snapshot.
The page cache is not part of the snapshot, so a restored microVM reads each
image file from scratch the first time, at a few MB/s: on staging the
`claude` binary (278 MiB) took 42-58 s to bring up its UI and the first
`aws s3 ls` of the workspace restore 23 s, against 1.5 s and under 1 s on a
Standard Runtime.

Pages a live process has mapped are part of the snapshot. This process maps
every file under the given paths, touches each page, keeps the mappings for
its whole life and writes a ready file; start.sh starts the contract server
(and so /ping) only after that. On staging that brought `claude` to 0.85 s
and the whole wake-up to 6.4-6.7 s in six of six fresh sessions.

The mappings are file-backed and read-only: the kernel may reclaim them under
memory pressure, which only costs a slow read again. libc mmap through ctypes
rather than the mmap module, which dups the descriptor and holds it for the
mapping's life and so stops at RLIMIT_NOFILE (1024 in the microVM).

usage: pin_pages.py <ready-file> <path>...
"""
from __future__ import annotations

import collections
import ctypes
import ctypes.util
import json
import os
import sys
import time

PROT_READ = 0x1
MAP_SHARED = 0x01
MADV_WILLNEED = 3


def _libc():
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
    libc.madvise.restype = ctypes.c_int
    libc.madvise.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    return libc


class Pinner:
    def __init__(self) -> None:
        self.libc = _libc()
        self.page = os.sysconf("SC_PAGE_SIZE")
        self.failed = ctypes.c_void_p(-1).value
        self.files = 0
        self.bytes = 0
        self.skipped: collections.Counter[str] = collections.Counter()
        self.seen: set[tuple[int, int]] = set()

    def pin(self, path: str) -> None:
        try:
            st = os.stat(path)
            if not os.path.isfile(path) or st.st_size == 0:
                return
            # hard links (the claude binary has two names) are mapped once
            if (st.st_dev, st.st_ino) in self.seen:
                return
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
            try:
                addr = self.libc.mmap(None, st.st_size, PROT_READ, MAP_SHARED, fd, 0)
            finally:
                os.close(fd)
            if addr is None or addr == self.failed:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err))
            self.libc.madvise(addr, st.st_size, MADV_WILLNEED)
            touch = 0
            for off in range(0, st.st_size, self.page):
                touch ^= ctypes.c_ubyte.from_address(addr + off).value
            self.seen.add((st.st_dev, st.st_ino))
            self.files += 1
            self.bytes += st.st_size
        except (OSError, ValueError) as e:
            self.skipped[f"{type(e).__name__}: {getattr(e, 'strerror', None) or e}"] += 1

    def pin_tree(self, root: str) -> None:
        """A root given by name is resolved (the image links `claude` to its
        binary); inside a tree no symlink is followed, file or directory, so a
        link cannot pull files from outside the listed paths into memory."""
        if os.path.isfile(root):
            self.pin(os.path.realpath(root))
            return
        for d, _, names in os.walk(root):
            for name in names:
                p = os.path.join(d, name)
                if not os.path.islink(p):
                    self.pin(p)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    ready, roots = argv[0], argv[1:]
    t0 = time.monotonic()
    pinner = Pinner()
    for root in roots:
        # a listed path the image no longer has (a base-image Python bump
        # moves /usr/local/lib/python3.X) would otherwise pin nothing, quietly
        if not os.path.exists(root):
            pinner.skipped[f"missing path: {root}"] += 1
            continue
        pinner.pin_tree(root)
    stats = {
        "files": pinner.files,
        "mib": round(pinner.bytes / 2**20, 1),
        "seconds": round(time.monotonic() - t0, 1),
        "skipped": sum(pinner.skipped.values()),
        "skip_reasons": pinner.skipped.most_common(3),
    }
    print(f"[pin] {json.dumps(stats)}", flush=True)
    tmp = f"{ready}.tmp"
    with open(tmp, "w") as f:
        json.dump(stats, f)
    os.replace(tmp, ready)
    # the mappings live as long as this process: never return
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
