#!/usr/bin/env python

"""Unit tests run as PYTHONPATH=../../.. python3 ./test_valve.py."""

# Copyright (C) 2015 Research and Innovation Advanced Network New Zealand Ltd.
# Copyright (C) 2015--2019 The Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from functools import partial
import hashlib
import unittest
from ryu.ofproto import ofproto_v1_3 as ofp
from faucet import config_parser_util
from valve_test_lib import CONFIG, DP1_CONFIG, FAUCET_MAC, ValveTestBases


class ValveIncludeTestCase(ValveTestBases.ValveTestSmall):
    """Test include optional files."""

    CONFIG = """
include-optional: ['/does/not/exist/']
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                native_vlan: 0x100
""" % DP1_CONFIG

    def setUp(self):
        self.setup_valve(self.CONFIG)

    def test_include_optional(self):
        """Test include optional files."""
        self.assertEqual(1, int(self.get_prom('dp_status')))


class ValveBadConfTestCase(ValveTestBases.ValveTestSmall):
    """Test recovery from a bad config file."""

    CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                native_vlan: 0x100
""" % DP1_CONFIG

    MORE_CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                native_vlan: 0x100
            p2:
                number: 2
                native_vlan: 0x100
""" % DP1_CONFIG

    BAD_CONFIG = """
dps: {}
"""

    def setUp(self):
        self.setup_valve(self.CONFIG)

    def test_bad_conf(self):
        for config, load_error in (
                (self.CONFIG, 0),
                (self.BAD_CONFIG, 1),
                (self.CONFIG, 0),
                (self.MORE_CONFIG, 0),
                (self.BAD_CONFIG, 1),
                (self.CONFIG, 0)):
            with open(self.config_file, 'w') as config_file:
                config_file.write(config)
            self.valves_manager.request_reload_configs(self.mock_time(), self.config_file)
            self.assertEqual(
                load_error,
                self.get_prom('faucet_config_load_error', bare=True),
                msg='%u: %s' % (load_error, config))


class ValveChangePortTestCase(ValveTestBases.ValveTestSmall):
    """Test changes to config on ports."""

    CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                native_vlan: 0x100
            p2:
                number: 2
                native_vlan: 0x200
                permanent_learn: True
""" % DP1_CONFIG

    LESS_CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                native_vlan: 0x100
            p2:
                number: 2
                native_vlan: 0x200
                permanent_learn: False
""" % DP1_CONFIG

    def setUp(self):
        self.setup_valve(self.CONFIG)

    def test_delete_permanent_learn(self):
        """Test port permanent learn can deconfigured."""
        self.rcv_packet(2, 0x200, {
            'eth_src': self.P2_V200_MAC,
            'eth_dst': self.P3_V200_MAC,
            'ipv4_src': '10.0.0.2',
            'ipv4_dst': '10.0.0.3',
            'vid': 0x200})
        self.update_config(self.LESS_CONFIG, reload_type='warm')


class ValveDeletePortTestCase(ValveTestBases.ValveTestSmall):
    """Test deletion of a port."""

    CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                tagged_vlans: [0x100]
            p2:
                number: 2
                tagged_vlans: [0x100]
            p3:
                number: 3
                tagged_vlans: [0x100]
""" % DP1_CONFIG

    LESS_CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                tagged_vlans: [0x100]
            p2:
                number: 2
                tagged_vlans: [0x100]
""" % DP1_CONFIG

    def setUp(self):
        self.setup_valve(self.CONFIG)

    def test_port_delete(self):
        """Test port can be deleted."""
        self.update_config(self.LESS_CONFIG, reload_type='cold')


class ValveAddPortTestCase(ValveTestBases.ValveTestSmall):
    """Test addition of a port."""

    CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                tagged_vlans: [0x100]
            p2:
                number: 2
                tagged_vlans: [0x100]
