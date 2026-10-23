import errno
import importlib.util
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

FILES = Path(__file__).resolve().parents[2] / "ansible/roles/node/files"
spec = importlib.util.spec_from_file_location("check_ipv6", FILES / "check_ipv6.py")
check_ipv6 = importlib.util.module_from_spec(spec)  # ty: ignore[invalid-argument-type]
spec.loader.exec_module(check_ipv6)  # ty: ignore[unresolved-attribute]


class IPv6Tests(unittest.TestCase):
    def source_boot_config(self, linux, defaults, repeats=1):
        shell = 'for iteration in $(seq 1 "$2"); do . "$1"; done; printf "%s\\n%s\\n" "$GRUB_CMDLINE_LINUX" "$GRUB_CMDLINE_LINUX_DEFAULT"'
        result = subprocess.run(
            ["sh", "-c", shell, "_", str(FILES / "disable-ipv6.cfg"), str(repeats)],
            env=dict(
                os.environ,
                GRUB_CMDLINE_LINUX=linux,
                GRUB_CMDLINE_LINUX_DEFAULT=defaults,
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.splitlines()

    def test_boot_option_replaces_conflicts_and_preserves_other_flags(self):
        for linux, defaults in (
            ("", ""),
            ("console=ttyS0 root=UUID=example", "quiet splash"),
            ("ipv6.disable=0 console=ttyS0 ipv6.disable=1", "quiet ipv6.disable=0"),
            ("myipv6.disable=0", "foo=ipv6.disable=0"),
        ):
            with self.subTest(linux=linux, defaults=defaults):
                actual = self.source_boot_config(linux, defaults)
                combined = shlex.split(" ".join(actual))
                self.assertEqual(
                    [
                        option
                        for option in combined
                        if option.startswith("ipv6.disable=")
                    ],
                    ["ipv6.disable=1"],
                )
                expected = [
                    option
                    for option in shlex.split(linux + " " + defaults)
                    if not option.startswith("ipv6.disable=")
                ]
                self.assertEqual(
                    [
                        option
                        for option in combined
                        if not option.startswith("ipv6.disable=")
                    ],
                    expected,
                )
                self.assertEqual(
                    actual, self.source_boot_config(linux, defaults, repeats=2)
                )

    def test_kernel_verification_requires_the_effective_boot_flag(self):
        for cmdline in (
            "root=UUID=example",
            "ipv6.disable=0",
            "ipv6.disable=1 ipv6.disable=0",
        ):
            with (
                self.subTest(cmdline=cmdline),
                patch.object(check_ipv6.Path, "read_text", return_value=cmdline),
                patch.object(check_ipv6.socket, "socket") as probe,
            ):
                with self.assertRaises(SystemExit):
                    check_ipv6.main()
                probe.assert_not_called()

    def test_kernel_verification_rejects_working_ipv6_sockets(self):
        with (
            patch.object(check_ipv6.Path, "read_text", return_value="ipv6.disable=1"),
            patch.object(check_ipv6.socket, "socket") as probe,
        ):
            with self.assertRaises(SystemExit):
                check_ipv6.main()
            probe.return_value.close.assert_called_once()

    def test_kernel_verification_accepts_only_unsupported_address_family(self):
        with (
            patch.object(check_ipv6.Path, "read_text", return_value="ipv6.disable=1"),
            patch.object(
                check_ipv6.socket,
                "socket",
                side_effect=OSError(errno.EAFNOSUPPORT, "disabled"),
            ),
            patch("builtins.print"),
        ):
            check_ipv6.main()
        with (
            patch.object(check_ipv6.Path, "read_text", return_value="ipv6.disable=1"),
            patch.object(
                check_ipv6.socket,
                "socket",
                side_effect=OSError(errno.EACCES, "unrelated error"),
            ),
            self.assertRaises(OSError),
        ):
            check_ipv6.main()

    def test_ansible_reboot_decision_without_changing_or_rebooting_hosts(self):
        inventory_command = os.environ.get("ANSIBLE_INVENTORY_BIN")
        if not inventory_command:
            self.skipTest(
                "Set ANSIBLE_INVENTORY_BIN to evaluate the Ansible reboot decision."
            )
        repository = FILES.parents[3]
        plays = []
        for cmdline, required in [
            ("root=UUID=example", True),
            ("ipv6.disable=1", False),
            ("ipv6.disable=1 ipv6.disable=0", True),
            ("fooipv6.disable=1", True),
        ]:
            plays.append(
                {
                    "hosts": "all",
                    "gather_facts": False,
                    "connection": "local",
                    "vars": {"ipv6_cmdline": {"stdout": cmdline}},
                    "tasks": [
                        {
                            "ansible.builtin.import_tasks": str(
                                FILES.parent / "tasks/ipv6.yml"
                            )
                        },
                        {
                            "ansible.builtin.assert": {
                                "that": [f"ipv6_reboot_required == {required}"]
                            },
                            "tags": ["ipv6_decision"],
                        },
                    ],
                }
            )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decision.json"
            path.write_text(json.dumps(plays))
            env = dict(
                os.environ,
                ANSIBLE_CONFIG=str(repository / "ansible/ansible.cfg"),
                ANSIBLE_LOCAL_TEMP=str(Path(directory) / "tmp"),
            )
            result = subprocess.run(
                [
                    str(Path(inventory_command).with_name("ansible-playbook")),
                    "-i",
                    "localhost,",
                    "--tags",
                    "ipv6_decision",
                    str(path),
                ],
                check=False,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
