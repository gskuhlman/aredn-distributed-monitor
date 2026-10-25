"""Small pure helpers: WiFi survey parsers, service probe targets, change
verdict summaries, hygiene scoring."""

from datetime import datetime

import change_verdict
import hygiene
import service_check
import wifi_survey

IW = """BSS aa:bb:cc:dd:ee:01(on wlan0)
	freq: 5745
	signal: -71.00 dBm
	SSID: Neighbor-5G
BSS aa:bb:cc:dd:ee:02(on wlan0)
	freq: 5805
	signal: -60.00 dBm
	SSID: AREDN-20-v3
"""

HTML = """<table><tr><th>Signal</th><th>Chan</th><th>SSID</th><th>BSSID</th></tr>
<tr><td>-75</td><td>157 (5785)</td><td>Office</td><td>11:22:33:44:55:66</td></tr>
<tr><td>-80</td><td>36</td><td>Cafe</td><td>11:22:33:44:55:67</td></tr></table>"""


def test_parse_iw():
    aps = wifi_survey.parse_text(IW)
    assert [(a['bssid'], a['frequency'], a['signal'], a['ssid']) for a in aps] == [
        ('aa:bb:cc:dd:ee:01', 5745.0, -71.0, 'Neighbor-5G'), ('aa:bb:cc:dd:ee:02', 5805.0, -60.0, 'AREDN-20-v3')]


def test_parse_html_table():
    aps = wifi_survey.parse_text(HTML)
    assert [(a['ssid'], a['frequency'], a['signal']) for a in aps] == [('Office', 5785.0, -75.0), ('Cafe', 5180.0, -80.0)]


def test_ap_cost_only_overlapping():
    aps = [dict(a, low=a['frequency'] - 10, high=a['frequency'] + 10) for a in wifi_survey.parse_iw(IW)[:1]]
    cost, hits = wifi_survey.ap_cost(5735, 5755, aps, 1)
    assert cost > 0 and len(hits) == 1
    assert wifi_survey.ap_cost(5895, 5915, aps, 1) == (0.0, [])


def test_service_targets():
    t = service_check.target
    assert t({'link': 'http://KE9FCX-nsm2:8080/meshchat'}) == {
        'kind': 'http', 'host': 'ke9fcx-nsm2', 'port': 8080, 'url': 'http://ke9fcx-nsm2.local.mesh:8080/meshchat'}
    assert t({'link': 'sip://pbx:5060/x'})['kind'] == 'tcp'
    assert t({'link': 'http://X:80/a/redirect?http://example.com'})['kind'] == 'external'
    assert t({'link': ''})['kind'] == 'none'
    assert t({'link': 'ntp://N0X:123'})['kind'] == 'none'


def _link(peer, verdict, delta):
    return {'peer': peer, 'verdict': verdict, 'directions': [{'source': 'a', 'target': peer, 'metrics': [
        {'key': 'snr', 'label': 'SNR', 'delta': delta, 'verdict': verdict}]}]}


def test_verdict_summary():
    result = {'window_hours': 24, 'links': [_link('p1', 'worse', -6), _link('p2', 'better', 4), _link('p3', 'worse', -2)],
              'summary': {'better': 1, 'worse': 2, 'same': 0, 'n/a': 0}, 'reachability': {}}
    v, text, detail = change_verdict.summarize(result, {'category': 'channel', 'node_name': 'a', 'description': 'to 5865'})
    assert v == 'worse'
    assert 'worse to p1' in text and 'SNR -6' in text
    assert len(detail) == 3


def test_hygiene_score():
    n = {'name': 'k1aa-home', 'lat': 42.0, 'lon': -91.6, 'firmware_version': '4.26.7.0',
         'description': 'Shack node, k1aa@example.com', 'rf_status': 'on', 'rf_frequency': '5805',
         'antenna_azimuth': 90, 'antenna_desc': 'Litebeam', 'antenna_height': 10, 'is_active': 1,
         'last_seen': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    full = hygiene.score_node(n, (4, 26, 7, 0), datetime.now(), {})
    assert full['score'] == 100 and not full['fixes']
    bare = hygiene.score_node(dict(n, lat=None, lon=None, description='', antenna_azimuth=None),
                              (4, 26, 7, 0), datetime.now(), {})
    assert bare['score'] < 70 and len(bare['fixes']) >= 4