""" % DP1_CONFIG

    MORE_CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                tagged_vlans: [0x100]
            p2:
                number: 2
                tagged_vlans: [0x100]
            p3:
                number: 3
                tagged_vlans: [0x100]
""" % DP1_CONFIG

    def _inport_flows(self, in_port, ofmsgs):
        return [
            ofmsg for ofmsg in self.flowmods_from_flows(ofmsgs)
            if ofmsg.match.get('in_port') == in_port]

    def setUp(self):
        initial_ofmsgs = self.setup_valve(self.CONFIG)
        self.assertFalse(self._inport_flows(3, initial_ofmsgs))

    def test_port_add(self):
        """Test port can be added."""
        reload_ofmsgs = self.update_config(self.MORE_CONFIG, reload_type='cold')
        self.assertTrue(self._inport_flows(3, reload_ofmsgs))


class ValveWarmStartVLANTestCase(ValveTestBases.ValveTestSmall):
    """Test change of port VLAN only is a warm start."""

    CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 9
                tagged_vlans: [0x100]
            p2:
                number: 11
                tagged_vlans: [0x100]
            p3:
                number: 13
                tagged_vlans: [0x100]
            p4:
                number: 14
                native_vlan: 0x200
""" % DP1_CONFIG

    WARM_CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 9
                tagged_vlans: [0x100]
            p2:
                number: 11
                tagged_vlans: [0x100]
            p3:
                number: 13
                tagged_vlans: [0x100]
            p4:
                number: 14
                native_vlan: 0x300
""" % DP1_CONFIG

    def setUp(self):
        self.setup_valve(self.CONFIG)

    def test_warm_start(self):
        """Test VLAN change is warm startable and metrics maintained."""
        self.rcv_packet(9, 0x100, {
            'eth_src': self.P1_V100_MAC,
            'eth_dst': self.UNKNOWN_MAC,
            'ipv4_src': '10.0.0.1',
            'ipv4_dst': '10.0.0.2'})
        vlan_labels = {'vlan': str(int(0x100))}
        port_labels = {'port': 'p1', 'port_description': 'p1'}
        port_labels.update(vlan_labels)
        self.assertEqual(
            1, self.get_prom('vlan_hosts_learned', labels=vlan_labels))
        self.assertEqual(
            1, self.get_prom('port_vlan_hosts_learned', labels=port_labels))
        self.update_config(self.WARM_CONFIG, reload_type='warm')
        self.assertEqual(
            1, self.get_prom('vlan_hosts_learned', labels=vlan_labels))
        self.assertEqual(
            1, self.get_prom('port_vlan_hosts_learned', labels=port_labels))


class ValveDeleteVLANTestCase(ValveTestBases.ValveTestSmall):
    """Test deleting VLAN."""

    CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                tagged_vlans: [0x100, 0x200]
            p2:
                number: 2
                native_vlan: 0x200
""" % DP1_CONFIG

    LESS_CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                tagged_vlans: [0x200]
            p2:
                number: 2
                native_vlan: 0x200
""" % DP1_CONFIG

    def setUp(self):
        self.setup_valve(self.CONFIG)

    def test_delete_vlan(self):
        """Test VLAN can be deleted."""
        self.update_config(self.LESS_CONFIG, reload_type='cold')


class ValveAddVLANTestCase(ValveTestBases.ValveTestSmall):
    """Test adding VLAN."""

    CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                tagged_vlans: [0x100, 0x200]
            p2:
                number: 2
                tagged_vlans: [0x100]
""" % DP1_CONFIG

    MORE_CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                tagged_vlans: [0x100, 0x200]
            p2:
                number: 2
                tagged_vlans: [0x100, 0x300]
""" % DP1_CONFIG

    def setUp(self):
        self.setup_valve(self.CONFIG)

    def test_add_vlan(self):
        """Test VLAN can added."""
        self.update_config(self.MORE_CONFIG, reload_type='cold')


