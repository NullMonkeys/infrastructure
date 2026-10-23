import copy
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "k3s_filters", REPOSITORY / "ansible/filter_plugins/k3s.py"
)
k3s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k3s)


class K3sTests(unittest.TestCase):
    def setUp(self):
        self.groups = {
            "control_plane": ["server-a", "server-b", "server-c"],
            "workers": ["worker-a"],
            "k3s_cluster": ["server-a", "server-b", "server-c", "worker-a"],
        }
        self.hosts = {
            name: {
                "private_ip": f"10.{index}.1.10",
                "k3s_existing_etcd": {"stat": {"exists": False}},
            }
            for index, name in enumerate(self.groups["k3s_cluster"], 1)
        }

    def validate(
        self,
        pod_cidr="10.42.0.0/16",
        service_cidr="10.43.0.0/16",
        dns="10.43.0.10",
        bootstrap="server-a",
    ):
        return k3s.k3s_inventory_valid(
            self.groups, self.hosts, bootstrap, pod_cidr, service_cidr, dns
        )

    def test_inventory_accepts_private_ipv4_and_distinct_roles(self):
        self.assertTrue(self.validate())

    def test_invalid_topologies_and_bootstrap_are_rejected(self):
        with self.assertRaises(ValueError):
            self.validate(bootstrap="worker-a")
        self.groups["control_plane"].remove("server-c")
        with self.assertRaises(ValueError):
            self.validate()
        self.groups["control_plane"].append("server-c")
        self.groups["workers"].append("server-a")
        with self.assertRaises(ValueError):
            self.validate()

    def test_overlapping_addresses_and_ipv6_are_rejected(self):
        for kwargs in (
            {"service_cidr": "10.42.0.0/16"},
            {"dns": "10.99.0.10"},
            {"pod_cidr": "10.1.0.0/16"},
            {"pod_cidr": "fd00::/64"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.validate(**kwargs)
        self.hosts["worker-a"]["private_ip"] = self.hosts["server-a"]["private_ip"]
        with self.assertRaises(ValueError):
            self.validate()

    def test_flannel_interface_is_selected_by_private_address(self):
        interfaces = [
            {"ifname": "lo", "addr_info": [{"local": "127.0.0.1"}]},
            {"ifname": "ens3", "addr_info": [{"local": "10.1.1.10"}]},
        ]
        self.assertEqual(k3s.k3s_interface(interfaces, "10.1.1.10"), "ens3")
        for address in ("10.1.1.99", "127.0.0.1"):
            with self.assertRaises(ValueError):
                k3s.k3s_interface(interfaces, address)
        with self.assertRaises(ValueError):
            k3s.k3s_interface(interfaces + [interfaces[1]], "10.1.1.10")

    def test_existing_cluster_cannot_be_accidentally_rebootstrapped(self):
        self.assertTrue(
            k3s.k3s_bootstrap_safe(self.groups["control_plane"], self.hosts, "server-a")
        )
        self.hosts["server-b"]["k3s_existing_etcd"]["stat"]["exists"] = True
        with self.assertRaises(ValueError):
            k3s.k3s_bootstrap_safe(self.groups["control_plane"], self.hosts, "server-a")
        self.assertTrue(
            k3s.k3s_bootstrap_safe(self.groups["control_plane"], self.hosts, "server-b")
        )

    def test_readiness_requires_all_nodes_ready_with_private_internal_ips(self):
        nodes = {
            "items": [
                {
                    "metadata": {"name": name},
                    "status": {
                        "addresses": [
                            {"type": "InternalIP", "address": host["private_ip"]}
                        ],
                        "conditions": [{"type": "Ready", "status": "True"}],
                    },
                }
                for name, host in self.hosts.items()
            ]
        }
        self.assertTrue(
            k3s.k3s_nodes_ready(nodes, self.groups["k3s_cluster"], self.hosts)
        )
        for mutate in (
            lambda value: value["items"].pop(),
            lambda value: value["items"][0]["status"]["conditions"][0].update(
                status="False"
            ),
            lambda value: value["items"][0]["status"]["addresses"][0].update(
                address="203.0.113.10"
            ),
        ):
            changed = copy.deepcopy(nodes)
            mutate(changed)
            self.assertFalse(
                k3s.k3s_nodes_ready(changed, self.groups["k3s_cluster"], self.hosts)
            )

        # Public DNS must wait until K3s advertises the inventory public IP.
        self.hosts["server-a"]["ansible_host"] = "203.0.113.10"
        self.assertFalse(
            k3s.k3s_nodes_ready(nodes, self.groups["k3s_cluster"], self.hosts)
        )
        addresses = next(
            node["status"]["addresses"]
            for node in nodes["items"]
            if node["metadata"]["name"] == "server-a"
        )
        addresses.append({"type": "ExternalIP", "address": "203.0.113.99"})
        self.assertFalse(
            k3s.k3s_nodes_ready(nodes, self.groups["k3s_cluster"], self.hosts)
        )
        addresses[-1]["address"] = "203.0.113.10"
        self.assertTrue(
            k3s.k3s_nodes_ready(nodes, self.groups["k3s_cluster"], self.hosts)
        )

    def test_real_ansible_templates_enable_private_etcd_metrics_and_hide_tokens(self):
        command = os.environ.get("ANSIBLE_INVENTORY_BIN")
        if not command:
            self.skipTest("Set ANSIBLE_INVENTORY_BIN to render the real K3s templates.")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.json"
            hosts = dict(self.hosts, localhost={})
            inventory.write_text(
                json.dumps(
                    {
                        "all": {
                            "hosts": hosts,
                            "children": {
                                name: {"hosts": {hostname: {} for hostname in members}}
                                for name, members in self.groups.items()
                            },
                        }
                    }
                )
            )
            tasks = []
            template = REPOSITORY / "ansible/roles/k3s/templates/config.yaml.j2"
            image_gc = str(root / "image-gc.conf")
            tasks.extend(
                [
                    {
                        "ansible.builtin.template": {
                            "src": str(template.with_name("image-gc.conf.j2")),
                            "dest": image_gc,
                            "mode": "0644",
                        }
                    },
                    {
                        "ansible.builtin.set_fact": {
                            "image_gc": "{{ lookup('file', '"
                            + image_gc
                            + "') | from_yaml }}"
                        }
                    },
                    {
                        "ansible.builtin.assert": {
                            "that": [
                                "image_gc.apiVersion == 'kubelet.config.k8s.io/v1beta1'",
                                "image_gc.kind == 'KubeletConfiguration'",
                                "image_gc.imageGCHighThresholdPercent == 70",
                                "image_gc.imageGCLowThresholdPercent == 60",
                                "image_gc.imageMinimumGCAge == '10m'",
                                "image_gc.imageMaximumGCAge == '24h'",
                            ]
                        }
                    },
                ]
            )
            for mode, bootstrap, name in (
                ("server", "localhost", "bootstrap"),
                ("server", "server-a", "server"),
                ("agent", "server-a", "agent"),
            ):
                path = str(root / f"{name}.yaml")
                tasks.extend(
                    [
                        {
                            "ansible.builtin.template": {
                                "src": str(template),
                                "dest": path,
                                "mode": "0600",
                            },
                            "vars": {"k3s_mode": mode, "k3s_bootstrap_host": bootstrap},
                        },
                        {
                            "ansible.builtin.set_fact": {
                                "rendered": "{{ lookup('file', '"
                                + path
                                + "') | from_yaml }}"
                            }
                        },
                        {
                            "ansible.builtin.assert": {
                                "that": [
                                    "rendered['node-ip'] == '10.1.1.10'",
                                    "rendered['node-external-ip'] == '203.0.113.10'",
                                    "rendered['flannel-iface'] == 'ens3'",
                                    "'token' not in rendered",
                                ]
                            }
                        },
                    ]
                )
                if mode == "server":
                    tasks.append(
                        {
                            "ansible.builtin.assert": {
                                "that": [
                                    "rendered['etcd-expose-metrics'] == true",
                                    "rendered['flannel-backend'] == 'vxlan'",
                                    "rendered['cluster-cidr'] == '10.42.0.0/16'",
                                    "rendered['secrets-encryption'] == true",
                                    "rendered['write-kubeconfig-mode'] == '0600'",
                                ]
                            }
                        }
                    )
                else:
                    tasks.append(
                        {
                            "ansible.builtin.assert": {
                                "that": [
                                    "'etcd-expose-metrics' not in rendered",
                                    "'cluster-init' not in rendered",
                                ]
                            }
                        }
                    )
                if name == "bootstrap":
                    tasks.append(
                        {
                            "ansible.builtin.assert": {
                                "that": [
                                    "rendered['cluster-init'] == true",
                                    "'server' not in rendered",
                                    "'token-file' not in rendered",
                                ]
                            }
                        }
                    )
                else:
                    tasks.append(
                        {
                            "ansible.builtin.assert": {
                                "that": [
                                    "rendered['server'] == 'https://10.1.1.10:6443'",
                                    "rendered['token-file'] == '/etc/rancher/k3s/join-token'",
                                    "'cluster-init' not in rendered",
                                ]
                            }
                        }
                    )
            manifest = str(root / "network.yaml")
            tasks.extend(
                [
                    {
                        "ansible.builtin.template": {
                            "src": str(template.with_name("network-check.yaml.j2")),
                            "dest": manifest,
                            "mode": "0600",
                        }
                    },
                    {
                        "ansible.builtin.set_fact": {
                            "workload": "{{ lookup('file', '"
                            + manifest
                            + "') | from_yaml_all | list }}"
                        }
                    },
                    {
                        "ansible.builtin.assert": {
                            "that": [
                                "workload | length == 3",
                                "workload[2].kind == 'DaemonSet'",
                                "workload[2].spec.template.spec.automountServiceAccountToken == false",
                                "workload[2].spec.template.spec.securityContext.runAsNonRoot == true",
                                "workload[2].spec.template.spec.containers[0].image == k3s_network_check_image",
                            ]
                        }
                    },
                ]
            )
            playbook = root / "render.json"
            playbook.write_text(
                json.dumps(
                    [
                        {
                            "hosts": "localhost",
                            "connection": "local",
                            "gather_facts": False,
                            "vars_files": [str(REPOSITORY / "ansible/vars/k3s.yml")],
                            "vars": {
                                "private_ip": "10.1.1.10",
                                "ansible_host": "203.0.113.10",
                                "k3s_private_interface": "ens3",
                                "k3s_bootstrap_ip": "10.1.1.10",
                            },
                            "tasks": tasks,
                        }
                    ]
                )
            )
            env = dict(
                os.environ,
                ANSIBLE_CONFIG=str(REPOSITORY / "ansible/ansible.cfg"),
                ANSIBLE_LOCAL_TEMP=str(root / "tmp"),
            )
            result = subprocess.run(
                [
                    str(Path(command).with_name("ansible-playbook")),
                    "-i",
                    str(inventory),
                    str(playbook),
                ],
                check=False,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
