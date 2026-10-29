"""Bounded, task-owned OCI cache and clean ext4 images; no global image pruning."""
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from execution import ROOT,read,write,sha

DOCKER=['sudo','-n','env','DOCKER_HOST=unix:///run/codex-agenttraces-v67.sock','docker']


def docker(*args,**kw):
    return subprocess.run([*DOCKER,*args],check=True,text=True,**kw)


def verify_scope():
    path=docker('info','--format','{{.DockerRootDir}}',capture_output=True).stdout.strip()
    if Path(path)!=(ROOT/'image-cache/docker'):raise RuntimeError('refusing to mutate an unrelated Docker store')


def prepare(image):
    verify_scope()
    key=hashlib.sha256(image.encode()).hexdigest()[:16]
    out=ROOT/'image-preparation'/key;out.mkdir(parents=True,exist_ok=True)
    metadata=out/'prepared.json'
    if metadata.exists():
        record=read(metadata)
        if Path(record['golden']).exists():return record
        raise RuntimeError('image already completed and retired; do not overwrite its evidence')
    if shutil.disk_usage(ROOT).free<50*(1<<30):raise RuntimeError('insufficient disk reserve for another image')
    with (out/'pull.log').open('a') as log:
        docker('pull',image,stdout=log,stderr=subprocess.STDOUT,timeout=1800)
    info=json.loads(docker('image','inspect',image,capture_output=True).stdout)[0]
    write(out/'oci-inspect.json',info)
    size_gib=max(16,math.ceil(info['Size']/(1<<30)*1.35+6))
    golden=ROOT/'golden-cache'/(key+'.ext4');golden.parent.mkdir(parents=True,exist_ok=True)
    if golden.exists():raise RuntimeError('refusing to replace a partial or live golden '+str(golden))
    mount=Path(tempfile.mkdtemp(prefix='ac55-rootfs-'));container=None;mounted=False
    started=time.time();created=False
    try:
        with golden.open('xb') as f:f.truncate(size_gib*(1<<30))
        created=True
        subprocess.run(['mkfs.ext4','-q','-F',str(golden)],check=True)
        subprocess.run(['sudo','-n','mount','-o','loop',str(golden),str(mount)],check=True);mounted=True
        container=docker('create','--network','none','--label','codex.task=agenttraces-v67',image,'/bin/true',capture_output=True).stdout.strip()
        with (out/'export.log').open('w') as log:
            export=subprocess.Popen([*DOCKER,'export',container],stdout=subprocess.PIPE,stderr=log)
            unpack=subprocess.run(['sudo','-n','tar','--numeric-owner','--xattrs','--xattrs-include=*','-xpf','-','-C',str(mount)],stdin=export.stdout,stdout=log,stderr=log)
            export.stdout.close();rc=export.wait()
            if rc or unpack.returncode:raise RuntimeError('OCI export/extraction failed')
        if not (mount/'testbed').is_dir():raise RuntimeError('source image has no /testbed')
        # Inspect data only. Image programs are executed inside the Guest VM.
        head=mount/'testbed/.git/HEAD'
        head_value=head.read_text().strip() if head.is_file() else None
        subprocess.run(['sudo','-n','umount',str(mount)],check=True);mounted=False
        digest=sha(golden)
        record=dict(image=image,oci_id=info['Id'],repo_digests=info.get('RepoDigests',[]),
            golden=str(golden),golden_sha256=digest,filesystem_gib=size_gib,
            source_size_bytes=info['Size'],git_head_file=head_value,prepared_epoch=time.time(),
            preparation_wall_s=time.time()-started,
            note='Source tags and current content digests recorded; upstream image_info IDs refer to permission-fixed derived images, not necessarily source OCI digests')
        write(metadata,record);return record
    finally:
        if mounted:subprocess.run(['sudo','-n','umount',str(mount)],check=False)
        if container:docker('rm','-v',container,capture_output=True)
        mount.rmdir()
        if created and not metadata.exists():golden.unlink(missing_ok=True)


def retire(record):
    """Only after all VM consumers exited; retain hashes, OCI digests and logs."""
    verify_scope();golden=Path(record['golden'])
    if not golden.is_relative_to(ROOT/'golden-cache'):raise RuntimeError('not a task-owned golden')
    for cmd in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            if str(golden).encode() in cmd.read_bytes():raise RuntimeError('golden still has a live consumer')
        except (FileNotFoundError,PermissionError,ProcessLookupError):pass
    golden.unlink(missing_ok=True)
    # This daemon has a distinct data root and container namespace. Remove only
    # the exact image that this task pulled; never invoke global prune.
    docker('image','rm',record['image'],capture_output=True)


def release_failed(image):
    """Free only this task's failed image preparation, retaining its metadata."""
    verify_scope()
    found=subprocess.run([*DOCKER,'image','inspect',image],capture_output=True,text=True)
    if found.returncode==0:docker('image','rm',image,capture_output=True)


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('image');args=p.parse_args()
    print(json.dumps(prepare(args.image),ensure_ascii=False),flush=True)
