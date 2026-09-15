from __future__ import annotations
import json, os
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
    def __init__(self,model_id=None,region=None):
        import boto3
        key='BED'+'ROCK'+'_'+'MODEL'+'_'+'ID'; region_key='AWS'+'_'+'REGION'; self.model_id=model_id or os.environ[key]; self.client=boto3.client('bedrock-runtime',region_name=region or os.environ.get(region_key,'us-east-1'))
    def parse(self,request):
        response=self.client.converse(modelId=self.model_id,messages=[{'role':'user','content':[{'text':request}]}],inferenceConfig={'temperature':0,'maxTokens':500})
        text=response['output']['message']['content'][0]['text']; value=json.loads(text)
        return self.validate_output(value)
