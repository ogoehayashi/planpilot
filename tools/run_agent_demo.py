"""Run the offline PlanPilot Agent demonstration."""
from planpilot.agent import AgentOrchestrator
from planpilot.agent.planning_tools import demo_registry
from planpilot.domain.importer import read_factory

result = AgentOrchestrator(demo_registry()).run({
    "request": "插入一个紧急订单，尽量不要加班，并说明交期影响",
    "factory_data": read_factory("examples/factory_demo.json"),
})
print("=== PlanPilot Agent ===")
print(result.response)
print("=== Tool trace ===")
for trace in result.traces:
    print(f"{trace.name}: {trace.status}")
print(f"approval_required: {result.approval_required}")
