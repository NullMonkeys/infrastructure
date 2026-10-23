"""Read-only verification of kernel-level IPv6 disabling on a prepared host."""

import errno
import socket
from pathlib import Path


def main():
    options = [
        option
        for option in Path("/proc/cmdline").read_text().split()
        if option.startswith("ipv6.disable=")
    ]
    if not options or options[-1] != "ipv6.disable=1":
        raise SystemExit("The running kernel was not booted with ipv6.disable=1.")
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError as error:
        if error.errno != errno.EAFNOSUPPORT:
            raise
    else:
        probe.close()
        raise SystemExit(
            "IPv6 sockets are still available; kernel-level disabling did not take effect."
        )
    print("IPv6 is disabled at kernel level; IPv6 sockets are unavailable.")


if __name__ == "__main__":
    main()
