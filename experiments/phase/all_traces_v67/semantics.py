"""Separate paired replay correctness from observable original-test fidelity."""
import collections
import json
from pathlib import Path
import re

from execution import read,write

COUNT=re.compile(r'\b(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b')


def summary(text):
    candidates=[line for line in text.splitlines() if COUNT.search(line)]
    if not candidates:return {}
    result=collections.Counter()
    for n,kind in COUNT.findall(candidates[-1]):result[kind.rstrip('s') if kind.startswith('error') else kind]+=int(n)
    return dict(result)


def original_summaries(path):
    if not path.exists():return {}
    results={}
    for line in path.read_text().splitlines():
        e=json.loads(line);content=e.get('message',{}).get('content',[])
        if not isinstance(content,list):continue
        for item in content:
            if not isinstance(item,dict) or item.get('type')!='tool_result':continue
            payload=item.get('content','')
            if isinstance(payload,list):payload='\n'.join(v.get('text','') for v in payload if isinstance(v,dict))
            if not isinstance(payload,str):continue
            parsed=summary(payload)
            if parsed:results[item.get('tool_use_id')]=parsed
    return results


def check(root,case,row,baseline=None,persist=True):
    row=dict(row);physical_valid=row['valid'];errors=[];differences=[]
    if row.get('complete'):
        data=read(root/row['name']/'guest-out/result.json')
        calls=read(Path(case['trace'])/'tool_calls.json')
        expected=original_summaries(Path(case['trace'])/'trace.jsonl')
        for step in data['steps']:
            if step.get('skipped') or step['tool']!='Bash':continue
            call=calls[step['source_index']];wanted=expected.get(call.get('id'))
            if not wanted:continue
            actual=summary(step.get('stdout_tail','')+'\n'+step.get('stderr_tail',''))
            if actual!=wanted:
                differences.append(dict(source_index=step['source_index'],expected=wanted,actual=actual))
                if wanted.get('passed',0)>0 and not actual.get('passed',0):
                    errors.append('original test suite did not reach any passing tests at call '+str(step['source_index']))
        if row.get('original_outcome_mismatches',0):errors.append('original observable tool outcome mismatch')
        if baseline is not None and json.dumps(row['signature'],sort_keys=True)!=json.dumps(baseline['signature'],sort_keys=True):
            errors.append('workload signature differs from fresh same-block DRAM')
        row['observable_test_differences']=differences
        row['original_observable_fidelity']='partial/no-test-summary' if not expected else ('different' if differences else 'matched')
    row['physical_audit_valid']=physical_valid
    row['reasons']=sorted(set([*row['reasons'],*errors]));row['valid']=not row['reasons']
    if errors and persist:write(root/(row['name']+'.physical.json'),{k:v for k,v in row.items() if k not in ['reasons','valid']})
    if persist:write(root/(row['name']+'.audit.json'),row)
    return row
