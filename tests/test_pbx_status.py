"""Asterisk PBX status over SSH (pbx_status.py, TODO #79)."""

from datetime import datetime, timedelta

import pytest

import config
import database
import gateway
import pbx_status as px
import ssh_collect

M = ssh_collect.MARK
T0 = datetime(2026, 11, 11, 12, 0, 0)

ENDPOINTS = """
 Endpoint:  <Endpoint/CID.....................................>  <State.....>  <Channels.>
    I/OAuth:  <AuthId/UserName...........................................................>
        Aor:  <Aor............................................>  <MaxContact>
      Contact:  <Aor/ContactUri..........................> <Hash....> <Status> <RTT(ms)..>
  Transport:  <TransportId........>  <Type>  <cos>  <tos>  <BindAddress..................>
   Identify:  <Identify/Endpoint.........................................................>
        Match:  <criteria.........................>
    Channel:  <ChannelId......................................>  <State.....>  <Time.....>
        Exten: <DialedExten...........>  CLCID: <ConnectedLineCID.......>
==========================================================================================

 Endpoint:  101/101                                              In use        1 of inf
     InAuth:  101-auth/101
        Aor:  101                                                1
      Contact:  101/sip:101@10.42.92.104:5060;ob           c0ffee1234 Avail        12.345
  Transport:  0.0.0.0-udp               udp      0      0  0.0.0.0:5060

 Endpoint:  102/102                                              Unavailable   0 of inf
     InAuth:  102-auth/102
        Aor:  102                                                1

 Endpoint:  103                                                  Not in use    0 of inf
        Aor:  103                                                1
      Contact:  103/sip:103@10.95.35.210:5062              0badc0de99 Unavail         nan

Objects found: 3
"""

PEERS = """Name/username             Host                                    Dyn Forcerport Comedia    ACL Port     Status      Description
201/201                   10.42.92.118                             D  Auto (No)  No             A  5060     OK (8 ms)
202/202                   (Unspecified)                            D  Auto (No)  No             A  0        UNKNOWN
101                       10.1.1.1                                 D  Auto (No)  No             A  5060     OK (1 ms)
3 sip peers [Monitored: 1 online, 1 offline Unmonitored: 0 online, 0 offline]
"""

CHANNELS = ("PJSIP/101-00000012!from-internal!102!1!Up!Dial!PJSIP/102,,Tt!101!!!3!95!a1b2!1731340000.18\n"
            "PJSIP/201-00000013!from-internal!*43!3!Up!Echo!!201!!!3!12!!1731340100.19\n")
CALLS = "2 active channels\n1 active call\n57 calls processed\n"


def output(endpoints=ENDPOINTS, peers=PEERS, version='Asterisk 20.9.3 built by root'):
    return (f"{M} version\n{version}\n{M} calls\n{CALLS}{M} channels\n{CHANNELS}"
            f"{M} endpoints\n{endpoints}\n{M} peers\n{peers}\n{M} end\n")


def test_parse_pjsip_endpoints():
    eps = {e['name']: e for e in px.parse_pjsip_endpoints(ENDPOINTS)}
    assert set(eps) == {'101', '102', '103'}
    assert eps['101'] == {'name': '101', 'tech': 'PJSIP', 'state': 'In use', 'registered': True,
                          'contact_ip': '10.42.92.104', 'rtt_ms': 12.345}
    assert not eps['102']['registered'] and eps['102']['contact_ip'] is None
    assert eps['103']['state'] == 'Not in use' and not eps['103']['registered']
    assert eps['103']['contact_ip'] == '10.95.35.210' and eps['103']['rtt_ms'] is None


def test_parse_sip_peers_skips_header_and_summary():
    peers = {p['name']: p for p in px.parse_sip_peers(PEERS)}
    assert set(peers) == {'201', '202', '101'}
    assert peers['201']['registered'] and peers['201']['rtt_ms'] == 8.0 and peers['201']['contact_ip'] == '10.42.92.118'
    assert not peers['202']['registered'] and peers['202']['contact_ip'] is None


def test_parse_channels_and_calls():
    ch = px.parse_channels(CHANNELS)
    assert ch[0]['channel'] == 'PJSIP/101-00000012' and ch[0]['exten'] == '102' and ch[0]['app'] == 'Dial'
    assert ch[0]['duration'] == 95 and ch[0]['bridge'] == 'a1b2' and ch[1]['bridge'] is None
    assert px.parse_calls(CALLS) == {'active_channels': 2, 'active_calls': 1, 'calls_processed': 57}


def test_parse_output_merges_pjsip_first():
    res = px.parse_output(output())
    assert res['ok'] and res['version'] == 'Asterisk 20.9.3 built by root'
    names = [(e['name'], e['tech']) for e in res['endpoints']]
    # 101 exists in both: the PJSIP endpoint wins
    assert names == [('101', 'PJSIP'), ('102', 'PJSIP'), ('103', 'PJSIP'), ('201', 'SIP'), ('202', 'SIP')]


def test_parse_output_asterisk_refused_or_cut():
    res = px.parse_output(output(version='Unable to connect to remote asterisk (does /var/run/asterisk/asterisk.ctl exist?)'))
    assert not res['ok'] and res['error_kind'] == 'asterisk'
    assert px.parse_output(output().replace(f'{M} end\n', '')) == {'ok': False}


def test_contact_ip():
    assert px.contact_ip('sip:101@10.1.2.3:5060;ob') == '10.1.2.3'
    assert px.contact_ip('sip:101@phone.local.mesh') == 'phone.local.mesh'
    assert px.contact_ip('sip:101@[fe80::1]:5060') == 'fe80::1'


