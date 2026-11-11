"""Deleting a defunct node that only leftovers keep listed (database.delete_node)."""

import database


def test_delete_link_only_node_clears_leftovers(mesh):
    mesh.node('w0aa-mmc-ac2')
    mesh.link('w0aa-mmc-ac2', 'w0aa-mmc-gone', link_type='DtD', both=False, status='removed')
    mesh.infra('w0aa-mmc-gone', 'MMC')
    database.set_node_selected('w0aa-mmc-gone', True)
    gone = database.get_observed_node('w0aa-mmc-gone')
    assert gone and gone['is_link_only'] and gone['observed_status'] == 'removed' and gone['is_infrastructure']

    assert database.delete_node('w0aa-mmc-gone') == 0   # no nodes row, leftovers only
    assert database.get_observed_node('w0aa-mmc-gone') is None
    assert 'w0aa-mmc-gone' not in {n['name'] for n in database.get_all_observed_nodes()}
    assert 'w0aa-mmc-gone' not in database.get_infrastructure_nodes()
    assert not database.is_node_selected('w0aa-mmc-gone')
    assert database.get_observed_node('w0aa-mmc-ac2')   # its neighbour stays
