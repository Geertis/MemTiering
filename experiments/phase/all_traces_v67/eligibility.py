"""Original replay fidelity and explicitly labelled healthy-runtime improvements."""
import json
from pathlib import Path
import re

from execution import read,write
from semantics import check as strict_check,summary

def healthy_local_improvement(command,output):
    if re.search(r'\bpytest\b',command):
        counts=summary(output)
        return bool(counts.get('passed',0)>0 and not counts.get('failed',0) and not counts.get('error',0))
    if re.search(r'\bpip(?:3)?\s+install\b|\bpython(?:3)?\s+-m\s+pip\s+install\b',command):
        return not re.search(r'ERROR:|No matching distribution|Temporary failure in name resolution|Could not install',output)
    return False

def check(root,case,row,baseline=None,persist=True):
    row=strict_check(root,case,row,baseline=baseline,persist=False)
    row['strict_reasons']=list(row['reasons']);improved=[]
    if row.get('complete'):
        data=read(root/row['name']/'guest-out/result.json');calls=read(Path(case['trace'])/'tool_calls.json')
        for step in data['steps']:
            if step.get('skipped') or step['tool']!='Bash':continue
            command=calls[step['source_index']].get('input',{}).get('command','')
            output=step.get('stdout_tail','')+'\n'+step.get('stderr_tail','')
            if re.search(r'\bpytest\b|\bruntests\b',command) and re.search(r'No module named [\x27\x22]?pytest|pytest: command not found',output):
                row['reasons'].append('intended pytest did not execute at call '+str(step['source_index']))
        mismatches=[step for step in data['steps'] if not step.get('skipped') and step.get('outcome_match') is False]
        if set(row['reasons'])=={'original observable tool outcome mismatch'} and row.get('physical_audit_valid'):
            allowed=bool(mismatches) and len(mismatches)==row.get('original_outcome_mismatches')
            for step in mismatches:
                command=calls[step['source_index']].get('input',{}).get('command','')
                output=step.get('stdout_tail','')+'\n'+step.get('stderr_tail','')
                ok=step['tool']=='Bash' and step.get('status')==0 and step.get('expected_error') is True and healthy_local_improvement(command,output)
                if not ok:allowed=False
                improved.append(dict(source_index=step['source_index'],command=command,expected_error=step.get('expected_error'),actual_status=step.get('status'),allowed=ok,summary=summary(output)))
            if allowed:row['reasons']=[]
    row['reasons']=sorted(set(row['reasons']));row['valid']=not row['reasons']
    row['runtime_reconstruction_review']=dict(improvements=improved,rule='Only originally failing local pytest/package-install commands may become healthy successes; original success-to-failure, Read/Edit drift, missing test runners, physical failures and same-block signature drift remain rejected.')
    if row['valid']:
        row['comparison_scope']='network-restored-runtime' if case.get('network_repair') else 'rebuilt-runtime' if improved else 'paired-replay'
        if baseline and row['comparison_scope']!=baseline['comparison_scope']:
            row['valid']=False;row['reasons'].append('runtime comparison scope differs from same-block DRAM')
    if persist:write(root/(row['name']+'.audit.json'),row)
    return row
