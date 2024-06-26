# -*- coding=utf-8 -*-
import collections
import enum
import ipaddress
import logging
import os
import socket

import bidict
from pyroute2 import IPRoute
from pyroute2.netlink.exceptions import NetlinkDumpInterrupted

from .address.ipv6 import ipv6_netmask_to_prefixlen
from .address.types import AddressFamily

logger = logging.getLogger(__name__)

__all__ = ["Route", "RouteFlags", "RoutingTable", "RouteTable", "IPRoute", "RuleTable"]

DEFAULT_TABLE_ID = 254  # This is the default table named as "main" and most of what we do happens here
ip = IPRoute()


def get_iproute2_rt_tables_paths(type="read"):
    # In new versions, it reads from both /usr and /etc
    # and the /etc one overrides file in /usr
    # type read: read locations, package-shipped default and /etc
    # type write: write locations, to /etc only
    rt_tables_path = "iproute2/rt_tables"
    # iproute2 location order by version descending
    defpaths = ["/usr/share", "/usr/lib", "/usr"]
    local_full_path = "/etc/iproute2/rt_tables"

    def isAbove650():
        for p_path in defpaths:
            f_p = os.path.join(p_path, rt_tables_path)
            if os.path.exists(f_p):
                return True, f_p
        return False, None

    if not os.path.exists(local_full_path):
        # create if not exist
        os.makedirs(os.path.dirname(local_full_path), exist_ok=True)
        with open(local_full_path, mode="a"):
            pass
    if type == "write":
        return [local_full_path]
    isNewVer, full_path = isAbove650()
    if not isNewVer:
        return [local_full_path]
    return [full_path, local_full_path]


class Route:
    def __init__(
        self, network, netmask, gateway=None, interface=None, flags=None,
        table_id=None, preferred_source=None, scope=None,
    ):
        self.network = ipaddress.ip_address(network)
        self.netmask = ipaddress.ip_address(netmask)
        self.gateway = ipaddress.ip_address(gateway) if gateway else None
        self.interface = interface or None
        self.flags = flags or set()
        self.table_id = table_id
        self.scope = scope
        self.preferred_source = preferred_source

    def asdict(self):
        return {
            'network': str(self.network),
            'netmask': str(self.netmask),
            'gateway': str(self.gateway) if self.gateway else None,
            'interface': self.interface,
            'flags': [x.name for x in self.flags],
            'table_id': self.table_id,
            'scope': self.scope,
            'preferred_source': self.preferred_source,
        }

    @property
    def af(self):
        if self.network.version == 4:
            return AddressFamily.INET

        if self.network.version == 6:
            return AddressFamily.INET6

        return None

    def __eq__(self, other):
        if not isinstance(other, Route):
            return False

        return (
            self.network == other.network and
            self.netmask == other.netmask and
            self.gateway == other.gateway
        )

    def __hash__(self):
        return hash((self.network, self.netmask, self.gateway))


class RouteTable:
    def __init__(self, table_id, table_name):
        self.table_id = table_id
        self.table_name = table_name

    def create(self):
        paths = get_iproute2_rt_tables_paths(type="write")
        assert len(paths) == 1
        with open(paths[0], "a+") as f:
            f.write(f'{self.table_id} {self.table_name}\n')

    @property
    def exists(self):
        return self.table_name in RoutingTable().routing_tables

    @property
    def is_reserved(self):
        return self.table_id in (255, 254, 253, 0)

    @property
    def routes(self):
        return RoutingTable().routes_internal(self.table_id)

    def flush_routes(self):
        ip.flush_routes(table=self.table_id)

    def flush_rules(self):
        ip.flush_rules(table=self.table_id)

    def __eq__(self, other):
        return self.table_id == other.table_id

    def asdict(self):
        return {
            "id": self.table_id,
            "name": self.table_name,
            "routes": [r.asdict() for r in self.routes],
        }


class RouteFlags(enum.IntEnum):
    # include/uapi/linux/route.h

    UP = 0x0001
    GATEWAY = 0x0002
    HOST = 0x0004
    REJECT = 0x0200
    DYNAMIC = 0x0010
    MODIFIED = 0x0020
    # DONE = defs.RTF_DONE
    # XRESOLVE = defs.RTF_XRESOLVE
    # LLINFO = defs.RTF_LLINFO
    # LLDATA = defs.RTF_LLDATA
    STATIC = 0x8000  # no-op
    # BLACKHOLE = defs.RTF_BLACKHOLE
    # PROTO1 = defs.RTF_PROTO1
    # PROTO2 = defs.RTF_PROTO2
    # PROTO3 = defs.RTF_PROTO3
    # PINNED = defs.RTF_PINNED
    # LOCAL = defs.RTF_LOCAL
    # BROADCAST = defs.RTF_BROADCAST
    # MULTICAST = defs.RTF_MULTICAST
    # STICKY = defs.RTF_STICKY


RTM_F_CLONED = 0x200


