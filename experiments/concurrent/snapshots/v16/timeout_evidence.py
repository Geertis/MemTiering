"""Bounded reads from live Guests before forced cohort termination."""
import io
import json
from pathlib import Path
import subprocess
import tarfile

# Never export the full work tree at a timeout: that export can itself page heavily.
GUEST = r'''
import io,json,subprocess,sys,tarfile
from pathlib import Path
out=Path('/mnt/workload/out')
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as tar:
 def emit(name,data):
  info=tarfile.TarInfo(name);info.size=len(data);tar.addfile(info,io.BytesIO(data))
 def tail(path,limit):
  if not path.exists():return b''
  with path.open('rb') as f:
   offset=max(0,path.stat().st_size-limit);f.seek(offset);data=f.read(limit)
  if offset:data=data.split(b'\n',1)[-1]
  return b'\n'.join(line for line in data.splitlines() if line.strip() and valid(line))+b'\n'
 def valid(line):
  try:json.loads(line);return True
  except (ValueError,UnicodeDecodeError):return False
 emit('dmesg.log',subprocess.check_output(['dmesg']))
 p=Path('/tmp/controller.err')
 if p.exists():
  with p.open('rb') as f:f.seek(max(0,p.stat().st_size-65536));emit('controller.err',f.read(65536))
 emit('controller-tail.jsonl',tail(out/'controller.jsonl',131072))
 emit('phase-tail.jsonl',tail(out/'phase.jsonl',65536))
 # A completed result is the only full replay artifact required for semantic audit.
 p=out/'result.json'
 if p.exists() and p.stat().st_size <= 50*1024*1024:
  try:
   data=p.read_bytes();json.loads(data);emit('result.json',data)
  except (ValueError,UnicodeDecodeError):pass
 emit('evidence-scope.json',json.dumps(dict(controller='last 128KiB of complete JSON lines',
      phase='last 64KiB of complete JSON lines',result='complete result if available',
      raw_work_tree_exported=False)).encode())
'''


def capture(folder, lane, key):
    target = folder / f'slot-{lane}'
    archive = target / 'timeout-evidence.tar'
    command = "sudo python3 - <<'PHASETIEING_EVIDENCE'\n" + GUEST + '\nPHASETIEING_EVIDENCE\n'
    receipt = dict(lane=lane, captured=False)
    try:
        with archive.open('wb') as dst, (target/'timeout-evidence.err').open('wb') as err:
            proc = subprocess.run(['ssh', '-i', str(key), '-o', 'StrictHostKeyChecking=no',
                '-o', 'ConnectTimeout=5', f'clear@192.168.{92+lane}.100', command],
                stdout=dst, stderr=err, timeout=20, check=False)
        if proc.returncode:
            raise RuntimeError('Guest evidence ssh returncode ' + str(proc.returncode))
        out = target / 'guest-out'
        out.mkdir(exist_ok=True)
        with tarfile.open(archive) as tar:
            # Our producer emits only named regular files; no links or directories.
            for member in tar:
                if not member.isfile() or Path(member.name).name != member.name:
                    raise ValueError('unexpected timeout evidence entry')
                with (out/member.name).open('wb') as dst:
                    import shutil
                    shutil.copyfileobj(tar.extractfile(member), dst)
        (target/'dmesg.log').write_bytes((out/'dmesg.log').read_bytes())
        done = folder / 'barrier' / f'lane{lane}.done'
        if done.exists():
            (target/'ssh-status').write_bytes(done.read_bytes())
        receipt['captured'] = True
    except Exception as exc:
        receipt['error'] = repr(exc)
    (target/'timeout-evidence-receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
    return receipt
