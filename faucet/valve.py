"""Implementation of Valve learning layer 2/3 switch."""

# Copyright (C) 2013 Nippon Telegraph and Telephone Corporation.
# Copyright (C) 2015 Brad Cowie, Christopher Lorier and Joe Stringer.
# Copyright (C) 2015 Research and Education Advanced Network New Zealand Ltd.
# Copyright (C) 2015--2019 The Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import logging

from collections import defaultdict, deque

from faucet import tfm_pipeline
from faucet import valve_acl
from faucet import valve_flood
from faucet import valve_host
from faucet import valve_of
from faucet import valve_packet
from faucet import valve_route
from faucet import valve_table
from faucet import valve_util
from faucet import valve_pipeline

from faucet.vlan import NullVLAN, OFVLAN


class ValveLogger:
    """Logger for a Valve that adds DP ID."""

    def __init__(self, logger, dp_id, dp_name):
        self.logger = logger
        self.dp_id = dp_id
        self.dp_name = dp_name

    def _dpid_prefix(self, log_msg):
        """Add DP ID prefix to log message."""
        return ' '.join((valve_util.dpid_log(self.dp_id), self.dp_name, log_msg))

    def debug(self, log_msg):
        """Log debug level message."""
        self.logger.debug(self._dpid_prefix(log_msg))

    def info(self, log_msg):
        """Log info level message."""
        self.logger.info(self._dpid_prefix(log_msg))

    def error(self, log_msg):
        """Log error level message."""
        self.logger.error(self._dpid_prefix(log_msg))

    def warning(self, log_msg):
        """Log warning level message."""
        self.logger.warning(self._dpid_prefix(log_msg))


