import json

import change_log
import database
import scanner


def _link(rxcost):
    return {'raw_tracker': json.dumps({'rxcost': rxcost, 'txcost': rxcost})}


def test_tunnel_cost_change_detected():
    assert scanner.link_cost_change('Wireguard', _link(206), {'rxcost': 1000}) == (206, 1000)
    assert scanner.link_cost_change('DtD', _link(96), {'rxcost': 200}) == (96, 200)


def test_no_change_rf_infinity_or_missing():
    assert scanner.link_cost_change('Wireguard', _link(206), {'rxcost': 206}) is None
    assert scanner.link_cost_change('RF', _link(300), {'rxcost': 900}) is None
    assert scanner.link_cost_change('Wireguard', _link(206), {'rxcost': 65535}) is None
    assert scanner.link_cost_change('Wireguard', _link(65535), {'rxcost': 206}) is None
    assert scanner.link_cost_change('Wireguard', None, {'rxcost': 206}) is None
    assert scanner.link_cost_change('Wireguard', {'raw_tracker': None}, {'rxcost': 206}) is None
    assert scanner.link_cost_change('Wireguard', _link(206), {}) is None


def test_cost_change_event_lands_in_change_log(mesh):
    database.log_event(database.EVENT_LINK_COST_CHANGE, 'w0gq-mmc-ac2',
                       'Wireguard link to kd0cgn-prox Babel cost changed: rxcost 206 -> 1000')
    rows = change_log.list_changes()
    assert len(rows) == 1
    r = rows[0]
    assert r['category'] == 'routing' and r['peer_node'] == 'kd0cgn-prox' and r['source'] == 'auto'
    assert change_log.sync_from_events() == 0
