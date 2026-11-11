"""Global search (search.py, TODO #62)."""

import pytest

import database
import search


@pytest.fixture
def mesh_with_things(mesh):
    mesh.node('w0aa-col-rkt-s', ip='10.1.2.1', model='Ubiquiti Rocket 5AC Lite', description='Collins south sector')
    mesh.node('w0aa-col-rkt-n', ip='10.1.3.1', model='Ubiquiti Rocket 5AC Lite')
    mesh.node('w0aa-mmc-ac2', ip='10.42.92.97', model='MikroTik hAP ac2')
    mesh.node('kb0zz-home', ip='10.9.9.1', is_active=0)
    mesh.infra('w0aa-col-rkt-s', 'COL')
    mesh.infra('w0aa-col-rkt-n', 'COL')
    mesh.infra('w0aa-mmc-ac2', 'MMC')
    mesh.link('w0aa-col-rkt-s', 'kb0zz-home', mac_address='48:a9:8a:19:f7:ee', both=False)
    import phones
    phones.ensure_tables()
    with database.get_connection() as conn:
        conn.executemany('INSERT INTO mesh_hosts (ip, name, first_seen, last_seen) VALUES (?, ?, ?, ?)', [
            ('10.42.92.108', 'w0aa-ubuntu', 'x', 'x'),           # a LAN device of w0aa-mmc-ac2
            ('10.42.92.97', 'W0AA-MMC-AC2', 'x', 'x'),           # the node itself: not a device
            ('10.42.92.98', 'dtdlink.w0aa-mmc-ac2', 'x', 'x'),   # node alias: not a device
        ])
        conn.execute("INSERT INTO services (node_name, name, protocol, link, ip) VALUES "
                     "('w0aa-mmc-ac2', 'MeshChat [chat]', 'tcp', 'http://w0aa-ubuntu:80/meshchat', '10.42.92.108')")
    return mesh


def kinds(q):
    return [(r['type'], r['label']) for r in search.search(q)['results']]


def test_too_short_or_empty(mesh):
    assert search.search('')['results'] == []
    assert search.search(' w ')['results'] == []


def test_callsign_then_its_nodes(mesh_with_things):
    res = search.search('w0aa')['results']
    assert res[0]['type'] == 'callsign' and res[0]['label'] == 'W0AA'
    assert res[0]['detail'] == '3 nodes, 3 active'
    assert res[0]['url'] == '/?nodes-search=w0aa#/nodes'
    assert {r['label'] for r in res if r['type'] == 'node'} == {'w0aa-col-rkt-s', 'w0aa-col-rkt-n', 'w0aa-mmc-ac2'}


def test_exact_node_name_first(mesh_with_things):
    res = search.search('W0AA-COL-RKT-S')['results']
    assert res[0] == {'type': 'node', 'label': 'w0aa-col-rkt-s',
                      'detail': '10.1.2.1 · Ubiquiti Rocket 5AC Lite · site COL · active',
                      'url': '/nodes/w0aa-col-rkt-s', 'score': 102}


def test_site_by_code_and_name(mesh_with_things):
    assert kinds('col')[0] == ('site', 'COL — Collins Tower')
    res = search.search('mercy')['results']
    assert res[0]['label'] == 'MMC — Mercy Medical Center' and res[0]['url'] == '/sites/MMC'
    assert res[0]['detail'] == '1 infrastructure node'


def test_ip_exact_and_prefix(mesh_with_things):
    assert kinds('10.1.2.1')[0] == ('node', 'w0aa-col-rkt-s')
    found = kinds('10.42.92')
    assert ('node', 'w0aa-mmc-ac2') in found and ('device', 'w0aa-ubuntu') in found
    # the node's own host entries are not listed as devices
    assert ('device', 'W0AA-MMC-AC2') not in found and ('device', 'dtdlink.w0aa-mmc-ac2') not in found


def test_device_points_to_its_node(mesh_with_things):
    dev = next(r for r in search.search('ubuntu')['results'] if r['type'] == 'device')
    assert dev['detail'] == '10.42.92.108 on w0aa-mmc-ac2' and dev['url'] == '/nodes/w0aa-mmc-ac2'


def test_mac_any_separator(mesh_with_things):
    for q in ('48:a9:8a:19:f7:ee', '48-A9-8A-19-F7-EE', '48a98a19f7ee'):
        res = search.search(q)['results']
        assert res[0]['type'] == 'mac' and res[0]['url'] == '/nodes/kb0zz-home', q
        assert res[0]['detail'] == 'radio of kb0zz-home (heard by w0aa-col-rkt-s)'
    assert search.search('48:a9')['results'][0]['type'] == 'mac'
    # a callsign or plain number is not taken for a MAC
    assert not search._looks_like_mac('w0aa') and not search._looks_like_mac('123456')


def test_service(mesh_with_things):
    svc = next(r for r in search.search('meshchat')['results'] if r['type'] == 'service')
    assert svc['label'] == 'MeshChat [chat]' and svc['url'] == '/nodes/w0aa-mmc-ac2'


def test_limit_and_per_type_cap(mesh_with_things):
    for i in range(20):
        mesh_with_things.node(f'w0aa-extra-{i:02d}')
    res = search.search('w0aa-extra')['results']
    assert len(res) == search.PER_TYPE
    assert len(search.search('w0aa', limit=3)['results']) == 3