class ValveChangeACLTestCase(ValveTestBases.ValveTestSmall):
    """Test changes to ACL on a port."""

    CONFIG = """
acls:
    acl_same_a:
        - rule:
            actions:
                allow: 1
    acl_same_b:
        - rule:
            actions:
                allow: 1
    acl_diff_c:
        - rule:
            actions:
                allow: 0
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                native_vlan: 0x100
                acl_in: acl_same_a
            p2:
                number: 2
                native_vlan: 0x200
""" % DP1_CONFIG

    SAME_CONTENT_CONFIG = """
acls:
    acl_same_a:
        - rule:
            actions:
                allow: 1
    acl_same_b:
        - rule:
            actions:
                allow: 1
    acl_diff_c:
        - rule:
            actions:
                allow: 0
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                native_vlan: 0x100
                acl_in: acl_same_b
            p2:
                number: 2
                native_vlan: 0x200
""" % DP1_CONFIG

    DIFF_CONTENT_CONFIG = """
acls:
    acl_same_a:
        - rule:
            actions:
                allow: 1
    acl_same_b:
        - rule:
            actions:
                allow: 1
    acl_diff_c:
        - rule:
            actions:
                allow: 0
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                native_vlan: 0x100
                acl_in: acl_diff_c
            p2:
                number: 2
                native_vlan: 0x200
""" % DP1_CONFIG

    def setUp(self):
        self.setup_valve(self.CONFIG)

    def test_change_port_acl(self):
        """Test port ACL can be changed."""
        self.update_config(self.SAME_CONTENT_CONFIG, reload_type='warm')
        self.rcv_packet(1, 0x100, {
            'eth_src': self.P1_V100_MAC,
            'eth_dst': self.UNKNOWN_MAC,
            'ipv4_src': '10.0.0.1',
            'ipv4_dst': '10.0.0.2'})
        vlan_labels = {'vlan': str(int(0x100))}
        port_labels = {'port': 'p1', 'port_description': 'p1'}
        port_labels.update(vlan_labels)
        self.assertEqual(
            1, self.get_prom('vlan_hosts_learned', labels=vlan_labels))
        self.assertEqual(
            1, self.get_prom('port_vlan_hosts_learned', labels=port_labels))
        # ACL changed but we kept the learn cache.
        self.update_config(self.DIFF_CONTENT_CONFIG, reload_type='warm')
        self.assertEqual(
            1, self.get_prom('vlan_hosts_learned', labels=vlan_labels))
        self.assertEqual(
            1, self.get_prom('port_vlan_hosts_learned', labels=port_labels))


class ValveChangeMirrorTestCase(ValveTestBases.ValveTestSmall):
    """Test changes mirroring port."""

    CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                native_vlan: 0x100
            p2:
                number: 2
                output_only: True
""" % DP1_CONFIG

    MIRROR_CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                native_vlan: 0x100
            p2:
                number: 2
                mirror: p1
""" % DP1_CONFIG

    def setUp(self):
        self.setup_valve(self.CONFIG)

    def test_change_port_acl(self):
        """Test port ACL can be changed."""
        self.rcv_packet(1, 0x100, {
            'eth_src': self.P1_V100_MAC,
            'eth_dst': self.UNKNOWN_MAC,
            'ipv4_src': '10.0.0.1',
            'ipv4_dst': '10.0.0.2'})
        vlan_labels = {'vlan': str(int(0x100))}
        port_labels = {'port': 'p1', 'port_description': 'p1'}
        port_labels.update(vlan_labels)
        self.assertEqual(
            1, self.get_prom('vlan_hosts_learned', labels=vlan_labels))
        self.assertEqual(
            1, self.get_prom('port_vlan_hosts_learned', labels=port_labels))
        # Now mirroring port 1 but we kept the cache.
        self.update_config(self.MIRROR_CONFIG, reload_type='warm')
        self.assertEqual(
            1, self.get_prom('vlan_hosts_learned', labels=vlan_labels))
        self.assertEqual(
            1, self.get_prom('port_vlan_hosts_learned', labels=port_labels))


class ValveACLTestCase(ValveTestBases.ValveTestSmall):
    """Test ACL drop/allow and reloading."""

    def setUp(self):
        self.setup_valve(CONFIG)

    def test_vlan_acl_deny(self):
        """Test VLAN ACL denies a packet."""
        acl_config = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                native_vlan: v100
            p2:
                number: 2
                native_vlan: v200
                tagged_vlans: [v100]
            p3:
                number: 3
                tagged_vlans: [v100, v200]
            p4:
                number: 4
                tagged_vlans: [v200]
            p5:
                number: 5
                native_vlan: v300
vlans:
    v100:
        vid: 0x100
    v200:
        vid: 0x200
        acl_in: drop_non_ospf_ipv4
    v300:
        vid: 0x300
