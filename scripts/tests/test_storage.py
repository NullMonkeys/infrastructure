import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "storage_filters",
    Path(__file__).resolve().parents[2] / "ansible/filter_plugins/storage.py",
)
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.target = {
            "device": "/dev/oracleoci/oraclevdb",
            "attachment_type": "paravirtualized",
            "is_read_only": False,
            "mount_path": "/var/lib/longhorn",
        }
        self.disk = {
            "name": "/dev/sdb",
            "type": "disk",
            "fstype": None,
            "uuid": None,
            "mountpoints": [None],
            "ro": False,
        }

    def validate(
        self, disk=None, signatures=None, mounted_uuid="", mounted=False, entries=0
    ):
        return storage.storage_device_valid(
            {"blockdevices": [self.disk if disk is None else disk]},
            {"signatures": [] if signatures is None else signatures},
            "/var/lib/longhorn",
            mounted_uuid,
            mounted,
            entries,
        )

    def test_blank_data_disk_can_be_initialized(self):
        self.assertTrue(self.validate())
        self.assertTrue(storage.storage_targets_valid([self.target]))

    def test_existing_ext4_is_reused_at_the_same_mount(self):
        self.disk.update(
            fstype="ext4", uuid="existing-uuid", mountpoints=["/var/lib/longhorn"]
        )
        self.assertTrue(
            self.validate(
                signatures=[{"type": "ext4"}],
                mounted_uuid="existing-uuid",
                mounted=True,
            )
        )

    def test_partitioned_read_only_non_disk_and_root_devices_are_rejected(self):
        for override in (
            {"children": [{"type": "part"}]},
            {"ro": True},
            {"type": "part"},
            {"mountpoints": ["/"]},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.validate(dict(self.disk, **override))

    def test_existing_data_and_probe_disagreement_are_rejected(self):
        for filesystem, signatures in (
            ("xfs", [{"type": "xfs"}]),
            (None, [{"type": "gpt"}]),
            (None, [{"type": "linux_raid_member"}]),
            (None, [{"type": "ext4"}]),
            ("ext4", []),
        ):
            with (
                self.subTest(filesystem=filesystem, signatures=signatures),
                self.assertRaises(ValueError),
            ):
                self.validate(dict(self.disk, fstype=filesystem), signatures)

    def test_missing_probe_data_is_rejected(self):
        incomplete = dict(self.disk)
        del incomplete["mountpoints"]
        with self.assertRaises(ValueError):
            self.validate(incomplete)
        with self.assertRaises(ValueError):
            storage.storage_device_valid(
                {"blockdevices": []},
                {"signatures": []},
                "/var/lib/longhorn",
                "",
                False,
                0,
            )
        with self.assertRaisesRegex(TypeError, "existing signatures"):
            storage.storage_device_valid(
                {"blockdevices": [self.disk]}, {}, "/var/lib/longhorn", "", False, 0
            )

    def test_other_mounts_and_nonempty_directories_are_rejected(self):
        for kwargs in (
            {"mounted_uuid": "another-disk", "mounted": True},
            {"mounted": True},
            {"entries": 1},
            {"disk": dict(self.disk, mountpoints=["/mnt/other"])},
            {"disk": dict(self.disk, mountpoints=["/var/lib/longhorn"])},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.validate(**kwargs)

    def test_ext4_without_uuid_is_rejected(self):
        with self.assertRaises(ValueError):
            self.validate(dict(self.disk, fstype="ext4"), [{"type": "ext4"}])

    def test_boot_and_unstable_device_names_are_rejected(self):
        for device in (
            "/dev/oracleoci/oraclevda",
            "/dev/sdb",
            "/dev/oracleoci/oraclevdb1",
            None,
        ):
            with self.subTest(device=device), self.assertRaises(ValueError):
                storage.storage_targets_valid([dict(self.target, device=device)])

    def test_unsafe_and_overlapping_mount_paths_are_rejected(self):
        for path in (
            "/",
            "/boot",
            "/var/lib",
            "/mnt/../boot",
            "/mnt/disk/",
            "relative",
            "/mnt//disk",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                storage.storage_targets_valid([dict(self.target, mount_path=path)])
        for path in ("/var/lib/longhorn", "/var/lib/longhorn/nested"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                storage.storage_targets_valid(
                    [
                        self.target,
                        dict(
                            self.target,
                            device="/dev/oracleoci/oraclevdc",
                            mount_path=path,
                        ),
                    ]
                )

    def test_distinct_devices_and_mount_paths_are_allowed(self):
        second = dict(
            self.target, device="/dev/oracleoci/oraclevdc", mount_path="/mnt/extra"
        )
        self.assertTrue(storage.storage_targets_valid([self.target, second]))
        with self.assertRaises(ValueError):
            storage.storage_targets_valid(
                [self.target, dict(second, device=self.target["device"])]
            )

    def test_read_only_and_other_attachment_modes_are_rejected(self):
        for override in ({"is_read_only": True}, {"attachment_type": "iscsi"}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                storage.storage_targets_valid([dict(self.target, **override)])

    def test_ansible_evaluates_the_actual_storage_guards_without_touching_disks(self):
        command = os.environ.get("ANSIBLE_INVENTORY_BIN")
        if not command:
            self.skipTest(
                "Set ANSIBLE_INVENTORY_BIN to evaluate the real Ansible storage guards."
            )
        repository = Path(__file__).resolve().parents[2]
        variables = {
            "ansible_facts": {"distribution": "Ubuntu"},
            "block_volumes": {"data": self.target},
            "storage_mounts": {},
            "storage_targets": [self.target],
            "storage_volume": self.target,
            "storage_layout": {"stdout": json.dumps({"blockdevices": [self.disk]})},
            "storage_signatures": {"stdout": json.dumps({"signatures": []})},
            "storage_current_mount": {"stdout": "", "rc": 1},
            "storage_directory": {"stat": {"exists": False}},
            "storage_destination": {"stdout": "/var/lib/longhorn"},
            "storage_contents": {"matched": 0},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guards.json"
            env = dict(
                os.environ,
                ANSIBLE_CONFIG=str(repository / "ansible/ansible.cfg"),
                ANSIBLE_FILTER_PLUGINS=str(repository / "ansible/filter_plugins"),
                ANSIBLE_LOCAL_TEMP=str(Path(directory) / "tmp"),
            )
            args = [
                str(Path(command).with_name("ansible-playbook")),
                "-i",
                "localhost,",
                "--tags",
                "storage_validation",
                str(path),
            ]
            for invalid in (False, True):
                if invalid:
                    variables["storage_layout"]["stdout"] = json.dumps(
                        {"blockdevices": [dict(self.disk, fstype="xfs")]}
                    )
                    variables["storage_signatures"]["stdout"] = json.dumps(
                        {"signatures": [{"type": "xfs"}]}
                    )
                tasks = [
                    {
                        "ansible.builtin.import_tasks": str(
                            repository / "ansible/roles/node/tasks" / filename
                        )
                    }
                    for filename in ("main.yml", "storage.yml")
                ]
                path.write_text(
                    json.dumps(
                        [
                            {
                                "hosts": "all",
                                "gather_facts": False,
                                "connection": "local",
                                "vars": variables,
                                "tasks": tasks,
                            }
                        ]
                    )
                )
                result = subprocess.run(
                    args, check=False, env=env, capture_output=True, text=True
                )
                if invalid:
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(
                        "Refusing to replace an existing non-ext4 filesystem",
                        result.stdout + result.stderr,
                    )
                else:
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )


if __name__ == "__main__":
    unittest.main()
