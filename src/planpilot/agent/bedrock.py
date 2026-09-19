"""Compatibility home for bounded intent validation; provider I/O lives in inference/."""
from __future__ import annotations


class BedrockIntentClient:
    @staticmethod
    def validate_output(value):
        if not isinstance(value,dict) or set(value)-{'request','constraints'}:
            raise ValueError('model output contains unsupported intent fields')
        if not isinstance(value.get('request'),str) or not 0 < len(value['request'].strip()) <= 4000:
            raise ValueError('model intent request must contain 1 to 4000 characters')
        constraints=value.get('constraints',{})
        if not isinstance(constraints,dict) or set(constraints)-{'profile','event_id'}:
            raise ValueError('unsupported constraint fields')
        if 'profile' in constraints and constraints['profile'] not in ('Balanced','Delivery First','Cost First'):
            raise ValueError('unsupported profile')
        if 'event_id' in constraints and constraints['event_id'] not in ('EVT-001','EVT-002','EVT-003','EVT-004','EVT-005','EVT-006','EVT-007'):
            raise ValueError('unsupported event')
        return value