acls:
    drop_non_ospf_ipv4:
        - rule:
            nw_dst: '224.0.0.5'
            dl_type: 0x800
            actions:
                allow: 1
        - rule:
            dl_type: 0x800
            actions:
                allow: 0
""" % DP1_CONFIG

        drop_match = {
            'in_port': 2,
            'vlan_vid': 0,
            'eth_type': 0x800,
            'ipv4_dst': '192.0.2.1'}
        accept_match = {
            'in_port': 2,
            'vlan_vid': 0,
            'eth_type': 0x800,
            'ipv4_dst': '224.0.0.5'}
        # base case
        for match in (drop_match, accept_match):
            self.assertTrue(
                self.table.is_output(match, port=3, vid=self.V200),
                msg='Packet not output before adding ACL')

        self.update_config(acl_config, reload_type='cold')
        self.flap_port(2)
        self.assertFalse(
            self.table.is_output(drop_match),
            msg='Packet not blocked by ACL')
        self.assertTrue(
            self.table.is_output(accept_match, port=3, vid=self.V200),
            msg='Packet not allowed by ACL')


class ValveEgressACLTestCase(ValveTestBases.ValveTestSmall):
    """Test ACL drop/allow and reloading."""

    def setUp(self):
        self.setup_valve(CONFIG)

    def test_vlan_acl_deny(self):
        """Test VLAN ACL denies a packet."""
        ALLOW_HOST_V6 = 'fc00:200::1:1'
        DENY_HOST_V6 = 'fc00:200::1:2'
        FAUCET_V100_VIP = 'fc00:100::1'
        FAUCET_V200_VIP = 'fc00:200::1'
        acl_config = """
dps:
    s1:
{dp1_config}
        interfaces:
            p1:
                number: 1
                native_vlan: v100
            p2:
                number: 2
                native_vlan: v200
                tagged_vlans: [v100]
            p3:
                number: 3
                tagged_vlans: [v100, v200]
            p4:
                number: 4
                tagged_vlans: [v200]
vlans:
    v100:
        vid: 0x100
        faucet_mac: '{mac}'
        faucet_vips: ['{v100_vip}/64']
    v200:
        vid: 0x200
        faucet_mac: '{mac}'
        faucet_vips: ['{v200_vip}/64']
        acl_out: drop_non_allow_host_v6
        minimum_ip_size_check: no
routers:
    r_v100_v200:
        vlans: [v100, v200]
acls:
    drop_non_allow_host_v6:
        - rule:
            ipv6_dst: '{allow_host}'
            eth_type: 0x86DD
            actions:
                allow: 1
        - rule:
            eth_type: 0x86DD
            actions:
                allow: 0
