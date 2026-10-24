"""全员统计的设计契约回归；不代表后台传输/汇总程序已实现。"""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

ROOT=Path(__file__).resolve().parents[1]/'docs/schemas'


def validator(name):
    schema=json.loads((ROOT/name).read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema,format_checker=FormatChecker())


def summary():
    return {'schema':'knowbase.team-summary.v1','definition_version':'daily-observed-v1',
            'team_id':'knowbase','member_id':'wenjian3','installation_id':'device-a',
            'db_epoch':'epoch-a','snapshot_id':'snapshot-a-1','source_timezone':'Asia/Shanghai',
            'period_start':'2026-10-22T16:00:00Z','data_watermark':'2026-10-23T09:00:00Z',
            'period_end':'2026-10-23T16:00:00Z','generated_at':'2026-10-23T09:00:00Z',
            'sequence':1,'coverage':'partial','metrics':{'calls':10,'known_searches':2,
            'hit_searches':1,'missing_searches':None,'updates':None,
            'commands':[{'command':'memory_search','host':'unknown','transport':'mcp',
                         'status':'ok','count':10}], 'usage':[]}}


def test_daily_summary_structure_accepts_unknown_fields_as_null():
    validator('team-summary.v1.schema.json').validate(summary())


@pytest.mark.parametrize('field,value',[('calls',True),('calls',-1),('updates',1.5)])
def test_counters_reject_boolean_negative_fraction(field,value):
    data=summary();data['metrics'][field]=value
    with pytest.raises(ValidationError):validator('team-summary.v1.schema.json').validate(data)


@pytest.mark.parametrize('location',['root','metrics','command'])
def test_unknown_private_field_is_rejected_at_all_levels(location):
    data=summary()
    target=data if location=='root' else data['metrics'] if location=='metrics' else data['metrics']['commands'][0]
    target['raw_query']='PRIVATE_SENTINEL'
    with pytest.raises(ValidationError):validator('team-summary.v1.schema.json').validate(data)


def test_account_period_cannot_masquerade_as_daily_request_usage():
    data=summary();data['metrics']['usage']=[{'host':'trae','source':'account_period',
        'model':'example','requests':None,'input_tokens':10,'output_tokens':10,
        'cache_read_tokens':None,'cache_creation_tokens':None}]
    with pytest.raises(ValidationError):validator('team-summary.v1.schema.json').validate(data)


def test_path_traversal_identity_is_rejected():
    data=summary();data['installation_id']='../../someone-else'
    with pytest.raises(ValidationError):validator('team-summary.v1.schema.json').validate(data)


def test_member_registry_can_grow_and_retire_without_fixed_number():
    v=validator('team-member.v1.schema.json')
    members=[{'schema':'knowbase.team-member.v1','team_id':'knowbase','member_id':f'member-{n}',
              'label':f'成员{n}','status':'active','joined_at':'2026-10-23T00:00:00Z',
              'retired_at':None,'sequence':1,'confirmation':'self_confirmed'} for n in range(12)]
    for m in members:v.validate(m)
    retired=copy.deepcopy(members[0]);retired.update(status='retired',retired_at='2026-10-23T08:00:00Z',sequence=2)
    v.validate(retired)
    retired['retired_at']=None
    with pytest.raises(ValidationError):v.validate(retired)
    active=copy.deepcopy(members[0]);active['confirmation']='pending'
    with pytest.raises(ValidationError):v.validate(active)