class Valve:
    """Generates the messages to configure a datapath as a l2 learning switch.

    Vendor specific implementations may require sending configuration flows.
    This can be achieved by inheriting from this class and overwriting the
    function switch_features.
    """

    __slots__ = [
        'dot1x',
        'dp',
        'flood_manager',
        'host_manager',
        'pipeline',
        'acl_manager',
        'logger',
        'logname',
        'metrics',
        'notifier',
        'ofchannel_logger',
        'recent_ofmsgs',
        '_last_advertise_sec',
        '_last_fast_advertise_sec',
        '_last_packet_in_sec',
        '_last_pipeline_flows',
        '_packet_in_count_sec',
        '_port_highwater',
        '_route_manager_by_eth_type',
        '_route_manager_by_ipv',
    ]

    DEC_TTL = True
    USE_BARRIERS = True
    STATIC_TABLE_IDS = False
    GROUPS = True


    def __init__(self, dp, logname, metrics, notifier, dot1x):
        self.dot1x = dot1x
        self.dp = dp
        self.logname = logname
        self.metrics = metrics
        self.notifier = notifier
        self.ofchannel_logger = None
        self.logger = None
        self.recent_ofmsgs = deque(maxlen=32)
        self._last_pipeline_flows = []
        self._packet_in_count_sec = None
        self._last_packet_in_sec = None
        self._last_advertise_sec = None
        self._last_fast_advertise_sec = None
        self.dp_init()

    def _port_vlan_labels(self, port, vlan):
        return dict(self.dp.port_labels(port.number), vlan=vlan.vid)

    def _inc_var(self, var, labels=None, val=1):
        if labels is None:
            labels = self.dp.base_prom_labels()
        self.metrics.inc_var(var, labels, val)

    def _set_var(self, var, val, labels=None):
        if labels is None:
            labels = self.dp.base_prom_labels()
        metrics_var = getattr(self.metrics, var)
        metrics_var.labels(**labels).set(val)

    def close_logs(self):
        """Explicitly close any active loggers."""
        if self.logger is not None:
            valve_util.close_logger(self.logger.logger)
        valve_util.close_logger(self.ofchannel_logger)

    def dp_init(self, new_dp=None):
        """Initialize datapath state at connection/re/config time."""
        if new_dp:
            new_dp.clone_dyn_state(self.dp)
            self.dp = new_dp

        self.close_logs()
        self.logger = ValveLogger(
            logging.getLogger(self.logname + '.valve'), self.dp.dp_id, self.dp.name)
        self.ofchannel_logger = None
        self._packet_in_count_sec = 0
        self._last_packet_in_sec = 0
        self._last_advertise_sec = 0
        self._last_fast_advertise_sec = 0
        self._route_manager_by_ipv = {}
        self._route_manager_by_eth_type = {}
        self._port_highwater = {}

        self.dp.reset_refs()

        self.pipeline = valve_pipeline.ValvePipeline(self.dp)
        for vlan_vid in self.dp.vlans.keys():
            self._port_highwater[vlan_vid] = {}
            for port_number in self.dp.ports.keys():
                self._port_highwater[vlan_vid][port_number] = 0
        for ipv, route_manager_class, neighbor_timeout in (
                (4, valve_route.ValveIPv4RouteManager, self.dp.arp_neighbor_timeout),
                (6, valve_route.ValveIPv6RouteManager, self.dp.nd_neighbor_timeout)):
            fib_table_name = 'ipv%u_fib' % ipv
            if not fib_table_name in self.dp.tables:
                continue
            fib_table = self.dp.tables[fib_table_name]
            proactive_learn = getattr(self.dp, 'proactive_learn_v%u' % ipv)
            route_manager = route_manager_class(
                self.logger, self.dp.global_vlan, neighbor_timeout,
                self.dp.max_hosts_per_resolve_cycle,
                self.dp.max_host_fib_retry_count,
                self.dp.max_resolve_backoff_time, proactive_learn,
                self.DEC_TTL, self.dp.multi_out, fib_table,
                self.dp.tables['vip'], self.pipeline, self.dp.routers)
            self._route_manager_by_ipv[route_manager.IPV] = route_manager
            for vlan in self.dp.vlans.values():
                if vlan.faucet_vips_by_ipv(route_manager.IPV):
                    route_manager.active = True
                    self.logger.info('IPv%u routing is active on %s with VIPs %s' % (
                        route_manager.IPV, vlan, vlan.faucet_vips_by_ipv(route_manager.IPV)))
            for eth_type in route_manager.CONTROL_ETH_TYPES:
                self._route_manager_by_eth_type[eth_type] = route_manager
        restricted_bcast_arpnd = bool(self.dp.restricted_bcast_arpnd_ports())
        if self.dp.stack:
            flood_class = valve_flood.ValveFloodStackManagerNoReflection
            if self.dp.stack_root_flood_reflection:
                flood_class = valve_flood.ValveFloodStackManagerReflection
                self.logger.info('Using stacking root flood reflection')
            else:
                self.logger.info('Not using stacking root flood reflection')
            self.flood_manager = flood_class(
                self.logger, self.dp.tables['flood'], self.pipeline,
                self.dp.group_table, self.dp.groups,
                self.dp.combinatorial_port_flood, self.dp.canonical_port_order,
                restricted_bcast_arpnd,
                self.dp.stack_ports, self.dp.has_externals,
                self.dp.shortest_path_to_root, self.dp.shortest_path_port,
                self.dp.is_stack_root, self.dp.is_stack_root_candidate,
                self.dp.is_stack_edge, self.dp.stack.get('graph', None))
        else:
            self.flood_manager = valve_flood.ValveFloodManager(
                self.logger, self.dp.tables['flood'], self.pipeline,
                self.dp.group_table, self.dp.groups,
                self.dp.combinatorial_port_flood, self.dp.canonical_port_order,
                restricted_bcast_arpnd)
        eth_dst_hairpin_table = self.dp.tables.get('eth_dst_hairpin', None)
        host_manager_cl = valve_host.ValveHostManager
        if self.dp.use_idle_timeout:
            host_manager_cl = valve_host.ValveHostFlowRemovedManager
        self.host_manager = host_manager_cl(
            self.logger, self.dp.ports,
            self.dp.vlans, self.dp.tables['eth_src'],
            self.dp.tables['eth_dst'], eth_dst_hairpin_table, self.pipeline,
            self.dp.timeout, self.dp.learn_jitter, self.dp.learn_ban_timeout,
            self.dp.cache_update_guard_time, self.dp.idle_dst, self.dp.stack,
            self.dp.has_externals, self.dp.stack_root_flood_reflection)
        self.acl_manager = None
        if self.dp.has_acls:
            self.acl_manager = valve_acl.ValveAclManager(
                self.dp.tables.get('port_acl'), self.dp.tables.get('vlan_acl'),
                self.dp.tables.get('egress_acl'), self.pipeline,
                self.dp.meters, self.dp.dp_acls)
        table_configs = sorted([
            (table.table_id, str(table.table_config)) for table in self.dp.tables.values()])
        for table_id, table_config in table_configs:
            self.logger.info('table ID %u %s' % (table_id, table_config))

    def _get_managers(self):
        for manager in (
                self.pipeline,
                self.host_manager,
                self._route_manager_by_ipv.get(4),
                self._route_manager_by_ipv.get(6),
                self.flood_manager,
                self.acl_manager):
            if manager is not None:
                yield manager

    def _notify(self, event_dict):
        """Send an event notification."""
        self.notifier.notify(self.dp.dp_id, self.dp.name, event_dict)

    def switch_features(self, _msg):
        """Send configuration flows necessary for the switch implementation.

        Args:
            msg (OFPSwitchFeatures): msg sent from switch.

        Vendor specific configuration should be implemented here.
        """
        ofmsgs = [
            valve_of.faucet_config(),
            valve_of.faucet_async(
                packet_in=False, notify_flow_removed=False, port_status=False),
            valve_of.desc_stats_request()]
        ofmsgs.extend(self._delete_all_valve_flows())
        return ofmsgs

    def ofchannel_log(self, ofmsgs):
        """Log OpenFlow messages in text format to debugging log."""
        if self.dp is None:
            return
        if self.dp.ofchannel_log is None:
            return
        if self.ofchannel_logger is None:
            self.ofchannel_logger = valve_util.get_logger(
                self.dp.ofchannel_log,
                self.dp.ofchannel_log,
                logging.DEBUG,
                0)
        log_prefix = '%u %s' % (
            len(ofmsgs), valve_util.dpid_log(self.dp.dp_id))
        for i, ofmsg in enumerate(ofmsgs, start=1):
            self.ofchannel_logger.debug(
                '%u/%s %s', i, log_prefix, ofmsg)

    def dot1x_event(self, event_dict):
        self._notify({'DOT1X': event_dict})

    def _delete_all_valve_flows(self):
        """Delete all flows from all FAUCET tables."""
        ofmsgs = [valve_table.wildcard_table.flowdel()]
        if self.dp.meters:
            ofmsgs.append(valve_of.meterdel())
        if self.dp.group_table:
            ofmsgs.append(self.dp.groups.delete_all())
        return ofmsgs

    def _delete_all_port_match_flows(self, port):
        """Delete all flows that match an input port from all FAUCET tables."""
        tables = [valve_table.wildcard_table]
        if self.dp.dp_acls:
            # DP ACL flows live forever.
            port_acl_table = self.dp.tables['port_acl']
            tables = set(self.dp.in_port_tables()) - set([port_acl_table])
        return [
            table.flowdel(match=table.match(in_port=port.number))
            for table in tables]

    @staticmethod
    def _pipeline_flows():
        return []

    def _add_default_drop_flows(self):
        """Add default drop rules on all FAUCET tables."""
        ofmsgs = []
        for table in self.dp.tables.values():
            miss_table_name = table.table_config.miss_goto
            if miss_table_name:
                miss_table = self.dp.tables[miss_table_name]
                ofmsgs.append(table.flowmod(
                    priority=self.dp.lowest_priority,
                    inst=[table.goto_miss(miss_table)]))
            else:
                ofmsgs.append(table.flowdrop(
                    priority=self.dp.lowest_priority))
        return ofmsgs

    def _add_packetin_meter(self):
        """Add rate limiting of packet in pps (not supported by many DPs)."""
        if self.dp.packetin_pps:
            return [
                valve_of.controller_pps_meterdel(),
                valve_of.controller_pps_meteradd(pps=self.dp.packetin_pps)]
        return []

    def _add_default_flows(self):
        """Configure datapath with necessary default tables and rules."""
        ofmsgs = []
        ofmsgs.extend(self._delete_all_valve_flows())
        ofmsgs.extend(self._add_packetin_meter())
        if self.dp.meters:
            for meter in self.dp.meters.values():
                ofmsgs.append(meter.entry_msg)
        ofmsgs.extend(self._add_default_drop_flows())
        return ofmsgs

    def _add_vlan(self, vlan):
        """Configure a VLAN."""
        self.logger.info('Configuring %s' % vlan)
        ofmsgs = []
        for manager in self._get_managers():
            ofmsgs.extend(manager.add_vlan(vlan))
        vlan.reset_caches()
        return ofmsgs

    def _add_vlans(self, vlans):
        ofmsgs = []
        for vlan in vlans:
            ofmsgs.extend(self._add_vlan(vlan))
        return ofmsgs

    def _del_vlan(self, vlan):
        """Delete a configured VLAN."""
        self.logger.info('Delete VLAN %s' % vlan)
        table = valve_table.wildcard_table
        return [table.flowdel(match=table.match(vlan=vlan))]

    def _del_vlans(self, vlans):
        ofmsgs = []
        for vlan in vlans:
            ofmsgs.extend(self._del_vlan(vlan))
        return ofmsgs

    def _get_all_configured_port_nos(self):
        ports = {port for port in self.dp.non_vlan_ports()}
        for vlan in self.dp.vlans.values():
            ports.update({port for port in vlan.get_ports()})
        ports = {port.number for port in ports}
        return ports

    @staticmethod
    def _get_ports_status(discovered_up_port_nos, all_configured_port_nos):
        port_status = {
            port_no: (port_no in discovered_up_port_nos) for port_no in all_configured_port_nos}
        all_up_port_nos = {port_no for port_no, status in port_status.items() if status}
        return (port_status, all_up_port_nos)

    def _add_ports_and_vlans(self, discovered_up_port_nos):
        """Add all configured and discovered ports and VLANs."""
        always_up_port_nos = {
            port.number for port in self.dp.ports.values() if not port.opstatus_reconf}
        discovered_up_port_nos = discovered_up_port_nos.union(always_up_port_nos)

        all_configured_port_nos = self._get_all_configured_port_nos()
        port_status, all_up_port_nos = self._get_ports_status(
            discovered_up_port_nos, all_configured_port_nos)

        for port_no, status in port_status.items():
            self._set_port_status(port_no, status)
        self._notify({'PORTS_STATUS': port_status})

        ofmsgs = []
        ofmsgs.extend(self.ports_add(
            all_up_port_nos, cold_start=True, log_msg='configured'))
        ofmsgs.extend(self._add_vlans(self.dp.vlans.values()))
        return ofmsgs

    def ofdescstats_handler(self, body):
        """Handle OF DP description."""
        labels = dict(
            self.dp.base_prom_labels(),
            mfr_desc=valve_util.utf8_decode(body.mfr_desc),
            hw_desc=valve_util.utf8_decode(body.hw_desc),
            sw_desc=valve_util.utf8_decode(body.sw_desc),
            serial_num=valve_util.utf8_decode(body.serial_num),
            dp_desc=valve_util.utf8_decode(body.dp_desc))
        self._set_var('of_dp_desc_stats', self.dp.dp_id, labels=labels)

    def _set_port_status(self, port_no, port_status):
        """Set port operational status."""
        if port_status:
            self.dp.dyn_up_port_nos.add(port_no)
        else:
            self.dp.dyn_up_port_nos -= set([port_no])
        port = self.dp.ports.get(port_no, None)
        if port is None:
            return
        port_labels = self.dp.port_labels(port.number)
        self._set_var('port_status', port_status, labels=port_labels)

    def port_status_handler(self, port_no, reason, state, _other_valves):
        """Return OpenFlow messages responding to port operational status change."""

        port_status_codes = {
            valve_of.ofp.OFPPR_ADD: 'ADD',
            valve_of.ofp.OFPPR_DELETE: 'DELETE',
            valve_of.ofp.OFPPR_MODIFY: 'MODIFY'
        }

        def _decode_port_status(reason):
            """Humanize the port status reason code."""
            return port_status_codes.get(reason, 'UNKNOWN')

        port_status = valve_of.port_status_from_state(state)
        self._notify(
            {'PORT_CHANGE': {
                'port_no': port_no,
                'reason': _decode_port_status(reason),
                'state': state,
                'status': port_status}})
        self._set_port_status(port_no, port_status)

        if not self.dp.port_no_valid(port_no):
            return {}
        port = self.dp.ports[port_no]
        if not port.opstatus_reconf:
            return {}
        if not reason in port_status_codes:
            self.logger.warning('Unhandled port status %s/state %s for %s' % (
                reason, state, port))
            return {}

        ofmsgs_by_valve = {self: []}
        self.logger.info('%s up status %s reason %s state %s' % (
            port, port_status, _decode_port_status(reason), state))
        new_port_status = (
            reason == valve_of.ofp.OFPPR_ADD or
            (reason == valve_of.ofp.OFPPR_MODIFY and port_status))
        if new_port_status:
            if port.dyn_phys_up:
                self.logger.info('%s already up, assuming flap as missing down event' % port)
                ofmsgs_by_valve[self].extend(self.port_delete(port_no))
            ofmsgs_by_valve[self].extend(self.port_add(port_no))
        else:
            ofmsgs_by_valve[self].extend(self.port_delete(port_no))
        return ofmsgs_by_valve

    def advertise(self, now, _other_values):
        """Called periodically to advertise services (eg. IPv6 RAs)."""
        if (not self.dp.advertise_interval or
                now - self._last_advertise_sec < self.dp.advertise_interval):
            return {}
        self._last_advertise_sec = now

        ofmsgs = []
        for route_manager in self._route_manager_by_ipv.values():
            for vlan in self.dp.vlans.values():
                ofmsgs.extend(route_manager.advertise(vlan))
        if ofmsgs:
            return {self: ofmsgs}
        return {}

    def _send_lldp_beacon_on_port(self, port, now):
        chassis_id = str(self.dp.faucet_dp_mac)
        ttl = min(
            self.dp.lldp_beacon.get('send_interval', self.dp.DEFAULT_LLDP_SEND_INTERVAL) * 3,
            2**16-1)
        org_tlvs = [
            (tlv['oui'], tlv['subtype'], tlv['info'])
            for tlv in port.lldp_beacon['org_tlvs']]
        org_tlvs.extend(valve_packet.faucet_lldp_tlvs(self.dp))
        org_tlvs.extend(valve_packet.faucet_lldp_stack_state_tlvs(self.dp, port))
        system_name = port.lldp_beacon['system_name']
        if not system_name:
            system_name = self.dp.lldp_beacon.get('system_name', self.dp.name)
        lldp_beacon_pkt = valve_packet.lldp_beacon(
            self.dp.faucet_dp_mac,
            chassis_id, port.number, ttl,
            org_tlvs=org_tlvs,
            system_name=system_name,
            port_descr=port.lldp_beacon['port_descr'])
        port.dyn_last_lldp_beacon_time = now
        return valve_of.packetout(port.number, lldp_beacon_pkt.data)

    def fast_advertise(self, now, _other_valves):
        """Called periodically to send LLDP/LACP packets."""
        # TODO: the beacon service is specifically NOT to support conventional R/STP.
        # It is intended to facilitate physical troubleshooting (e.g.
        # a standard cable tester can display OF port information).
        # It is used also by stacking to verify stacking links.
        # TODO: in the stacking case, provide an authentication scheme for the probes
        # so they cannot be forged.
        if (not self.dp.fast_advertise_interval or
                now - self._last_fast_advertise_sec < self.dp.fast_advertise_interval):
            return {}
        self._last_fast_advertise_sec = now

        ofmsgs = []
        for port in self.dp.lacp_active_ports:
            if port.running():
                ofmsgs.extend(self._lacp_actions(port.dyn_last_lacp_pkt, port))

        ports = self.dp.lldp_beacon_send_ports(now)
        ofmsgs.extend([self._send_lldp_beacon_on_port(port, now) for port in ports])

        if ofmsgs:
            return {self: ofmsgs}
        return {}

    def _next_stack_link_state(self, port, now):
        next_state = None

        if port.is_stack_admin_down():
            return next_state

        last_seen_lldp_time = port.dyn_stack_probe_info.get('last_seen_lldp_time', None)
        if last_seen_lldp_time is None:
            if port.is_stack_down():
                next_state = port.stack_init
                self.logger.info('Stack %s new, state INIT' % port)
            return next_state

        remote_dp = port.stack['dp']
        stack_correct = port.dyn_stack_probe_info.get(
            'stack_correct', None)
        send_interval = remote_dp.lldp_beacon.get(
            'send_interval', remote_dp.DEFAULT_LLDP_SEND_INTERVAL)

        time_since_lldp_seen = None
        num_lost_lldp = None
        stack_timed_out = True

        if last_seen_lldp_time is not None:
            time_since_lldp_seen = now - last_seen_lldp_time
            num_lost_lldp = time_since_lldp_seen / send_interval
            if num_lost_lldp < port.max_lldp_lost:
                stack_timed_out = False

        if not stack_correct:
            if not port.is_stack_down():
                next_state = port.stack_down
                self.logger.error('Stack %s DOWN, incorrect cabling' % port)
            return next_state

        if stack_timed_out:
            if not port.is_stack_down():
                # Stay in init state if we never got a packet.
                if time_since_lldp_seen:
                    next_state = port.stack_down
                    self.logger.error(
                        'Stack %s DOWN, too many (%u) packets lost, last received %us ago' % (
                            port, num_lost_lldp, time_since_lldp_seen))
        else:
            if not port.is_stack_up():
                next_state = port.stack_up
                self.logger.info('Stack %s UP' % port)
        return next_state

    def _update_stack_link_state(self, ports, now, other_valves):
        stack_changes = 0
        ofmsgs_by_valve = defaultdict(list)
        stacked_valves = {self}.union(self._stacked_valves(other_valves))

        for port in ports:
            next_state = self._next_stack_link_state(port, now)
            if next_state is not None:
                next_state()
                self._set_var(
                    'port_stack_state',
                    port.dyn_stack_current_state,
                    labels=self.dp.port_labels(port.number))
                if port.is_stack_up() or port.is_stack_down() or port.is_stack_init():
                    stack_changes += 1
                    port_stack_up = port.is_stack_up()
                    for valve in stacked_valves:
                        valve.flood_manager.update_stack_topo(port_stack_up, self.dp, port)
        if stack_changes:
            self.logger.info('%u stack ports changed state' % stack_changes)
            for valve in stacked_valves:
                valve.update_tunnel_flowrules()
                if not valve.dp.dyn_running:
                    continue
                ofmsgs_by_valve[valve].extend(valve.get_tunnel_flowmods())
                for vlan in valve.dp.vlans.values():
                    ofmsgs_by_valve[valve].extend(valve.flood_manager.add_vlan(vlan))
                for port in valve.dp.stack_ports:
                    ofmsgs_by_valve[valve].extend(valve.host_manager.del_port(port))
        return ofmsgs_by_valve

    def update_tunnel_flowrules(self):
        """Update tunnel ACL rules because the stack topology has changed"""
        if self.dp.tunnel_acls:
            for tunnel_id, tunnel_acl in self.dp.tunnel_acls.items():
                updated = tunnel_acl.update_tunnel_acl_conf(self.dp)
                if updated:
                    self.dp.tunnel_updated_flags[tunnel_id] = True
                    self.logger.info('updated tunnel %s' % tunnel_id)

    def get_tunnel_flowmods(self):
        """Returns flowmods for the tunnels"""
        if self.acl_manager:
            return self.acl_manager.create_acl_tunnel(self.dp)
        return []

    def fast_state_expire(self, now, other_valves):
        """Called periodically to verify the state of stack ports."""
        if self.dp.lldp_beacon:
            for port in self.dp.ports.values():
                if port.dyn_lldp_beacon_recv_state:
                    age = now - port.dyn_lldp_beacon_recv_time
                    if age > self.dp.lldp_beacon['send_interval'] * 3:
                        self.logger.info('LLDP for %s inactive after %us' % (port, age))
                        port.dyn_lldp_beacon_recv_state = None
        return self._update_stack_link_state(self.dp.stack_ports, now, other_valves)

    def _reset_dp_status(self):
        if self.dp.dyn_running:
            self._set_var('dp_status', 1)
        else:
            self._set_var('dp_status', 0)

    def datapath_connect(self, now, discovered_up_ports):
        """Handle Ryu datapath connection event and provision pipeline.

        Args:
            now (float): current epoch time.
            discovered_up_ports (set): datapath port numbers that are up.
        Returns:
            list: OpenFlow messages to send to datapath.
        """
        self.logger.info('Cold start configuring DP')
        self._notify(
            {'DP_CHANGE': {
                'reason': 'cold_start'}})
        ofmsgs = []
        ofmsgs.extend(self._add_default_flows())
        for manager in self._get_managers():
            ofmsgs.extend(manager.initialise_tables())
        ofmsgs.extend(self._add_ports_and_vlans(discovered_up_ports))
        ofmsgs.append(
            valve_of.faucet_async(
                packet_in=True,
                port_status=True,
                notify_flow_removed=self.dp.use_idle_timeout))
        self.dp.dyn_last_coldstart_time = now
        self.dp.dyn_running = True
        self._inc_var('of_dp_connections')
        self._reset_dp_status()
        return ofmsgs

    def datapath_disconnect(self):
        """Handle Ryu datapath disconnection event."""
        self.logger.warning('datapath down')
        self._notify(
            {'DP_CHANGE': {
                'reason': 'disconnect'}})
        self.dp.dyn_running = False
        self._inc_var('of_dp_disconnections')
        self._reset_dp_status()

    def _port_add_vlan_rules(self, port, vlan, mirror_act, push_vlan=True):
        vlan_table = self.dp.tables['vlan']
        actions = copy.copy(mirror_act)
        match_vlan = vlan
        if push_vlan:
            actions.extend(valve_of.push_vlan_act(
                vlan_table, vlan.vid))
            match_vlan = NullVLAN()
        if self.dp.has_externals:
            if port.loop_protect_external:
                actions.append(vlan_table.set_no_external_forwarding_requested())
            else:
                actions.append(vlan_table.set_external_forwarding_requested())
        inst = [
            valve_of.apply_actions(actions),
            vlan_table.goto(self._find_forwarding_table(vlan))]
        return vlan_table.flowmod(
            vlan_table.match(in_port=port.number, vlan=match_vlan),
            priority=self.dp.low_priority,
            inst=inst
            )

    def _find_forwarding_table(self, vlan):
        if vlan.acls_in:
            return self.dp.tables['vlan_acl']
        return self.dp.classification_table()

    def _port_add_vlans(self, port, mirror_act):
        ofmsgs = []
        for vlan in port.tagged_vlans:
            ofmsgs.append(self._port_add_vlan_rules(
                port, vlan, mirror_act, push_vlan=False))
        if port.dyn_dot1x_native_vlan is not None:
            ofmsgs.append(self._port_add_vlan_rules(
                port, port.dyn_dot1x_native_vlan, mirror_act))
        elif port.native_vlan is not None:
            ofmsgs.append(self._port_add_vlan_rules(
                port, port.native_vlan, mirror_act))
        return ofmsgs

    def _port_delete_manager_state(self, port):
        ofmsgs = []
        for manager in self._get_managers():
            ofmsgs.extend(manager.del_port(port))
        return ofmsgs

    def _port_delete_flows_state(self, port):
        """Delete flows/state for a port."""
        ofmsgs = []
        for route_manager in self._route_manager_by_ipv.values():
            ofmsgs.extend(route_manager.expire_port_nexthops(port))
        ofmsgs.extend(self._delete_all_port_match_flows(port))
        ofmsgs.extend(self._port_delete_manager_state(port))
        return ofmsgs

    def _coprocessor_flows(self, port):
        """Add flows to allow coprocessor to inject or output packets."""
        ofmsgs = []
        copro_table = self.dp.tables['copro']
        vlan_table = self.dp.tables['vlan']
        ofmsgs.append(vlan_table.flowmod(
            vlan_table.match(in_port=port.number),
            priority=self.dp.low_priority,
            inst=[vlan_table.goto(copro_table)]))
        # TODO: add additional output port strategies (eg. MPLS)
        # TODO: support tagged ports with additional VLAN VID ranges.
        dp_port_numbers = [dp_port.number for dp_port in self.dp.ports.values()]
        vlan_vid_base = port.coprocessor.get('vlan_vid_base', 0)
        for in_port_num in dp_port_numbers:
            inst = [valve_of.apply_actions([
                valve_of.pop_vlan(),
                valve_of.output_port(in_port_num)])]
            vid = vlan_vid_base + in_port_num
            vlan = OFVLAN(str(vid), vid)
            match = copro_table.match(vlan=vlan)
            ofmsgs.append(copro_table.flowmod(
                match=match, priority=self.dp.high_priority, inst=inst))
        return ofmsgs

    def ports_add(self, port_nums, cold_start=False, log_msg='up'):
        """Handle the addition of ports.

        Args:
            port_num (list): list of port numbers.
            cold_start (bool): True if configuring datapath from scratch.
        Returns:
            list: OpenFlow messages, if any.
        """
        ofmsgs = []
        vlans_with_ports_added = set()
        vlan_table = self.dp.tables['vlan']

        for port_num in port_nums:
            if not self.dp.port_no_valid(port_num):
                self.logger.info(
                    'Ignoring port:%u not present in configuration file' % port_num)
                continue
            port = self.dp.ports[port_num]
            port.dyn_phys_up = True
            self.logger.info('%s (%s) %s' % (port, port.description, log_msg))

            if not port.running():
                continue

            for manager in self._get_managers():
                ofmsgs.extend(manager.add_port(port))

            if port.coprocessor:
                ofmsgs.extend(self._coprocessor_flows(port))
                continue

            if self.dp.dot1x:
                nfv_sw_port = self.dp.ports[self.dp.dot1x['nfv_sw_port']]
                if port == nfv_sw_port:
                    ofmsgs.extend(self.dot1x.nfv_sw_port_up(
                        self.dp.dp_id, self.dp.dot1x_ports(), nfv_sw_port))
                elif port.dot1x:
                    ofmsgs.extend(self.dot1x.port_up(
                        self.dp.dp_id, port, nfv_sw_port))

            if port.output_only:
                ofmsgs.append(vlan_table.flowdrop(
                    match=vlan_table.match(in_port=port_num),
                    priority=self.dp.highest_priority))
                continue

            if port.receive_lldp:
                ofmsgs.append(vlan_table.flowcontroller(
                    match=vlan_table.match(
                        in_port=port_num,
                        eth_dst=valve_packet.LLDP_MAC_NEAREST_BRIDGE,
                        eth_dst_mask=valve_packet.BRIDGE_GROUP_MASK,
                        eth_type=valve_of.ether.ETH_TYPE_LLDP),
                    priority=self.dp.highest_priority,
                    max_len=128))

            if port.lacp:
                ofmsgs.extend(self.lacp_down(port, cold_start=cold_start))
                if port.lacp_active:
                    ofmsgs.extend(self._lacp_actions(port.dyn_last_lacp_pkt, port))

            port_vlans = port.vlans()

            # If this is a stacking port, accept all VLANs (came from another FAUCET)
            if port.stack:
                # Actual stack traffic will have VLAN tags.
                ofmsgs.append(vlan_table.flowdrop(
                    match=vlan_table.match(
                        in_port=port_num,
                        vlan=NullVLAN()),
                    priority=self.dp.low_priority+1))
                ofmsgs.append(vlan_table.flowmod(
                    match=vlan_table.match(in_port=port_num),
                    priority=self.dp.low_priority,
                    inst=self.pipeline.accept_to_classification()))
                port_vlans = self.dp.vlans.values()
            else:
                mirror_act = port.mirror_actions()
                # Add port/to VLAN rules.
                ofmsgs.extend(self._port_add_vlans(port, mirror_act))

            vlans_with_ports_added.update({vlan for vlan in port_vlans})

        # Only update flooding rules if not cold starting.
        if not cold_start:
            for vlan in vlans_with_ports_added:
                ofmsgs.extend(self.flood_manager.add_vlan(vlan))
        return ofmsgs

    def port_add(self, port_num):
        """Handle addition of a single port.

        Args:
            port_num (list): list of port numbers.
        Returns:
            list: OpenFlow messages, if any.
        """
        return self.ports_add([port_num])

    def ports_delete(self, port_nums, log_msg='down'):
        """Handle the deletion of ports.

        Args:
            port_nums (list): list of port numbers.
        Returns:
            list: OpenFlow messages, if any.
        """
        ofmsgs = []
        vlans_with_deleted_ports = set()

        for port_num in port_nums:
            if not self.dp.port_no_valid(port_num):
                continue
            port = self.dp.ports[port_num]
            port.dyn_phys_up = False
            self.logger.info('%s (%s) %s' % (port, port.description, log_msg))

            if port.output_only:
                continue

            vlans_with_deleted_ports.update({vlan for vlan in port.vlans()})

            if port.dot1x:
                ofmsgs.extend(self.dot1x.port_down(
                    self.dp.dp_id,
                    port,
                    self.dp.ports[self.dp.dot1x['nfv_sw_port']]
                    ))
            if port.lacp:
                ofmsgs.extend(self.lacp_down(port))
            else:
                ofmsgs.extend(self._port_delete_flows_state(port))

        for vlan in vlans_with_deleted_ports:
            ofmsgs.extend(self.flood_manager.update_vlan(vlan))

        return ofmsgs

    def port_delete(self, port_num):
        """Return flow messages that delete port from pipeline."""
        return self.ports_delete([port_num])

    def _reset_lacp_status(self, port):
        self._set_var('port_lacp_status', port.dyn_lacp_up, labels=self.dp.port_labels(port.number))

    def lacp_down(self, port, cold_start=False):
        """Return OpenFlow messages when LACP is down on a port."""
        ofmsgs = []
        if port.dyn_lacp_up != 0:
            self.logger.info('LAG %u %s down (previous state %s)' % (
                port.lacp, port, port.dyn_lacp_up))
        port.dyn_lacp_up = 0
        port.dyn_last_lacp_pkt = None
        port.dyn_lacp_updated_time = None
        port.dyn_lacp_last_resp_time = None
        if not cold_start:
            ofmsgs.extend(self.host_manager.del_port(port))
            for vlan in port.vlans():
                ofmsgs.extend(self.flood_manager.add_vlan(vlan))
        vlan_table = self.dp.tables['vlan']
        ofmsgs.append(vlan_table.flowdrop(
            match=vlan_table.match(in_port=port.number),
            priority=self.dp.high_priority))
        ofmsgs.append(vlan_table.flowcontroller(
            vlan_table.match(
                in_port=port.number,
                eth_type=valve_of.ether.ETH_TYPE_SLOW,
                eth_dst=valve_packet.SLOW_PROTOCOL_MULTICAST),
            priority=self.dp.highest_priority,
            max_len=valve_packet.LACP_SIZE))
        self._reset_lacp_status(port)
        return ofmsgs

    def lacp_up(self, port):
        """Return OpenFlow messages when LACP is up on a port."""
        vlan_table = self.dp.tables['vlan']
        ofmsgs = []
        if port.dyn_lacp_up != 1:
            self.logger.info('LAG %u %s up (previous state %s)' % (
                port.lacp, port, port.dyn_lacp_up))
        port.dyn_lacp_up = 1
        # Only enable learning if this bundle is selected for forwarding.
        # E.g. non stack or root of stack.
        if self.dp.lacp_forwarding(port):
            ofmsgs.append(vlan_table.flowdel(
                match=vlan_table.match(in_port=port.number),
                priority=self.dp.high_priority, strict=True))
            for vlan in port.vlans():
                ofmsgs.extend(self.flood_manager.add_vlan(vlan))
        self._reset_lacp_status(port)
        return ofmsgs

    def _lacp_actions(self, lacp_pkt, port):
        if port.lacp_passthrough:
            for peer_num in port.lacp_passthrough:
                lacp_peer = self.dp.ports.get(peer_num, None)
                if not lacp_peer.dyn_lacp_up:
                    self.logger.warning('Suppressing LACP LAG %s on %s, peer %s link is down' %
                                        (port.lacp, port, lacp_peer))
                    return []
        actor_state_activity = 0
        if port.lacp_active:
            actor_state_activity = 1
        lacp_forwarding = self.dp.lacp_forwarding(port)
        actor_state_collecting = lacp_forwarding
        actor_state_distributing = lacp_forwarding
        if lacp_pkt:
            pkt = valve_packet.lacp_reqreply(
                self.dp.faucet_dp_mac, self.dp.faucet_dp_mac,
                port.lacp, port.number, 1, actor_state_activity,
                actor_state_collecting, actor_state_distributing,
                lacp_pkt.actor_system, lacp_pkt.actor_key, lacp_pkt.actor_port,
                lacp_pkt.actor_system_priority, lacp_pkt.actor_port_priority,
                lacp_pkt.actor_state_defaulted,
                lacp_pkt.actor_state_expired,
                lacp_pkt.actor_state_timeout,
                lacp_pkt.actor_state_collecting,
                lacp_pkt.actor_state_distributing,
                lacp_pkt.actor_state_aggregation,
                lacp_pkt.actor_state_synchronization,
                lacp_pkt.actor_state_activity)
        else:
            pkt = valve_packet.lacp_reqreply(
                self.dp.faucet_dp_mac, self.dp.faucet_dp_mac,
                port.lacp, port.number,
                actor_state_activity=actor_state_activity,
                actor_state_collecting=actor_state_collecting,
                actor_state_distributing=actor_state_distributing)
        self.logger.debug('Sending LACP %s on %s activity %s' % (pkt, port, actor_state_activity))
        return [valve_of.packetout(port.number, pkt.data)]

    def lacp_handler(self, now, pkt_meta):
        """Handle a LACP packet.

        We are a currently a passive, non-aggregateable LACP partner.

        Args:
            now (float): current epoch time.
            pkt_meta (PacketMeta): packet for control plane.
        Returns:
            dict: OpenFlow messages, if any by Valve.
        """
        # TODO: ensure config consistent between LAG ports.
        ofmsgs_by_valve = defaultdict(list)
        if (pkt_meta.eth_dst == valve_packet.SLOW_PROTOCOL_MULTICAST and
                pkt_meta.eth_type == valve_of.ether.ETH_TYPE_SLOW and
                pkt_meta.port.lacp):
            pkt_meta.data = pkt_meta.data[:valve_packet.LACP_SIZE]
            pkt_meta.reparse_all()
            lacp_pkt = valve_packet.parse_lacp_pkt(pkt_meta.pkt)
            if lacp_pkt:
                self.logger.debug('receive LACP %s on %s' % (lacp_pkt, pkt_meta.port))
                age = None
                if pkt_meta.port.dyn_lacp_last_resp_time:
                    age = now - pkt_meta.port.dyn_lacp_last_resp_time
                actor_up = valve_packet.lacp_actor_up(lacp_pkt)
                lacp_state_change = pkt_meta.port.dyn_lacp_up != actor_up
                lacp_pkt_change = (
                    pkt_meta.port.dyn_last_lacp_pkt is None or
                    str(lacp_pkt) != str(pkt_meta.port.dyn_last_lacp_pkt))
                if lacp_state_change:
                    self.logger.info(
                        'remote LACP state change from %s to %s from %s LAG %u (%s)' % (
                            pkt_meta.port.dyn_lacp_up, actor_up,
                            lacp_pkt.actor_system, pkt_meta.port.lacp,
                            pkt_meta.log()))
                    if actor_up:
                        ofmsgs_by_valve[self].extend(self.lacp_up(pkt_meta.port))
                    else:
                        ofmsgs_by_valve[self].extend(self.lacp_down(pkt_meta.port))
                lacp_resp_interval = pkt_meta.port.lacp_resp_interval
                if lacp_pkt_change or (age is not None and age > lacp_resp_interval):
                    ofmsgs_by_valve[self].extend(self._lacp_actions(lacp_pkt, pkt_meta.port))
                    pkt_meta.port.dyn_lacp_last_resp_time = now
                pkt_meta.port.dyn_last_lacp_pkt = lacp_pkt
                pkt_meta.port.dyn_lacp_updated_time = now
                other_lag_ports = [
                    port for port in self.dp.ports.values()
                    if port.lacp == pkt_meta.port.lacp and port.dyn_last_lacp_pkt]
                actor_system = pkt_meta.port.dyn_last_lacp_pkt.actor_system
                for other_lag_port in other_lag_ports:
                    other_actor_system = other_lag_port.dyn_last_lacp_pkt.actor_system
                    if actor_system != other_actor_system:
                        self.logger.error(
                            'LACP actor system mismatch %s: %s, %s %s' % (
                                pkt_meta.port, actor_system,
                                other_lag_port, other_actor_system))
        return ofmsgs_by_valve

    def _verify_stack_lldp(self, port, now, other_valves,
                           remote_dp_id, remote_dp_name,
                           remote_port_id, remote_port_state):
        if not port.stack:
            return {}
        remote_dp = port.stack['dp']
        remote_port = port.stack['port']
        stack_correct = True
        self._inc_var('stack_probes_received')
        if (remote_dp_id != remote_dp.dp_id or
                remote_dp_name != remote_dp.name or
                remote_port_id != remote_port.number):
            self.logger.error(
                'Stack %s cabling incorrect, expected %s:%s:%u, actual %s:%s:%u' % (
                    port,
                    valve_util.dpid_log(remote_dp.dp_id),
                    remote_dp.name,
                    remote_port.number,
                    valve_util.dpid_log(remote_dp_id),
                    remote_dp_name,
                    remote_port_id))
            stack_correct = False
            self._inc_var('stack_cabling_errors')
        port.dyn_stack_probe_info = {
            'last_seen_lldp_time': now,
            'stack_correct': stack_correct,
            'remote_dp_id': remote_dp_id,
            'remote_dp_name': remote_dp_name,
            'remote_port_id': remote_port_id,
            'remote_port_state': remote_port_state
        }
        return self._update_stack_link_state([port], now, other_valves)

    def lldp_handler(self, now, pkt_meta, other_valves):
        """Handle an LLDP packet.

        Args:
            pkt_meta (PacketMeta): packet for control plane.
        """
        if pkt_meta.eth_type != valve_of.ether.ETH_TYPE_LLDP:
            return {}
        pkt_meta.reparse_all()
        lldp_pkt = valve_packet.parse_lldp(pkt_meta.pkt)
        if not lldp_pkt:
            return {}

        port = pkt_meta.port
        (remote_dp_id, remote_dp_name,
         remote_port_id, remote_port_state) = valve_packet.parse_faucet_lldp(
             lldp_pkt, self.dp.faucet_dp_mac)

        port.dyn_lldp_beacon_recv_time = now
        if port.dyn_lldp_beacon_recv_state != remote_port_state:
            chassis_id = str(self.dp.faucet_dp_mac)
            if remote_port_state:
                self.logger.info('LLDP on %s, %s from %s (remote %s, port %u) state %s' % (
                    chassis_id, port, pkt_meta.eth_src, valve_util.dpid_log(remote_dp_id),
                    remote_port_id, remote_port_state))
            port.dyn_lldp_beacon_recv_state = remote_port_state

        peer_mac_src = self.dp.ports[port.number].lldp_peer_mac
        if peer_mac_src and peer_mac_src != pkt_meta.eth_src:
            self.logger.warning('Unexpected LLDP peer. Received pkt from %s instead of %s' % (
                pkt_meta.eth_src, peer_mac_src))
        ofmsgs_by_valve = {}
        if remote_dp_id and remote_port_id:
            self.logger.debug('FAUCET LLDP on %s from %s (remote %s, port %u)' % (
                port, pkt_meta.eth_src, valve_util.dpid_log(remote_dp_id), remote_port_id))
            ofmsgs_by_valve.update(self._verify_stack_lldp(
                port, now, other_valves,
                remote_dp_id, remote_dp_name,
                remote_port_id, remote_port_state))
        else:
            self.logger.debug('LLDP on %s from %s: %s' % (port, pkt_meta.eth_src, str(lldp_pkt)))
        return ofmsgs_by_valve

    @staticmethod
    def _control_plane_handler(now, pkt_meta, route_manager):
        """Handle a packet probably destined to FAUCET's route managers.

        For example, next hop resolution or ICMP echo requests.

        Args:
            pkt_meta (PacketMeta): packet for control plane.
            route_manager (ValveRouteManager): route manager for this eth_type.
        Returns:
            list: OpenFlow messages, if any.
        """
        if (pkt_meta.eth_dst == pkt_meta.vlan.faucet_mac or
                not valve_packet.mac_addr_is_unicast(pkt_meta.eth_dst)):
            return route_manager.control_plane_handler(now, pkt_meta)
        return []

    def rate_limit_packet_ins(self, now):
        """Return True if too many packet ins this second."""
        if self._last_packet_in_sec != now:
            self._last_packet_in_sec = now
            self._packet_in_count_sec = 0
        self._packet_in_count_sec += 1
        if self.dp.ignore_learn_ins:
            if self._packet_in_count_sec % self.dp.ignore_learn_ins == 0:
                self._inc_var('of_ignored_packet_ins')
                return True
        return False

    def router_learn_host(self, pkt_meta):
        """Add L3 forwarding rule.

        Args:
            pkt_meta (PacketMeta): PacketMeta instance for packet received.
        Returns:
            list: OpenFlow messages, if any.
        """
        if pkt_meta.eth_src == pkt_meta.vlan.faucet_mac:
            return self.host_manager.learn_host_intervlan_routing_flows(
                pkt_meta.port, pkt_meta.vlan, pkt_meta.eth_src, pkt_meta.eth_dst)
        if pkt_meta.eth_dst == pkt_meta.vlan.faucet_mac:
            return self.host_manager.learn_host_intervlan_routing_flows(
                pkt_meta.port, pkt_meta.vlan, pkt_meta.eth_dst, pkt_meta.eth_src)
        return []

    def learn_host(self, now, pkt_meta, other_valves):
        """Possibly learn a host on a port.

        Args:
            now (float): current epoch time.
            pkt_meta (PacketMeta): PacketMeta instance for packet received.
            other_valves (list): all Valves other than this one.
        Returns:
            list: OpenFlow messages, if any.
        """
        learn_port = self.flood_manager.edge_learn_port(
            self._stacked_valves(other_valves), pkt_meta)
        if learn_port is not None:
            learn_flows, previous_port, update_cache = self.host_manager.learn_host_on_vlan_ports(
                now, learn_port, pkt_meta.vlan, pkt_meta.eth_src,
                last_dp_coldstart_time=self.dp.dyn_last_coldstart_time)
            if update_cache:
                pkt_meta.vlan.add_cache_host(pkt_meta.eth_src, learn_port, now)
                if pkt_meta.l3_pkt is None:
                    pkt_meta.reparse_ip()
                learn_log = 'L2 learned on %s %s (%u hosts total)' % (
                    learn_port, pkt_meta.log(), pkt_meta.vlan.hosts_count())
                if pkt_meta.port.stack:
                    learn_log += ' from %s' % pkt_meta.port.stack_descr()
                previous_port_no = None
                if previous_port is not None:
                    previous_port_no = previous_port.number
                    if pkt_meta.port.number != previous_port_no:
                        learn_log += ', moved from %s' % previous_port
                        if previous_port.stack:
                            learn_log += ' from %s' % previous_port.stack_descr()
                self.logger.info(learn_log)
                self._notify(
                    {'L2_LEARN': {
                        'port_no': learn_port.number,
                        'previous_port_no': previous_port_no,
                        'vid': pkt_meta.vlan.vid,
                        'eth_src': pkt_meta.eth_src,
                        'eth_dst': pkt_meta.eth_dst,
                        'eth_type': pkt_meta.eth_type,
                        'l3_src_ip': str(pkt_meta.l3_src),
                        'l3_dst_ip': str(pkt_meta.l3_dst)}})
            return learn_flows
        return []

    def parse_rcv_packet(self, in_port, vlan_vid, eth_type, data, orig_len, pkt, eth_pkt, vlan_pkt):
        """Parse a received packet into a PacketMeta instance.

        Args:
            in_port (int): port packet was received on.
            vlan_vid (int): VLAN VID of port packet was received on.
            eth_type (int): Ethernet type of packet.
            data (bytes): Raw packet data.
            orig_len (int): Original length of packet.
            pkt (ryu.lib.packet.packet): parsed packet received.
            ekt_pkt (ryu.lib.packet.ethernet): parsed Ethernet header.
            vlan_pkt (ryu.lib.packet.vlan): parsed VLAN Ethernet header.
        Returns:
            PacketMeta instance.
        """
        eth_src = eth_pkt.src
        eth_dst = eth_pkt.dst
        vlan = None
        if vlan_vid in self.dp.vlans:
            vlan = self.dp.vlans[vlan_vid]
        port = self.dp.ports[in_port]
        pkt_meta = valve_packet.PacketMeta(
            data, orig_len, pkt, eth_pkt, vlan_pkt, port, vlan, eth_src, eth_dst, eth_type)
        if vlan_vid == self.dp.global_vlan:
            vlan_vid = valve_packet.int_from_mac(pkt_meta.eth_dst)
            vlan = self.dp.vlans.get(vlan_vid, None)
            pkt_meta.vlan = vlan
            if vlan is not None:
                pkt_meta.eth_dst = vlan.faucet_mac
        return pkt_meta

    def parse_pkt_meta(self, msg):
        """Parse OF packet-in message to PacketMeta."""
        if not self.dp.dyn_running:
            return None
        if self.dp.strict_packet_in_cookie and self.dp.cookie != msg.cookie:
            self.logger.info('got packet in with unknown cookie %s' % msg.cookie)
            return None
        # Drop any packet we didn't specifically ask for
        if msg.reason != valve_of.ofp.OFPR_ACTION:
            return None
        if not msg.match:
            return None
        in_port = msg.match['in_port']
        if not in_port or not self.dp.port_no_valid(in_port):
            return None

        if not msg.data:
            return None
        # Truncate packet in data (OVS > 2.5 does not honor max_len)
        data = msg.data[:valve_of.MAX_PACKET_IN_BYTES]

        # eth/VLAN header only
        pkt, eth_pkt, eth_type, vlan_pkt, vlan_vid = valve_packet.parse_packet_in_pkt(
            data, max_len=valve_packet.ETH_VLAN_HEADER_SIZE)
        if pkt is None or eth_pkt is None:
            self.logger.info(
                'unparseable packet from port %u' % in_port)
            return None
        if (vlan_vid is not None and
                vlan_vid not in self.dp.vlans and
                vlan_vid != self.dp.global_vlan):
            self.logger.info(
                'packet for unknown VLAN %u' % vlan_vid)
            return None
        pkt_meta = self.parse_rcv_packet(
            in_port, vlan_vid, eth_type, data, msg.total_len, pkt, eth_pkt, vlan_pkt)
        if not valve_packet.mac_addr_is_unicast(pkt_meta.eth_src):
            self.logger.info(
                'packet with non-unicast eth_src %s port %u' % (
                    pkt_meta.eth_src, in_port))
            return None
        if valve_packet.mac_addr_all_zeros(pkt_meta.eth_src):
            self.logger.info(
                'packet with all zeros eth_src %s port %u' % (
                    pkt_meta.eth_src, in_port))
            return None
        if self.dp.stack is not None:
            if (not pkt_meta.port.stack and
                    pkt_meta.vlan and
                    pkt_meta.vlan not in pkt_meta.port.tagged_vlans and
                    pkt_meta.vlan != pkt_meta.port.native_vlan):
                self.logger.warning(
                    ('packet from non-stack port number %u is not member of VLAN %u' % (
                        pkt_meta.port.number, pkt_meta.vlan.vid)))
                return None
        return pkt_meta

    def update_config_metrics(self):
        """Update table names for configuration."""
        self.metrics.reset_dpid(self.dp.base_prom_labels())
        self._reset_dp_status()

        # Map table ids to table names
        tables = self.dp.tables.values()
        table_id_to_name = {table.table_id: table.name for table in tables}

        for table in tables:
            table_id = table.table_id
            next_tables = [table_id_to_name[t] for t in table.next_tables]

            # Also add table miss destination as possible next table, if set
            if table.table_config.miss_goto:
                miss_table = table.table_config.miss_goto
                if miss_table not in next_tables:
                    next_tables.append(miss_table)

            self._set_var(
                'faucet_config_table_names',
                table_id,
                labels=dict(self.dp.base_prom_labels(), table_name=table.name,
                            next_tables=",".join(next_tables)))

    def update_metrics(self, now, updated_port=None, rate_limited=False):
        """Update Gauge/metrics."""

        def _update_vlan(vlan, now, rate_limited):
            if vlan.dyn_last_updated_metrics_sec and rate_limited:
                if now - vlan.dyn_last_updated_metrics_sec < self.dp.metrics_rate_limit_sec:
                    return False
            vlan_labels = dict(self.dp.base_prom_labels(), vlan=vlan.vid)
            self._set_var('vlan_hosts_learned', vlan.hosts_count(), labels=vlan_labels)
            self._set_var('vlan_learn_bans', vlan.dyn_learn_ban_count, labels=vlan_labels)
            for ipv in vlan.ipvs():
                self._set_var(
                    'vlan_neighbors',
                    vlan.neigh_cache_count_by_ipv(ipv),
                    labels=dict(vlan_labels, ipv=ipv))
            return True

        def _update_port(vlan, port):
            port_labels = self.dp.port_labels(port.number)
            port_vlan_labels = self._port_vlan_labels(port, vlan)
            port_vlan_hosts_learned = port.hosts_count(vlans=[vlan])
            self._set_var(
                'port_learn_bans', port.dyn_learn_ban_count, labels=port_labels)
            self._set_var(
                'port_vlan_hosts_learned', port_vlan_hosts_learned, labels=port_vlan_labels)
            highwater = self._port_highwater[vlan.vid][port.number]
            stats_stale = vlan.dyn_host_cache_stats_stale.get(port.number, True)
            # No change in hosts learned on this VLAN, don't re-export MACs.
            if highwater == port_vlan_hosts_learned and not stats_stale:
                return
            if highwater > port_vlan_hosts_learned:
                for i in range(port_vlan_hosts_learned, highwater + 1):
                    self._set_var('learned_macs', 0, dict(port_vlan_labels, n=i))
            self._port_highwater[vlan.vid][port.number] = port_vlan_hosts_learned
            port_vlan_hosts = port.hosts(vlans=[vlan])
            assert port_vlan_hosts_learned == len(port_vlan_hosts)
            # TODO: make MAC table updates less expensive.
            for i, entry in enumerate(sorted(port_vlan_hosts)):
                self._set_var('learned_macs', entry.eth_src_int, dict(port_vlan_labels, n=i))
            vlan.dyn_host_cache_stats_stale[port.number] = False

        if updated_port:
            for vlan in updated_port.vlans():
                if _update_vlan(vlan, now, rate_limited):
                    _update_port(vlan, updated_port)
                    vlan.dyn_last_updated_metrics_sec = now
        else:
            for vlan in self.dp.vlans.values():
                if _update_vlan(vlan, now, rate_limited):
                    for port in vlan.get_ports():
                        _update_port(vlan, port)
                    vlan.dyn_last_updated_metrics_sec = now

    def _non_vlan_rcv_packet(self, now, other_valves, pkt_meta):
        self._inc_var('of_non_vlan_packet_ins')
        if pkt_meta.port.lacp:
            lacp_ofmsgs_by_valve = self.lacp_handler(now, pkt_meta)
            if lacp_ofmsgs_by_valve:
                return lacp_ofmsgs_by_valve
        # TODO: verify LLDP message (e.g. org-specific authenticator TLV)
        return self.lldp_handler(now, pkt_meta, other_valves)

    def router_rcv_packet(self, now, pkt_meta):
        """Process packets destined for router or run resolver.

        Args:
            now (float): current epoch time.
            pkt_meta (PacketMeta): packet for control plane.
        Returns:
            list: OpenFlow messages.
        """
        if not pkt_meta.vlan.faucet_vips:
            return []
        route_manager = self._route_manager_by_eth_type.get(
            pkt_meta.eth_type, None)
        if not (route_manager and route_manager.active):
            return []
        pkt_meta.reparse_ip()
        if not pkt_meta.l3_pkt:
            return []
        control_plane_ofmsgs = self._control_plane_handler(
            now, pkt_meta, route_manager)
        ofmsgs = []
        if control_plane_ofmsgs:
            ofmsgs.extend(control_plane_ofmsgs)
        else:
            ofmsgs.extend(
                route_manager.add_host_fib_route_from_pkt(now, pkt_meta))
            # No CPN activity, run resolver.
            ofmsgs.extend(
                route_manager.resolve_gateways(
                    pkt_meta.vlan, now, resolve_all=False))
            ofmsgs.extend(
                route_manager.resolve_expire_hosts(
                    pkt_meta.vlan, now, resolve_all=False))
        return ofmsgs

    @staticmethod
    def _stacked_valves(valves):
        return {valve for valve in valves if valve.dp.stack_root_name}

    def _vlan_rcv_packet(self, now, other_valves, pkt_meta):
        """Handle packet with VLAN tag across all Valves.

        Args:
            now (float): current epoch time.
            other_valves (list): all Valves other than this one.
            pkt_meta (PacketMeta): packet for control plane.
        Returns:
            dict: OpenFlow messages, if any by Valve.
        """
        self._inc_var('of_vlan_packet_ins')
        ban_rules = self.host_manager.ban_rules(pkt_meta)
        if ban_rules:
            return {self: ban_rules}

        def handle_pkt(valve, now, pkt_meta, other_valves):
            ofmsgs = []
            ofmsgs.extend(valve.learn_host(now, pkt_meta, other_valves))
            ofmsgs.extend(valve.router_rcv_packet(now, pkt_meta))
            if self.dp.stack_route_learning and not self.dp.is_stack_root():
                # TODO: we will repeatedly spam the DP for each packet in.
                # Should use learn_host() style rate limiting.
                ofmsgs.extend(valve.router_learn_host(pkt_meta))
            return ofmsgs

        ofmsgs_by_valve = {}
        stacked_other_valves = self._stacked_valves(other_valves)
        all_stacked_valves = {self}.union(stacked_other_valves)

        # TODO: generalize multi DP routing
        if self.dp.stack_route_learning:
            # TODO: multi DP routing requires learning from directly attached switch first.
            if pkt_meta.port.stack:
                peer_dp = pkt_meta.port.stack['dp']
                if peer_dp.dyn_running:
                    faucet_macs = {pkt_meta.vlan.faucet_mac}.union(
                        {valve.dp.faucet_dp_mac for valve in all_stacked_valves})
                    # Must always learn FAUCET VIP, but rely on neighbor
                    # to learn other hosts first.
                    if pkt_meta.eth_src not in faucet_macs:
                        return {}

            for valve in stacked_other_valves:
                # TODO: does not handle pruning.
                stack_port = valve.dp.shortest_path_port(self.dp.name)
                valve_vlan = valve.dp.vlans.get(pkt_meta.vlan.vid, None)
                if stack_port and valve_vlan:
                    valve_pkt_meta = copy.copy(pkt_meta)
                    valve_pkt_meta.vlan = valve_vlan
                    valve_pkt_meta.port = stack_port
                    valve_other_valves = all_stacked_valves - {valve}
                    ofmsgs_by_valve[valve] = handle_pkt(
                        valve, now, valve_pkt_meta, valve_other_valves)

        ofmsgs_by_valve[self] = handle_pkt(
            self, now, pkt_meta, other_valves)
        return ofmsgs_by_valve

    def rcv_packet(self, now, other_valves, pkt_meta):
        """Handle a packet from the dataplane (eg to re/learn a host).

        The packet may be sent to us also in response to FAUCET
        initiating IPv6 neighbor discovery, or ARP, to resolve
        a nexthop.

        Args:
            other_valves (list): all Valves other than this one.
            pkt_meta (PacketMeta): packet for control plane.
        Returns:
            dict: OpenFlow messages, if any by Valve.
        """
        # TODO: expensive, even at non-debug level.
        # self.logger.debug(
        #    'Packet_in src:%s in_port:%d VLAN:%s' % (
        #        pkt_meta.eth_src,
        #        pkt_meta.port.number,
        #        pkt_meta.vlan))

        if pkt_meta.vlan is None:
            return self._non_vlan_rcv_packet(now, other_valves, pkt_meta)
        return self._vlan_rcv_packet(now, other_valves, pkt_meta)

    def _lacp_state_expire(self, now, _other_valves):
        """Expire controller state for LACP.

        Args:
            now (float): current epoch time.
            _other_valves (list): all Valves other than this one.
        Return:
            dict: OpenFlow messages, if any by Valve.
        """
        ofmsgs_by_valve = defaultdict(list)
        for lag, ports_up in self.dp.lags_up().items():
            for port in ports_up:
                lacp_age = now - port.dyn_lacp_updated_time
                if lacp_age > self.dp.lacp_timeout:
                    self.logger.info('LACP %s on %s expired (age %u)' % (lag, port, lacp_age))
                    ofmsgs_by_valve[self].extend(self.lacp_down(port))
        return ofmsgs_by_valve

    def state_expire(self, now, other_valves):
        """Expire controller caches/state (e.g. hosts learned).

        Args:
            now (float): current epoch time.
            other_valves (list): all Valves other than this one.
        Return:
            dict: OpenFlow messages, if any by Valve.
        """
        ofmsgs_by_valve = defaultdict(list)
        if self.dp.dyn_running:
            ofmsgs_by_valve.update(self._lacp_state_expire(now, other_valves))
            for vlan in self.dp.vlans.values():
                expired_hosts = self.host_manager.expire_hosts_from_vlan(vlan, now)
                if not self.dp.idle_dst:
                    for entry in expired_hosts:
                        ofmsgs_by_valve[self].extend(
                            self.host_manager.delete_host_from_vlan(entry.eth_src, vlan))
                for entry in expired_hosts:
                    self._notify(
                        {'L2_EXPIRE': {
                            'port_no': entry.port.number,
                            'vid': vlan.vid,
                            'eth_src': entry.eth_src}})
                for route_manager in self._route_manager_by_ipv.values():
                    ofmsgs_by_valve[self].extend(route_manager.resolve_expire_hosts(vlan, now))
        return ofmsgs_by_valve

    def _pipeline_change(self):
        def table_msgs(tfm_flow):
            return {str(x) for x in tfm_flow.body}

        if self._last_pipeline_flows:
            _last_pipeline_flows = table_msgs(self._last_pipeline_flows[0])
            _pipeline_flows = table_msgs(self._pipeline_flows()[0])
            if _last_pipeline_flows != _pipeline_flows:
                self.logger.info('pipeline change: %s' % str(
                    _last_pipeline_flows.difference(_pipeline_flows)))
                return True
        return False

    def _apply_config_changes(self, new_dp, changes):
        """Apply any detected configuration changes.

        Args:
            new_dp: (DP): new dataplane configuration.
            changes (tuple) of:
                deleted_ports (set): deleted port numbers.
                changed_ports (set): changed/added port numbers.
                changed_acl_ports (set): changed ACL only port numbers.
                deleted_vids (set): deleted VLAN IDs.
                changed_vids (set): changed/added VLAN IDs.
                all_ports_changed (bool): True if all ports changed.
        Returns:
            tuple:
                cold_start (bool): whether cold starting.
                ofmsgs (list): OpenFlow messages.
        """
        (deleted_ports, changed_ports, changed_acl_ports,
         deleted_vids, changed_vids, all_ports_changed) = changes

        if self._pipeline_change():
            self.logger.info('pipeline change')
            self.dp_init(new_dp)
            return True, []

        if all_ports_changed:
            self.logger.info('all ports changed')
            self.dp_init(new_dp)
            return True, []

        all_up_port_nos = [
            port for port in changed_ports
            if port in self.dp.dyn_up_port_nos]

        ofmsgs = []

        if deleted_ports:
            ofmsgs.extend(self.ports_delete(deleted_ports))
        if deleted_vids:
            deleted_vlans = [self.dp.vlans[vid] for vid in deleted_vids]
            ofmsgs.extend(self._del_vlans(deleted_vlans))
        if changed_ports:
            ofmsgs.extend(self.ports_delete(changed_ports))

        self.dp_init(new_dp)

        if changed_vids:
            changed_vlans = [self.dp.vlans[vid] for vid in changed_vids]
            # TODO: handle change versus add separately so can avoid delete first.
            ofmsgs.extend(self._del_vlans(changed_vlans))
            ofmsgs.extend(self._add_vlans(changed_vlans))
        if changed_ports:
            ofmsgs.extend(self.ports_add(all_up_port_nos))
        if self.acl_manager and changed_acl_ports:
            for port_num in changed_acl_ports:
                port = self.dp.ports[port_num]
                ofmsgs.extend(self.acl_manager.cold_start_port(port))
        return False, ofmsgs

    def reload_config(self, _now, new_dp):
        """Reload configuration new_dp.

        Following config changes are currently supported:
            - Port config: support all available configs
                  (e.g. native_vlan, acl_in) & change operations
                  (add, delete, modify) a port
            - ACL config:support any modification, currently reload all
                  rules belonging to an ACL
            - VLAN config: enable, disable routing, etc...

        Args:
            now (float): current epoch time.
            new_dp (DP): new dataplane configuration.
        Returns:
            ofmsgs (list): OpenFlow messages.
        """
        cold_start, ofmsgs = self._apply_config_changes(
            new_dp, self.dp.get_config_changes(self.logger, new_dp))
        restart_type = None
        if cold_start:
            restart_type = 'cold'
            if self.dp.dyn_running:
                self.logger.info('forcing DP reconnection to ensure ports are synchronized')
                ofmsgs = None
        elif self.dp.dyn_running and ofmsgs:
            restart_type = 'warm'
        else:
            ofmsgs = []
        if restart_type is not None:
            self._inc_var('faucet_config_reload_%s' % restart_type)
            self.logger.info('%s starting' % restart_type)
        self._notify({'CONFIG_CHANGE': {'restart_type': restart_type}})
        return ofmsgs

    def _del_native_vlan(self, port):
        vlan_table = self.dp.tables['vlan']
        ofmsg = vlan_table.flowdel(
            vlan_table.match(in_port=port.number, vlan=port.native_vlan),
            priority=self.dp.low_priority,
        )
        return [ofmsg]

    def _warm_reconfig_port_vlans(self, port, vlans):
        ofmsgs = []
        ofmsgs.extend(self.host_manager.del_port(port))
        mirror_act = port.mirror_actions()
        ofmsgs.extend(self._port_add_vlans(port, mirror_act))
        for vlan in vlans:
            ofmsgs.extend(self.flood_manager.update_vlan(vlan))
        return ofmsgs

    def add_dot1x_native_vlan(self, port_num, vlan_name):
        ofmsgs = []
        port = self.dp.ports[port_num]
        vlans = [vlan for vlan in self.dp.vlans.values() if vlan.name == vlan_name]
        if vlans:
            vlan = vlans[0]
            port.dyn_dot1x_native_vlan = vlan
            vlan.reset_ports(self.dp.ports.values())
            ofmsgs.extend(self._del_native_vlan(port))
            ofmsgs.extend(self._warm_reconfig_port_vlans(
                port, (port.dyn_dot1x_native_vlan, port.native_vlan)))
        return ofmsgs

    def del_dot1x_native_vlan(self, port_num):
        ofmsgs = []
        port = self.dp.ports[port_num]
        if port.dyn_dot1x_native_vlan is not None:
            dyn_vlan = port.dyn_dot1x_native_vlan
            port.dyn_dot1x_native_vlan = None
            dyn_vlan.reset_ports(self.dp.ports.values())
            # Delete any existing native VLAN rule.
            vlan_table = self.dp.tables['vlan']
            ofmsgs.append(vlan_table.flowdel(
                vlan_table.match(in_port=port.number, vlan=NullVLAN()),
                priority=self.dp.low_priority))
            ofmsgs.extend(self._warm_reconfig_port_vlans(
                port, (dyn_vlan, port.native_vlan)))
        return ofmsgs

    def router_vlan_for_ip_gw(self, vlan, ip_gw):
        route_manager = self._route_manager_by_ipv[ip_gw.version]
        return route_manager.router_vlan_for_ip_gw(vlan, ip_gw)

    def add_route(self, vlan, ip_gw, ip_dst):
        """Add route to VLAN routing table."""
        route_manager = self._route_manager_by_ipv[ip_dst.version]
        return route_manager.add_route(vlan, ip_gw, ip_dst)

    def del_route(self, vlan, ip_dst):
        """Delete route from VLAN routing table."""
        route_manager = self._route_manager_by_ipv[ip_dst.version]
        return route_manager.del_route(vlan, ip_dst)

    def resolve_gateways(self, now, _other_valves):
        """Call route managers to re/resolve gateways.

        Returns:
            dict: OpenFlow messages, if any by Valve.
        """
        ofmsgs = []
        if self.dp.dyn_running:
            for route_manager in self._route_manager_by_ipv.values():
                for vlan in self.dp.vlans.values():
                    ofmsgs.extend(route_manager.resolve_gateways(vlan, now))
        if ofmsgs:
            return {self: ofmsgs}
        return {}

    def oferror(self, msg):
        """Correlate OFError message with flow we sent, if any.

        Args:
            msg (ryu.controller.ofp_event.EventOFPMsgBase): message from datapath.
        """
        self._inc_var('of_errors')
        orig_msgs = [orig_msg for orig_msg in self.recent_ofmsgs if orig_msg.xid == msg.xid]
        error_txt = msg
        if orig_msgs:
            error_txt = '%s caused by %s' % (error_txt, orig_msgs[0])
        error_type = 'UNKNOWN'
        error_code = 'UNKNOWN'
        try:
            error_tuple = valve_of.OFERROR_TYPE_CODE[msg.type]
            error_type = error_tuple[0]
            error_code = error_tuple[1][msg.code]
        except KeyError:
            pass
        self.logger.error('OFError type: %s code: %s %s' % (error_type, error_code, error_txt))

    def prepare_send_flows(self, flow_msgs):
        """Prepare to send flows to datapath.

        Args:
            flow_msgs (list): OpenFlow messages to send.
        """
        if flow_msgs is None:
            return flow_msgs
        reordered_flow_msgs = valve_of.valve_flowreorder(
            flow_msgs, use_barriers=self.USE_BARRIERS)
        self.ofchannel_log(reordered_flow_msgs)
        self._inc_var('of_flowmsgs_sent', val=len(reordered_flow_msgs))
        self.recent_ofmsgs.extend(reordered_flow_msgs)
        return reordered_flow_msgs

    def send_flows(self, ryu_dp, flow_msgs):
        """Send flows to datapath (or disconnect an OF session).

        Args:
            ryu_dp (ryu.controller.controller.Datapath): datapath.
            flow_msgs (list): OpenFlow messages to send.
        """
        if flow_msgs is None:
            self.datapath_disconnect()
            ryu_dp.close()
        else:
            for flow_msg in self.prepare_send_flows(flow_msgs):
                flow_msg.datapath = ryu_dp
                ryu_dp.send_msg(flow_msg)

    def flow_timeout(self, now, table_id, match):
        """Call flow timeout message handler:

        Args:
            now (float): current epoch time.
            table_id (int): ID of table where flow was installed.
            match (dict): match conditions for expired flow.
        Returns:
            list: OpenFlow messages, if any.
        """
        return self.host_manager.flow_timeout(now, table_id, match)

    def get_config_dict(self):
        """Return datapath config as a dict for experimental API."""
        return self.dp.get_config_dict()


