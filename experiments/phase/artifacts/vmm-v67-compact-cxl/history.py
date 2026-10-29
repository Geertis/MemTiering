"""Bounded (process incarnation, virtual page) reuse observations.

PTE swapped->present is a refault observation, not proof of a disk read: resident
SwapCache can satisfy it. Host/Guest I/O counters are measured independently.
Only already observed transitions influence the lower placement policy.
"""
import os
from pathlib import Path
import struct
import time

PRESENT=1<<63
SWAPPED=1<<62
PFN_MASK=(1<<55)-1
PAGE=4096
CHUNK_PAGES=16
EMPTY_CHUNK=bytes(CHUNK_PAGES*8)


def transition(previous,current):
    if previous is None:return None
    if previous&PRESENT and current&SWAPPED:return 'evicted'
    if previous&SWAPPED and current&PRESENT:return 'refault'
    return None


def coalesce_pfns(pfns,limit=64):
    # A conservative 64KiB physical exclusion also protects neighbouring pages.
    blocks=sorted({(p//16)*16 for p in pfns if p})
    spans=[]
    for lo in blocks:
        hi=lo+16
        if spans and spans[-1][1]==lo:spans[-1]=(spans[-1][0],hi)
        else:spans.append((lo,hi))
    return [(lo*PAGE,hi*PAGE) for lo,hi in spans[:limit]]


def reuse_evidence(previous,now):
    """A first return is not evidence of a future return or an SSD disk read."""
    if previous is None or now-previous[0]>30:
        return (now,1),None
    interval=max(.001,now-previous[0]);count=min(16,previous[1]+1)
    protection=None
    if count>=2 and interval<=10:
        protection=(now+min(30.,max(1.,2*interval)),(count-1)/max(.5,interval))
    return (now,count),protection


class PageHistory:
    def __init__(self,group):
        self.group=Path(group)
        self.processes={}
        self.last_sample=-float('inf')
        self.counters=dict(evicted=0,refault=0,exited_swapped_pages=0,
                           recycled_present_pages=0,lifecycle_resets=0)
        self.latest=dict(protected_ranges=[],sampled_pages=0,processes=0,
                         counters=dict(self.counters),sample_cpu_s=0.)

    @staticmethod
    def identity(pid):
        proc=Path('/proc')/str(pid)
        fields=(proc/'stat').read_text().rsplit(')',1)[1].split()
        # starttime + startcode + startstack invalidate PID reuse and ordinary
        # exec transitions; same-address remaps have the caveat documented below.
        return (int(fields[19]),int(fields[23]),int(fields[25]),os.readlink(proc/'exe'))

    @staticmethod
    def maps(pid):
        result=[]
        for line in (Path('/proc')/str(pid)/'maps').read_text().splitlines():
            fields=line.split(maxsplit=5)
            name=fields[5] if len(fields)>5 else ''
            # Private anonymous heap/stack/mmap pages; shared mappings, device
            # mappings and file-backed VMAs are not assigned invented identities.
            if fields[1][0:2]!='rw' or fields[1][3]!='p' or (name and name not in ('[heap]','[stack]')):
                continue
            lo,hi=(int(x,16) for x in fields[0].split('-'))
            result.append((lo,hi))
        return result

    def sample(self,now=None):
        now=time.monotonic() if now is None else now
        if now-self.last_sample<.5:return self.latest
        cpu=time.process_time();started=time.monotonic()
        pids=set()
        for group in [self.group,*[p for p in self.group.rglob('*') if p.is_dir()]]:
            try:pids.update(int(p) for p in (group/'cgroup.procs').read_text().split())
            except (FileNotFoundError,ProcessLookupError):pass
        for pid in list(self.processes):
            if pid not in pids:
                old=self.processes.pop(pid)
                self.counters['exited_swapped_pages']+=sum(bool(e&SWAPPED)
                    for data,_ in old['chunks'].values() for (e,) in struct.iter_unpack('<Q',data))
        sampled=0;protected=[];errors=0
        for pid in sorted(pids)[:16]:
            fd=None
            try:
                identity=self.identity(pid)
                old=self.processes.get(pid)
                if old is None or old['identity']!=identity:
                    if old:self.counters['lifecycle_resets']+=1
                    old=dict(identity=identity,chunks={},cursor=0,protected={},returns={})
                    self.processes[pid]=old
                ranges=self.maps(pid)
                total=sum((hi-lo)//PAGE for lo,hi in ranges)
                if not total:continue
                offset=old['cursor']%total
                remaining=min(total,8192)  # <=32MiB per process per 500ms.
                original=remaining
                fd=os.open(f'/proc/{pid}/pagemap',os.O_RDONLY)
                for lo,hi in ranges+ranges:
                    length=(hi-lo)//PAGE
                    if offset>=length:offset-=length;continue
                    count=min(remaining,length-offset)
                    address=lo+offset*PAGE
                    data=os.pread(fd,count*8,address//PAGE*8)
                    for first in range(0,len(data)//8):
                        # Process one aligned 64KiB chunk at a time, keeping
                        # pagemap entries packed (no dict/tuple per 4KiB page).
                        if first and (address//PAGE+first)%CHUNK_PAGES:continue
                        va=address+first*PAGE
                        chunk=va//(PAGE*CHUNK_PAGES)
                        index=(va//PAGE)%CHUNK_PAGES
                        nr=min(CHUNK_PAGES-index,len(data)//8-first)
                        previous=old['chunks'].get(chunk)
                        prior=previous[0] if previous else EMPTY_CHUNK
                        updated=bytearray(prior)
                        updated[index*8:(index+nr)*8]=data[first*8:(first+nr)*8]
                        for j in range(nr):
                            page=va+j*PAGE
                            entry=struct.unpack_from('<Q',data,(first+j)*8)[0]
                            prev=struct.unpack_from('<Q',prior,(index+j)*8)[0]
                            kind=transition(prev if previous else None,entry)
                            if kind:
                                self.counters[kind]+=1
                                if kind=='refault':
                                    evidence,protection=reuse_evidence(old['returns'].get(page),now)
                                    old['returns'][page]=evidence
                                    if protection is not None:old['protected'][page]=protection
                            if prev&PRESENT and entry&PRESENT and (prev&PFN_MASK)!=(entry&PFN_MASK):
                                old['protected'].pop(page,None)
                                old['returns'].pop(page,None)
                                self.counters['recycled_present_pages']+=1
                            if not entry&(PRESENT|SWAPPED):
                                old['protected'].pop(page,None);old['returns'].pop(page,None)
                        if any(updated):old['chunks'][chunk]=(bytes(updated),now)
                        else:old['chunks'].pop(chunk,None)
                    sampled+=len(data)//8;remaining-=count;offset=0
                    if remaining<=0:break
                old['cursor']=(old['cursor']+original)%total
                # Revalidate current PFNs; a remembered physical address is not
                # a stable page identity after free, unmap, COW, or migration.
                for va,(until,score) in list(old['protected'].items()):
                    if until<=now:
                        del old['protected'][va];continue
                    if len(protected)>=4096:break
                    data=os.pread(fd,8,va//PAGE*8)
                    if len(data)==8:
                        entry=struct.unpack('<Q',data)[0]
                        if entry&PRESENT:protected.append((score,entry&PFN_MASK))
                if len(old['chunks'])>4096:
                    old['chunks']={va:e for va,e in old['chunks'].items() if now-e[1]<2}
                if len(old['returns'])>4096:
                    old['returns']=dict(sorted(old['returns'].items(),key=lambda item:item[1][0],reverse=True)[:4096])
            except (OSError,ValueError,IndexError):errors+=1
            finally:
                if fd is not None:os.close(fd)
        self.last_sample=now
        blocks={}
        for score,pfn in protected:
            if pfn:blocks[pfn//16*16]=max(score,blocks.get(pfn//16*16,0))
        priority=sorted(((score,lo*PAGE,(lo+16)*PAGE) for lo,score in blocks.items()),reverse=True)[:64]
        self.latest=dict(protected_ranges=coalesce_pfns([pfn for _,pfn in protected]),priority_blocks=priority,
            protected_sampled_pages=len(protected),sampled_pages=sampled,
            processes=len(pids),counters=dict(self.counters),raced_processes=errors,
            sample_cpu_s=time.process_time()-cpu,elapsed_s=time.monotonic()-started,
            packed_history_bytes=sum(len(data) for p in self.processes.values() for data,_ in p['chunks'].values()),
            interval_s=.5,max_pages_per_process=8192,
            identity_scope='private anonymous VMA pages; PID+start+exec-layout+virtual address',
            refault_is_disk_io=False)
        return self.latest