""".format(dp1_config=DP1_CONFIG, mac=FAUCET_MAC, v100_vip=FAUCET_V100_VIP,
           v200_vip=FAUCET_V200_VIP, allow_host=ALLOW_HOST_V6)

        l2_drop_match = {
            'in_port': 2,
            'eth_dst': self.P3_V200_MAC,
            'vlan_vid': 0,
            'eth_type': 0x86DD,
            'ipv6_dst': DENY_HOST_V6}
        l2_accept_match = {
            'in_port': 3,
            'eth_dst': self.P2_V200_MAC,
            'vlan_vid': 0x200 | ofp.OFPVID_PRESENT,
            'eth_type': 0x86DD,
            'ipv6_dst': ALLOW_HOST_V6}
        v100_accept_match = {'in_port': 1, 'vlan_vid': 0}

        # base case
        for match in (l2_drop_match, l2_accept_match):
            self.assertTrue(
                self.table.is_output(match, port=4),
                msg='Packet not output before adding ACL')

        # multicast
        self.update_config(acl_config, reload_type='cold')
        self.assertTrue(
            self.table.is_output(v100_accept_match, port=3),
            msg='Packet not output when on vlan with no ACL'
            )
        self.assertFalse(
            self.table.is_output(l2_drop_match, port=3),
            msg='Packet not blocked by ACL')
        self.assertTrue(
            self.table.is_output(l2_accept_match, port=2),
            msg='Packet not allowed by ACL')

        # unicast
        self.rcv_packet(2, 0x200, {
            'eth_src': self.P2_V200_MAC,
            'eth_dst': self.P3_V200_MAC,
            'vid': 0x200,
            'ipv6_src': ALLOW_HOST_V6,
            'ipv6_dst': DENY_HOST_V6,
            'neighbor_advert_ip': ALLOW_HOST_V6,
            })
        self.rcv_packet(3, 0x200, {
            'eth_src': self.P3_V200_MAC,
            'eth_dst': self.P2_V200_MAC,
            'vid': 0x200,
            'ipv6_src': DENY_HOST_V6,
            'ipv6_dst': ALLOW_HOST_V6,
            'neighbor_advert_ip': DENY_HOST_V6,
            })

        self.assertTrue(
            self.table.is_output(l2_accept_match, port=2),
            msg='Packet not allowed by ACL')
        self.assertFalse(
            self.table.is_output(l2_drop_match, port=3),
            msg='Packet not blocked by ACL')

        # l3
        l3_drop_match = {
            'in_port': 1,
            'eth_dst': FAUCET_MAC,
            'vlan_vid': 0,
            'eth_type': 0x86DD,
            'ipv6_dst': DENY_HOST_V6}
        l3_accept_match = {
            'in_port': 1,
            'eth_dst': FAUCET_MAC,
            'vlan_vid': 0,
            'eth_type': 0x86DD,
            'ipv6_dst': ALLOW_HOST_V6}

        self.assertTrue(
            self.table.is_output(l3_accept_match, port=2),
            msg='Routed packet not allowed by ACL')
        self.assertFalse(
            self.table.is_output(l3_drop_match, port=3),
            msg='Routed packet not blocked by ACL')


class ValveReloadConfigProfile(ValveTestBases.ValveTestSmall):
    """Test reload processing time."""

    CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                native_vlan: 0x100
""" % DP1_CONFIG

    def setUp(self):
        pstats_out, _ = self.profile(partial(self.setup_valve, self.CONFIG))
        self.baseline_total_tt = pstats_out.total_tt  # pytype: disable=attribute-error

    def test_profile_reload(self):
        """Test reload processing time."""
        for i in range(2, 100):
            self.CONFIG += """
            p%u:
                number: %u
                native_vlan: 0x100
""" % (i, i)
        pstats_out, pstats_text = self.profile(
            partial(self.update_config, self.CONFIG, reload_type='cold'))
        total_tt_prop = pstats_out.total_tt / self.baseline_total_tt  # pytype: disable=attribute-error
        # must not be 50x slower, to ingest config for 100 interfaces than 1.
        self.assertLessEqual(total_tt_prop, 50, msg=pstats_text)


class ValveTestConfigHash(ValveTestBases.ValveTestSmall):
    """Verify faucet_config_hash_info update after config change"""

    CONFIG = """
dps:
    s1:
%s
        interfaces:
            p1:
                number: 1
                native_vlan: 0x100
""" % DP1_CONFIG

    def setUp(self):
        self.setup_valve(self.CONFIG)

    def _get_info(self, metric, name):
        """"Return (single) info dict for metric"""
        # There doesn't seem to be a nice API for this,
        # so we use the prometheus client internal API
        metrics = list(metric.collect())
        self.assertEqual(len(metrics), 1)
        samples = metrics[0].samples
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual(sample.name, name)
        return sample.labels

    def _check_hashes(self):
        """Verify and return faucet_config_hash_info labels"""
        labels = self._get_info(metric=self.metrics.faucet_config_hash,
                                name='faucet_config_hash_info')
        files = labels['config_files'].split(',')
        hashes = labels['hashes'].split(',')
        self.assertTrue(len(files) == len(hashes) == 1)
        self.assertEqual(files[0], self.config_file, 'wrong config file')
        hash_value = config_parser_util.config_file_hash(self.config_file)
        self.assertEqual(hashes[0], hash_value, 'hash validation failed')
        return labels

    def _change_config(self):
        """Change self.CONFIG"""
        if '0x100' in self.CONFIG:
            self.CONFIG = self.CONFIG.replace('0x100', '0x200')
        else:
            self.CONFIG = self.CONFIG.replace('0x200', '0x100')
        self.update_config(self.CONFIG, reload_expected=True)
        return self.CONFIG

    def test_config_hash_func(self):
        """Verify that faucet_config_hash_func is set correctly"""
        labels = self._get_info(metric=self.metrics.faucet_config_hash_func,
                                name='faucet_config_hash_func')
        hash_funcs = list(labels.values())
        self.assertEqual(len(hash_funcs), 1, "found multiple hash functions")
        hash_func = hash_funcs[0]
        # Make sure that it matches and is supported in hashlib
        self.assertEqual(hash_func, config_parser_util.CONFIG_HASH_FUNC)
        self.assertTrue(hash_func in hashlib.algorithms_guaranteed)

    def test_config_hash_update(self):
        """Verify faucet_config_hash_info is properly updated after config"""
        # Verify that hashes change after config is changed
        old_config = self.CONFIG
        old_hashes = self._check_hashes()
        starting_hashes = old_hashes
        self._change_config()
        new_config = self.CONFIG
        self.assertNotEqual(old_config, new_config, 'config not changed')
        new_hashes = self._check_hashes()
        self.assertNotEqual(old_hashes, new_hashes,
                            'hashes not changed after config change')
        # Verify that hashes don't change after config isn't changed
        old_hashes = new_hashes
        self.update_config(self.CONFIG, reload_expected=False)
        new_hashes = self._check_hashes()
        self.assertEqual(old_hashes, new_hashes,
                         "hashes changed when config didn't")
        # Verify that hash is restored when config is restored
        self._change_config()
        new_hashes = self._check_hashes()
        self.assertEqual(new_hashes, starting_hashes,
                         'hashes should be restored to starting values')