class TfmValve(Valve):
    """Valve implementation that uses OpenFlow send table features messages."""

    USE_OXM_IDS = True
    MAX_TABLE_ID = 0
    MIN_MAX_FLOWS = 0
    FILL_REQ = True

    def _pipeline_flows(self):
        return [valve_of.table_features(
            tfm_pipeline.load_tables(
                self.dp, self, self.MAX_TABLE_ID, self.MIN_MAX_FLOWS,
                self.USE_OXM_IDS, self.FILL_REQ))]

    def _add_default_flows(self):
        ofmsgs = self._pipeline_flows()
        self._last_pipeline_flows = copy.deepcopy(ofmsgs)
        ofmsgs.extend(super(TfmValve, self)._add_default_flows())
        return ofmsgs


class OVSValve(Valve):
    """Valve implementation for OVS."""

    USE_BARRIERS = False


class OVSTfmValve(TfmValve):
    """Valve implementation for OVS."""

    # TODO: use OXMIDs acceptable to OVS.
    # TODO: dynamically determine tables/flows
    USE_BARRIERS = False
    USE_OXM_IDS = False
    MAX_TABLE_ID = 253
    MIN_MAX_FLOWS = 1000000


class ArubaValve(TfmValve):
    """Valve implementation for Aruba."""

    DEC_TTL = False
    # Aruba does not like empty miss instructions even if not used.
    FILL_REQ = False

    def _delete_all_valve_flows(self):
        ofmsgs = super(ArubaValve, self)._delete_all_valve_flows()
        # Unreferenced group(s) from a previous config that used them,
        # can steal resources from regular flowmods. Unconditionally
        # delete all groups even if groups are not enabled to avoid this.
        ofmsgs.append(self.dp.groups.delete_all())
        return ofmsgs


class CiscoC9KValve(TfmValve):
    """Valve implementation for C9K."""


class AlliedTelesis(OVSValve):
    """Valve implementation for AT."""

    DEC_TTL = False


class NoviFlowValve(Valve):
    """Valve implementation for NoviFlow with static pipeline."""

    STATIC_TABLE_IDS = True
    USE_BARRIERS = True


SUPPORTED_HARDWARE = {
    'Generic': Valve,
    'GenericTFM': TfmValve,
    'Allied-Telesis': AlliedTelesis,
    'Aruba': ArubaValve,
    'CiscoC9K': CiscoC9KValve,
    'Lagopus': OVSValve,
    'Netronome': OVSValve,
    'NoviFlow': NoviFlowValve,
    'Open vSwitch': OVSValve,
    'Open vSwitch TFM': OVSTfmValve,
    'ZodiacFX': OVSValve,
    'ZodiacGX': OVSValve,
}


def valve_factory(dp):
    """Return a Valve object based dp's hardware configuration field.

    Args:
        dp (DP): DP instance with the configuration for this Valve.
    """
    if dp.hardware in SUPPORTED_HARDWARE:
        return SUPPORTED_HARDWARE[dp.hardware]
    return None
