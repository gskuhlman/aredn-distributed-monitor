"""Radio firmware read over SSH (ssh_health.radio_firmware) and the Settings
Drift check: a firmware file replaced in the overlay, or a radio whose
firmware differs from others with the same chip on the same AREDN release."""

from conftest import ts
import drift
import ssh_collect as sc
import ssh_health

CT = ('[   14.27] ath10k_ahb a800000.wifi: qca4019 hw1.0 target 0x01000000 chip_id 0x003b00ff sub 0000:0000\n'
      '[   14.28] ath10k_ahb a800000.wifi: firmware ver 10.4-ddwrt-4019-tW-13-7768 api 5 features '
      'mfp,peer-flow-ctrl,txstatus-noack,wmi-10.x-CT,ratemask-CT crc32 f0941592\n')
STOCK = ('[   14.66] ath10k_ahb a800000.wifi: qca4019 hw1.0 target 0x01000000 chip_id 0x003b00ff sub 0000:0000\n'
         '[   14.67] ath10k_ahb a800000.wifi: firmware ver 10.4-3.6-00140 api 5 features '
         'no-p2p,mfp,peer-flow-ctrl,btcoex-param,allows-mesh-bcast,no-ps crc32 ba79b746\n')
FILE = '/lib/firmware/ath10k/QCA4019/hw1.0/firmware-5.bin'
PCI = ('[   18.79] ath10k 6.15 driver, optimized for CT firmware, probing pci device: 0x11ac.\n'
       '[   20.79] ath10k_pci 0000:00:00.0: qca988x hw2.0 ubiquiti target 0x4100016c chip_id 0x043222ff\n'
       '[   20.80] ath10k_pci 0000:00:00.0: firmware ver 10.1-ct-8x-__fH-023-23ea9f8e api 2 features '
       'wmi-10.x,mfp,wmi-10.x-CT,txrate-CT crc32 23ea9f8e\n')


def snap(mesh, node, kind, text, minutes_ago=5):
    sc.ensure_tables()
    mesh.sql('INSERT INTO ssh_snapshots (node_name, timestamp, kind, text) VALUES (?, ?, ?, ?)',
             (node, ts(minutes_ago), kind, text))


def radios(mesh):
    for n in ('w0aa-col-sq-2mmc', 'w0aa-mmc-sq-2col', 'w0aa-col-sq-2bldg', 'w0aa-eoc-ac3'):
        mesh.node(n, model='MikroTik SXTsq 5 ac', firmware_version='4.26.10.0', lat=42.0, lon=-91.6)
    mesh.node('w0aa-col-rkt-e', model='Ubiquiti Rocket 5AC Lite', firmware_version='4.26.10.0', lat=42.0, lon=-91.6)


def test_parse_radio_firmware():
    p = ssh_health.parse_radio_firmware(STOCK + f'd56944163e5e257cd2b65739d36401f3  {FILE}\n'
                                                f'size {FILE} 583344\noverlay {FILE}\n')
    assert p['radios'] == [{'device': 'a800000.wifi', 'chip': 'qca4019 hw1.0', 'driver': 'ath10k',
                            'version': '10.4-3.6-00140', 'ct': False,
                            'features': ['no-p2p', 'mfp', 'peer-flow-ctrl', 'btcoex-param', 'allows-mesh-bcast', 'no-ps']}]
    assert p['files'] == [{'path': FILE, 'md5': 'd56944163e5e257cd2b65739d36401f3', 'size': 583344, 'overlay': True}]
    pci = ssh_health.parse_radio_firmware(PCI)
    assert pci['driver_ct'] and pci['radios'][0]['chip'] == 'qca988x hw2.0' and pci['radios'][0]['ct']


def test_reader_prefers_radiofw_and_keeps_older_version_lines(mesh):
    radios(mesh)
    snap(mesh, 'W0AA-COL-SQ-2MMC', 'dmesg', STOCK, 60)
    # The newest radiofw snapshot lost the boot lines (long uptime) but has the files.
    snap(mesh, 'w0aa-col-sq-2mmc', 'radiofw', f'd569  {FILE}\nsize {FILE} 583344\noverlay {FILE}\n', 5)
    fw = ssh_health.radio_firmware()['w0aa-col-sq-2mmc']
    assert fw['source'] == 'radiofw'
    assert fw['radios'][0]['version'] == '10.4-3.6-00140'
    assert fw['files'][0]['overlay'] is True


def test_drift_flags_odd_and_replaced_radio_firmware(mesh):
    radios(mesh)
    snap(mesh, 'w0aa-col-sq-2bldg', 'dmesg', CT)
    snap(mesh, 'w0aa-eoc-ac3', 'dmesg', CT)
    snap(mesh, 'w0aa-col-sq-2mmc', 'dmesg', STOCK)        # versions only: no file info yet
    snap(mesh, 'w0aa-mmc-sq-2col', 'radiofw', STOCK + f'd56944163e5e257cd2b65739d36401f3  {FILE}\n'
                                                       f'size {FILE} 583344\noverlay {FILE}\n')
    snap(mesh, 'w0aa-col-rkt-e', 'dmesg', PCI)
    r = drift.run_report('all')
    fw = {x['title']: x for x in r['findings'] if x['category'] == 'radio' and 'radio firmware' in x['title']}
    titles = sorted(fw)
    assert titles == ['w0aa-col-sq-2mmc: radio firmware 10.4-3.6-00140 differs from other qca4019 hw1.0 radios',
                      'w0aa-mmc-sq-2col: radio firmware file replaced'], titles
    odd = fw[titles[0]]
    assert odd['severity'] == 'warning' and 'not a CT build' in odd['detail']
    # Only radios on the reference build are named as the others.
    assert any('w0aa-col-sq-2bldg, w0aa-eoc-ac3' in e and 'mmc-sq-2col' not in e for e in odd['evidence'])
    assert any('replaced in the overlay' in e for e in fw[titles[1]]['evidence'])
    rows = {x['name']: x for x in r['radio_firmware']}
    assert rows['w0aa-col-sq-2bldg']['differs'] is False and rows['w0aa-col-sq-2bldg']['expected'].startswith('10.4-ddwrt')
    assert rows['w0aa-mmc-sq-2col']['replaced'] and rows['w0aa-mmc-sq-2col']['md5'].startswith('d569')
    assert rows['w0aa-col-rkt-e']['differs'] is False


def test_no_ssh_data_means_no_rows(mesh):
    radios(mesh)
    r = drift.run_report('all')
    assert r['radio_firmware'] == []
    assert not [x for x in r['findings'] if 'radio firmware' in x['title']]