@pytest.mark.parametrize('entry', [
    {'name': 'eoc', 'host': '-oProxyCommand=x'},
    {'name': 'eoc', 'host': '10.1.1.1', 'user': '-l'},
    {'name': '../x', 'host': '10.1.1.1'},
    {'name': 'eoc', 'host': '10.1.1.1', 'port': 70000},
    {'name': 'eoc', 'host': 'a b'},
])
def test_validate_rejects_unsafe(entry):
    with pytest.raises(ValueError):
        px.validate_host(entry)


def test_ssh_args_end_options_before_host():
    args = px.ssh_args(px.validate_host({'name': 'eoc', 'host': '10.95.35.209', 'port': 22, 'user': 'w0gq'}))
    i = args.index('--')
    assert args[i + 1:] == ['10.95.35.209', 'sh -s'] and args[args.index('-l') + 1] == 'w0gq'
    assert 'HostKeyAlias=pbx-eoc' in args and 'BatchMode=yes' in args


def test_save_hosts_defaults_and_duplicates(mesh):
    saved = px.save_hosts([{'name': 'gs_pbx_eoc', 'host': '10.95.35.209'}])
    assert saved == [{'name': 'gs_pbx_eoc', 'host': '10.95.35.209', 'port': config.PBX_SSH_PORT,
                      'user': config.PBX_SSH_USER}]
    assert px.get_hosts() == saved
    with pytest.raises(ValueError):
        px.save_hosts([{'name': 'a', 'host': '10.0.0.1'}, {'name': 'A', 'host': '10.0.0.2'}])


@pytest.fixture
def pbx(mesh, monkeypatch, tmp_path):
    mesh.node('w0aa-mmc-ac2', ip='10.42.92.97')
    mesh.node('w0aa-eoc-ac3', ip='10.95.35.193')
    monkeypatch.setattr(gateway, 'is_reachable', lambda: True)
    monkeypatch.setattr(gateway, 'failure_is_ours', lambda: False)
    key = tmp_path / 'key'
    key.write_text('x')
    monkeypatch.setattr(config, 'SSH_KEY_PATH', str(key))
    monkeypatch.setattr(px, '_tables_ready', False)
    px.save_hosts([{'name': 'gs_pbx_eoc', 'host': '10.95.35.209'}])
    state = {'out': output(), 'err': ''}
    monkeypatch.setattr(px, '_run_ssh', lambda p, script: (0, state['out'], state['err']))
    return state


def kinds(result):
    return [(e['event_type'], e['node_name']) for e in result['events']]


def test_check_stores_status_and_endpoints(pbx):
    r = px.run(now=T0)
    assert r['aborted'] is None and r['results'][0]['ok'] and r['events'] == []
    st = px.status()['pbxes'][0]
    assert st['version'].startswith('Asterisk 20') and st['active_calls'] == 1 and st['calls_processed'] == 57
    assert len(st['channels']) == 2 and st['registered'] == 2 and st['unregistered_alarm'] == 0
    ep = {e['endpoint']: e for e in st['endpoints']}
    assert ep['101']['node_name'] == 'w0aa-mmc-ac2' and ep['101']['last_registered']
    assert ep['102']['last_registered'] is None


def test_phone_drop_alarm_and_recovery(pbx):
    px.run(now=T0)
    dropped = ENDPOINTS.replace('c0ffee1234 Avail        12.345', 'c0ffee1234 Unavail         nan')
    pbx['out'] = output(endpoints=dropped)
    r1 = px.run(now=T0 + timedelta(minutes=5))
    assert r1['events'] == []  # one check is not enough
    r2 = px.run(now=T0 + timedelta(minutes=10))
    assert kinds(r2) == [('pbx_phone_unregistered', 'w0aa-mmc-ac2')]
    assert '101 (PJSIP) on PBX gs_pbx_eoc' in r2['events'][0]['details']
    assert 'last from 10.42.92.104 on node w0aa-mmc-ac2' in r2['events'][0]['details']
    assert px.run(now=T0 + timedelta(minutes=15))['events'] == []  # no repeat
    pbx['out'] = output()
    r = px.run(now=T0 + timedelta(minutes=20))
    assert kinds(r) == [('pbx_phone_registered', 'w0aa-mmc-ac2')]
    # spare extensions that never registered (102) never alarm
    with database.get_connection() as conn:
        assert conn.execute("SELECT alarm FROM pbx_endpoints WHERE endpoint = '102'").fetchone()[0] == 0


def test_pbx_unreachable_and_hints(pbx):
    pbx['out'], pbx['err'] = '', 'root@10.95.35.209: Permission denied (publickey).'
    r = px.run(now=T0)
    assert r['results'][0]['error_kind'] == 'auth' and 'authorized_keys' in r['results'][0]['hint']
    r = px.run(now=T0 + timedelta(minutes=5))
    assert kinds(r) == [('pbx_unreachable', 'w0aa-eoc-ac3')]  # the PBX's address is on that node's LAN
    pbx['out'], pbx['err'] = output(), ''
    assert kinds(px.run(now=T0 + timedelta(minutes=10))) == [('pbx_restored', 'w0aa-eoc-ac3')]


def test_failure_discarded_when_starting_node_lost(pbx, monkeypatch):
    monkeypatch.setattr(gateway, 'failure_is_ours', lambda: True)
    pbx['out'], pbx['err'] = '', 'ssh: connect to host 10.95.35.209 port 22: Connection timed out'
    for i in range(3):
        r = px.run(now=T0 + timedelta(minutes=5 * i))
        assert r['results'][0].get('discarded') and r['events'] == []


def test_removed_pbx_forgets_its_rows(pbx):
    px.run(now=T0)
    px.save_hosts([])
    with database.get_connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM pbx_endpoints').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM pbx_status').fetchone()[0] == 0
    assert px.run(now=T0)['aborted'] == 'no PBX configured'
