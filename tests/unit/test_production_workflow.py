import pytest
from planpilot.persistence import Database
from planpilot.domain.importer import load_factory
from planpilot.domain.planning import build_candidates

def test_approval_requires_every_action_before_publish(tmp_path):
    db=Database(tmp_path/'state.db')
    from dataclasses import asdict
    from planpilot.domain.importer import read_factory
    raw=read_factory('examples/factory_demo.json')
    db.save_plan('PLAN-1', {'candidates': [asdict(p) for p in build_candidates(load_factory('examples/factory_demo.json'))], 'factory_data': raw, 'change_promised_due_date': True}, 1)
    requests=db.request_approval('PLAN-1','Balanced',['change_promised_due_date','publish_plan'])
    with pytest.raises(PermissionError): db.publish_plan('PLAN-1','Balanced','p','planner')
    db.decide_approval(requests[0]['request_id'],'m','manager','APPROVED')
    with pytest.raises(PermissionError): db.publish_plan('PLAN-1','Balanced','p','planner')
    db.decide_approval(requests[1]['request_id'],'p','planner','APPROVED')
    assert db.publish_plan('PLAN-1','Balanced','p','planner')['status']=='PUBLISHED'

def test_cp_sat_factory_data_is_deterministic():
    factory=load_factory('examples/factory_demo.json')
    a=build_candidates(factory); b=build_candidates(factory)
    assert [[(x['order_id'],x['start'],x['end']) for x in p.operations] for p in a]==[[ (x['order_id'],x['start'],x['end']) for x in p.operations] for p in b]

def test_batch_inventory_is_reserved_fifo():
    from planpilot.domain.importer import factory_from_dict
    from planpilot.domain.planning import validate_plan
    factory=factory_from_dict({'orders':[],'inventory':{'AL':{'batches':[{'batch_id':'old','quantity':5,'available_at':0},{'batch_id':'new','quantity':5,'available_at':10}]}}})
    ops=[{'order_id':'O','operation_no':1,'machine_id':'M','worker_id':None,'start':0,'end':1,'material_id':'AL','material_qty':8}]
    assert any(v['type']=='unknown_operation' for v in validate_plan(ops,factory))