class RoutingTable:
    @property
    def routes(self):
        return self.routes_internal()

    def routes_internal(self, table_filter=None):
        interfaces = self._interfaces()

        result = []
        for r in ip.get_routes(table=table_filter):
            if r["flags"] & RTM_F_CLONED:
                continue

            attrs = dict(r["attrs"])

            if "RTA_DST" in attrs:
                network = ipaddress.ip_address(attrs["RTA_DST"])
                netmask = ipaddress.ip_network(f"{attrs['RTA_DST']}/{r['dst_len']}").netmask
            else:
                network, netmask = {
                    socket.AF_INET: (ipaddress.IPv4Address(0), ipaddress.IPv4Address(0)),
                    socket.AF_INET6: (ipaddress.IPv6Address(0), ipaddress.IPv6Address(0)),
                }[r["family"]]

            result.append(Route(
                network,
                netmask,
                ipaddress.ip_address(attrs["RTA_GATEWAY"]) if "RTA_GATEWAY" in attrs else None,
                interfaces[attrs["RTA_OIF"]] if "RTA_OIF" in attrs and attrs["RTA_OIF"] in interfaces else None,
                table_id=attrs["RTA_TABLE"],
                preferred_source=attrs.get("RTA_PREFSRC"),
                scope=r["scope"],
            ))

        return result

    @property
    def routing_tables(self):
        iproute2_rt_tables_paths = get_iproute2_rt_tables_paths(type="read")
        content = []
        for full_p in iproute2_rt_tables_paths:
            with open(full_p, "r") as f:
                content.extend(f.readlines())

        content = list(
            filter(
                lambda v: v.strip()
                and not v.startswith("#")
                and v.split()[0].strip().isdigit(),
                content
            )
        )
        if len(content) == 0:
            return {}

        mapping = list(
            map(
                lambda v: {
                    "id": int(v.split()[0].strip()),
                    "name": v.split()[1].strip(),
                },
                content
            )
        )
        deduped = [
            ii
            for idx, ii in enumerate(mapping)
            if ii["id"] not in map(lambda v: v["id"], mapping[idx+1:])
        ]

        return {t["name"]: RouteTable(t["id"], t["name"]) for t in deduped}

    @property
    def default_route_ipv4(self):
        f = list(filter(lambda r: int(r.network) == 0 and int(r.netmask) == 0 and r.af == AddressFamily.INET,
                        self.routes_internal(DEFAULT_TABLE_ID)))
        return f[0] if len(f) > 0 else None

    @property
    def default_route_ipv6(self):
        f = list(filter(lambda r: int(r.network) == 0 and int(r.netmask) == 0 and r.af == AddressFamily.INET6,
                        self.routes_internal(DEFAULT_TABLE_ID)))
        return f[0] if len(f) > 0 else None

    def add(self, route):
        self._op("add", route)

    def change(self, route):
        self._op("set", route)

    def delete(self, route):
        self._op("delete", route)

    def _interfaces(self):
        return bidict.bidict({i["index"]: dict(i["attrs"]).get("IFLA_IFNAME") for i in self._ip_links()})

    def _ip_links(self):
        retries = 5
        while True:
            try:
                return ip.get_links()
            except NetlinkDumpInterrupted:
                retries -= 1
                if retries <= 0:
                    raise

    def _op(self, op, route):
        if route.netmask.version == 4:
            prefixlen = ipaddress.ip_network(f"{route.network}/{route.netmask}").prefixlen
        elif route.netmask.version == 6:
            prefixlen = ipv6_netmask_to_prefixlen(str(route.netmask))
        else:
            raise RuntimeError()

        kwargs = dict(dst=f"{route.network}/{prefixlen}", gateway=str(route.gateway) if route.gateway else None)
        for key, value in map(
            lambda v: [v[0], v[1]() if isinstance(v[1], collections.abc.Callable) else v[1]],
            filter(
                lambda v: v[2] if len(v) == 3 else v[1], (
                    ("oif", lambda: self._interfaces().inv[route.interface], route.interface is not None),
                    ("table", route.table_id),
                    ("scope", route.scope),
                    ("prefsrc", route.preferred_source),
                )
            )
        ):
            kwargs[key] = value

        ip.route(op, **kwargs)


class RuleTable:

    @property
    def rules(self):
        rules = []
        tables = {t.table_id: t for t in RoutingTable().routing_tables.values()}
        for rule in filter(lambda r: r.get('attrs'), ip.get_rules()):
            attrs = dict(rule['attrs'])
            if not all(k in attrs for k in ('FRA_TABLE', 'FRA_PRIORITY')) or attrs.get('FRA_TABLE') not in tables:
                continue

            rules.append({
                'table': tables[attrs['FRA_TABLE']],
                'priority': attrs['FRA_PRIORITY'],
                'source_addr': attrs.get('FRA_SRC'),
            })

        return rules

    def add_rule(self, table_id, priority, source_addr=None):
        kwargs = {'table': table_id, 'priority': priority}
        if source_addr:
            kwargs['src'] = source_addr
        ip.rule('add', **kwargs)

    def delete_rule(self, priority):
        ip.rule('delete', priority=priority)

    def rule_exists(self, priority):
        return any(priority == rule['priority'] for rule in self.rules)