class ValveTestConfigRevert(ValveTestBases.ValveTestSmall):

    CONFIG = """
dps:
    s1:
        dp_id: 0x1
        hardware: 'GenericTFM'
        interfaces:
            p1:
                number: 1
                native_vlan: 0x100
"""

    CONFIG_AUTO_REVERT = True

    def setUp(self):
        self.setup_valve(self.CONFIG)

    def test_config_revert(self):
        """Verify config is automatically reverted if bad."""
        self.assertEqual(self.get_prom('faucet_config_load_error', bare=True), 0)
        self.update_config('***broken***', reload_expected=True, error_expected=1)
        self.assertEqual(self.get_prom('faucet_config_load_error', bare=True), 1)
        with open(self.config_file, 'r') as config_file:
            config_content = config_file.read()
        self.assertEqual(self.CONFIG, config_content)
        self.update_config(self.CONFIG + '\n', reload_expected=False, error_expected=0)
        more_config = self.CONFIG + """
            p2:
                number: 2
                native_vlan: 0x100
        """
        self.update_config(more_config, reload_expected=True, error_expected=0)


class ValveTestConfigApplied(ValveTestBases.ValveTestSmall):
    """Test cases for faucet_config_applied."""

    CONFIG = """
dps:
    s1:
        dp_id: 0x1
        hardware: 'GenericTFM'
        interfaces:
            p1:
                number: 1
                native_vlan: 0x100
"""

    def setUp(self):
        self.setup_valve(self.CONFIG)

    def test_config_applied_update(self):
        """Verify that config_applied increments after DP connect"""
        # 100% for a single datapath
        self.assertEqual(self.get_prom('faucet_config_applied', bare=True), 1.0)
        # Add a second datapath, which currently isn't programmed
        self.CONFIG += """
    s2:
        dp_id: 0x2
        interfaces:
            p1:
                number: 1
                native_vlan: 0x100
"""
        self.update_config(self.CONFIG, reload_expected=False)
        # Should be 50%
        self.assertEqual(self.get_prom('faucet_config_applied', bare=True), .5)
        # We don't have a way to simulate the second datapath connecting,
        # we update the statistic manually
        self.valves_manager.update_config_applied({0x2: True})
        # Should be 100% now
        self.assertEqual(self.get_prom('faucet_config_applied', bare=True), 1.0)


class ValveReloadConfigTestCase(ValveTestBases.ValveTestBig):
    """Repeats the tests after a config reload."""

    def setUp(self):
        super(ValveReloadConfigTestCase, self).setUp()
        self.flap_port(1)
        self.update_config(CONFIG, reload_type='warm', reload_expected=False)


if __name__ == "__main__":
    unittest.main()  # pytype: disable=module-attr
