"""Node traffic over SSH (ssh_traffic.py): parsing and classification of the
read-only script's output. No SSH is run."""

import ssh_collect
import ssh_traffic as t

M = t.MARK
SAMPLE = f"""{M} addr
1: lo    inet 127.0.0.1/8 scope host lo
5: br-lan    inet 10.204.160.33/27 brd 10.204.160.63 scope global br-lan
9: wlan1    inet 10.103.42.159/16 brd 10.103.255.255 scope global wlan1
12: wg0    inet 172.31.10.2/30 scope global wg0
{M} roles
wan wlan1
lan br-lan
wifi br-wifi
dtdlink br-dtdlink
{M} dev1
1000.00 900.00
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
 wlan1: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0
 br-lan: 500 5 0 0 0 0 0 0 600 6 0 0 0 0 0 0
{M} wg1
wg0 PUBKEYabcdefghij= 100 200
{M} dev2
1005.00 900.00
Inter-|   Receive |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
 wlan1: 6000 60 0 0 0 0 0 0 52000 520 0 0 0 0 0 0
 br-lan: 1500 15 0 0 0 0 0 0 1600 16 0 0 0 0 0 0
{M} wg2
wg0 PUBKEYabcdefghij= 5100 10200
{M} wgend
wg0 PUBKEYabcdefghij= 203.0.113.5:5525
{M} acct
1
{M} conntrack
ipv4     2 udp      17 170 src=10.204.160.38 dst=10.10.20.30 sport=5060 dport=5060 packets=50 bytes=30000 src=10.10.20.30 dst=10.204.160.38 sport=5060 dport=5060 packets=40 bytes=20000 [ASSURED] mark=0 use=2
ipv4     2 tcp      6 7400 ESTABLISHED src=10.51.2.3 dst=142.250.1.1 sport=40000 dport=443 packets=10 bytes=900 src=142.250.1.1 dst=10.103.42.159 sport=443 dport=40000 packets=9 bytes=5000 [ASSURED] mark=0 use=1
ipv4     2 udp      17 20 src=10.103.42.159 dst=203.0.113.5 sport=5525 dport=5525 packets=1 bytes=100 [UNREPLIED] src=203.0.113.5 dst=10.103.42.159 sport=5525 dport=5525 packets=0 bytes=0 mark=0 use=1
{M} leases
1700000000 aa:bb:cc:dd:ee:ff 10.204.160.38 MercyPhone *
{M} end
"""


def _run():
    return t.analyse(ssh_collect.split_sections(SAMPLE), 5, names={'10.10.20.30': 'pbx'})


def test_interface_rates_and_roles():
    r = _run()
    wan = next(i for i in r['interfaces'] if i['name'] == 'wlan1')
    assert wan['role'] == 'wan' and r['seconds'] == 5.0
    assert wan['tx_bps'] == 50000 * 8 / 5 and wan['rx_pps'] == 10.0
    assert next(i for i in r['interfaces'] if i['name'] == 'br-lan')['role'] == 'lan'


def test_wireguard_throughput():
    w = _run()['wireguard'][0]
    assert w['iface'] == 'wg0' and w['endpoint'] == '203.0.113.5:5525'
    assert w['rx_bps'] == 5000 * 8 / 5 and w['tx_bps'] == 10000 * 8 / 5


def test_flows_classified_and_named():
    r = _run()
    assert r['conntrack']['accounting'] and r['conntrack']['count'] == 3
    sip, web, wg = r['flows']   # sorted by bytes
    assert (sip['src_name'], sip['src_class'], sip['dst_name'], sip['dst_class'], sip['service']) == \
        ('MercyPhone', 'lan', 'pbx', 'mesh', 'SIP')
    assert web['state'] == 'ESTABLISHED' and web['nat'] and web['via'] and web['dst_class'] == 'internet'
    assert web['src_class'] == 'mesh'
    assert wg['src_class'] == 'node' and wg['unreplied'] and wg['service'] == 'WireGuard tunnel (wg0)'
    kinds = {s['kind'] for s in r['summary']}
    assert 'mesh → internet (NAT via WAN)' in kinds and 'LAN → mesh' in kinds


def test_no_key(monkeypatch):
    monkeypatch.setattr(ssh_collect, 'key_present', lambda: False)
    assert t.traffic('x')['error_kind'] == 'no_key'


def test_explain_lines():
    r = _run()
    homes = [(t.ipaddress.ip_address('10.10.20.25'), 'pbx-node')]
    lines = t.explain(r, 'mmc', homes)
    assert any(l.startswith('WireGuard tunnel wg0 to 203.0.113.5:5525') for l in lines)
    assert any('LAN host MercyPhone talks to pbx' in l and "pbx-node's LAN" in l for l in lines)
    assert any('leave through this node' in l for l in lines)
    assert t.home_of('10.10.21.30', homes) is None
