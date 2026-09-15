import json
from planpilot.domain.importer import load_factory
from planpilot.domain.planning import build_candidates, validate_plan

def test_factory_dataset_produces_three_deterministic_profiles():
    factory=load_factory("examples/factory_demo.json")
    first=build_candidates(factory); second=build_candidates(factory)
    assert [x.profile for x in first]==["Balanced","Delivery First","Cost First"]
    assert [[(o["order_id"],o["start"],o["end"]) for o in x.operations] for x in first]==[[ (o["order_id"],o["start"],o["end"]) for o in x.operations] for x in second]

def test_validator_detects_machine_overlap():
    factory=load_factory("examples/factory_demo.json")
    violations=validate_plan([{"order_id":"X","operation_no":1,"machine_id":"CNC-01","worker_id":None,"start":0,"end":10,"material_id":None,"material_qty":0},{"order_id":"Y","operation_no":1,"machine_id":"CNC-01","worker_id":None,"start":5,"end":12,"material_id":None,"material_qty":0}],factory)
    assert any(v["type"]=="machine_conflict" for v in violations)
