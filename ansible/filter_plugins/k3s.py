"""Inventory and observed-state checks for the K3s installation."""

import ipaddress


def k3s_inventory_valid(groups, hostvars, bootstrap, pod_cidr, service_cidr, dns):
    servers = set(groups.get("control_plane", []))
    workers = set(groups.get("workers", []))
    if not servers or len(servers) % 2 == 0:
        raise ValueError(
            "Embedded etcd requires an odd, nonzero number of control-plane nodes."
        )
    if servers & workers or servers | workers != set(groups.get("k3s_cluster", [])):
        raise ValueError(
            "Every cluster host must have exactly one server or worker role."
        )
    if bootstrap not in servers:
        raise ValueError("The bootstrap host must be a control-plane node.")
    pods = ipaddress.IPv4Network(pod_cidr)
    services = ipaddress.IPv4Network(service_cidr)
    if pods.overlaps(services) or ipaddress.IPv4Address(dns) not in services:
        raise ValueError(
            "Pod and service CIDRs must not overlap; cluster DNS must be inside the service CIDR."
        )
    addresses = set()
    for hostname in sorted(servers | workers):
        address = ipaddress.IPv4Address(hostvars[hostname]["private_ip"])
        if address in addresses or address in pods or address in services:
            raise ValueError(
                "Node IPv4 addresses must be unique and outside pod/service CIDRs."
            )
        addresses.add(address)
    return True


def k3s_interface(interfaces, private_ip):
    matches = [
        interface["ifname"]
        for interface in interfaces
        if any(
            address.get("local") == private_ip
            for address in interface.get("addr_info", [])
        )
    ]
    if len(matches) != 1 or matches[0] == "lo":
        raise ValueError(
            "The inventory private IP must identify exactly one non-loopback interface."
        )
    return matches[0]


def k3s_bootstrap_safe(servers, hostvars, bootstrap):
    existing = {
        name
        for name in servers
        if hostvars[name]["k3s_existing_etcd"]["stat"]["exists"]
    }
    if existing and bootstrap not in existing:
        raise ValueError(
            "Other servers already contain etcd data. Select an existing server as k3s_bootstrap_host; refusing a second cluster."
        )
    return True


def k3s_nodes_ready(document, names, hostvars):
    nodes = {node["metadata"]["name"]: node for node in document.get("items", [])}
    if set(nodes) != set(names):
        return False
    for name, node in nodes.items():
        status = node.get("status", {})
        if not any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in status.get("conditions", [])
        ):
            return False
        if not any(
            address.get("type") == "InternalIP"
            and address.get("address") == hostvars[name]["private_ip"]
            for address in status.get("addresses", [])
        ):
            return False
        public_ip = hostvars[name].get("ansible_host")
        if public_ip and public_ip != hostvars[name]["private_ip"] and not any(
            address.get("type") == "ExternalIP"
            and address.get("address") == public_ip
            for address in status.get("addresses", [])
        ):
            return False
    return True


class FilterModule:
    def filters(self):
        return {
            "k3s_inventory_valid": k3s_inventory_valid,
            "k3s_interface": k3s_interface,
            "k3s_bootstrap_safe": k3s_bootstrap_safe,
            "k3s_nodes_ready": k3s_nodes_ready,
        }
