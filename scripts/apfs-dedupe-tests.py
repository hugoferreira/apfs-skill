import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import hashlib
import uuid
import fcntl
import gzip
import struct
import zlib
import io
import contextlib
from unittest import mock

SCRIPT = Path(__file__).with_name('apfs-dedupe.py')
spec = importlib.util.spec_from_file_location('dedupe', SCRIPT)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
from apfs_common import XATTR_SHOWCOMPRESSION

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dedupe-test-')
        self.root = Path(self.tmp.name).resolve() / 'root'
        self.root.mkdir()
        self.state = self.root.parent / 'state'
        self.native = d.Native()
    def tearDown(self):
        self.tmp.cleanup()
    def run_cli(self, *args, code=0):
        r = subprocess.run([sys.executable, str(SCRIPT), str(self.root), '--state-dir', str(self.state), '--json', *args], capture_output=True, text=True)
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        return json.loads(r.stdout)
    def pair(self):
        a, b = self.root/'a', self.root/'b'
        a.write_bytes(b'example data\n'*4096)
        b.write_bytes(a.read_bytes())
        return a, b
    def test_unreadable_directory_is_skipped_and_reported(self):
        a,b=self.pair()
        locked=self.root/'locked';locked.mkdir();(locked/'c').write_bytes(a.read_bytes());locked.chmod(0)
        try:
            result=self.run_cli('--apply')
        finally:
            locked.chmod(0o700)
        self.assertEqual(result['cloned_files'],1)
        self.assertIn({'path':'locked','reason':'Permission denied'},result['skipped_files'])
    def test_cli_metadata_and_repeat(self):
        a,b=self.pair()
        b.chmod(0o751)
        os.utime(b, ns=(1_600_000_000_123456789,1_600_000_001_234567890))
        subprocess.run(['/usr/bin/xattr','-w','test.dedupe','destination',str(b)],check=True)
        subprocess.run(['/bin/chmod','+a',f'user:{os.getlogin()} allow read,readattr,readextattr,readsecurity',str(b)],check=True)
        before=self.native.metadata(b);old=b.stat().st_ino
        preview=self.run_cli()
        self.assertEqual(preview['planned_files'],1)
        self.assertFalse((self.root/'.apfs-dedupe.jsonl').exists())
        self.assertFalse((self.root/'.apfs-dedupe.jsonl.gz').exists())
        result=self.run_cli('--apply')
        self.assertEqual(result['cloned_files'],1)
        self.assertNotEqual(b.stat().st_ino,old)
        self.assertNotEqual(a.stat().st_ino,b.stat().st_ino)
        self.assertEqual(self.native.metadata(b),before)
        self.assertEqual(self.run_cli('--apply')['cloned_files'],0)
        self.assertEqual(self.run_cli('--apply')['already_completed_files'],1)
        self.assertEqual(self.run_cli()['planned_files'],0)
        b.write_bytes(b'changed')
        self.assertNotEqual(a.read_bytes(),b.read_bytes())
    def test_no_acl_and_exclusions(self):
        a,b=self.pair()
        (self.root/'.git').mkdir();(self.root/'.git'/'object').write_bytes(a.read_bytes())
        (self.root/'link').symlink_to(a)
        os.link(a,self.root/'hardlink')
        (self.root/'empty').touch()
        (self.root/'fifo').touch();(self.root/'fifo').unlink();os.mkfifo(self.root/'fifo')
        self.assertEqual(self.run_cli()['planned_files'],0)
        (self.root/'hardlink').unlink()
        self.assertEqual(self.run_cli('--apply')['cloned_files'],1)
    def test_usage_and_empty(self):
        self.assertEqual(self.run_cli()['planned_files'],0)
        r=subprocess.run([sys.executable,str(SCRIPT),str(self.root),'--min-size','0'],capture_output=True,text=True)
        self.assertEqual(r.returncode,2)
        r=subprocess.run([sys.executable,str(SCRIPT),str(self.root/'missing'),'--json'],capture_output=True,text=True)
        self.assertEqual(r.returncode,1);self.assertFalse(json.loads(r.stdout)['ok'])
        r=subprocess.run([sys.executable,str(SCRIPT)],capture_output=True,text=True)
        self.assertEqual(r.returncode,0);self.assertIn('usage:',r.stdout)
    def stage(self, prepared=False, replace=False):
        a,b=self.pair();ident=uuid.uuid4().hex;t=self.root/(d.PREFIX+ident)
        j=d.Journal(self.root/'.apfs-dedupe.jsonl',self.root)
        digest=hashlib.sha256(a.read_bytes()).hexdigest()
        j.add(event='stage',id=ident,source='a',destination='b',temporary=t.name,sha256=digest)
        self.native.clone(a,t)
        if prepared:j.add(event='prepared',id=ident,signature=d.signature(t.stat()))
        if replace:os.replace(t,b)
        os.close(j.fd)
        return a,b,t
    def test_recover_unprepared(self):
        a,b,t=self.stage()
        self.assertEqual(self.run_cli('--apply')['cloned_files'],1)
        self.assertFalse(t.exists())
    def test_recover_prepared(self):
        a,b,t=self.stage(prepared=True)
        self.assertEqual(self.run_cli('--apply')['cloned_files'],1)
        self.assertFalse(t.exists())
    def test_recover_replaced_and_torn_journal(self):
        a,b,t=self.stage(prepared=True,replace=True);old=b.stat().st_ino
        with open(self.root/'.apfs-dedupe.jsonl','ab') as f:f.write(b'{"torn":')
        self.assertEqual(self.run_cli('--apply')['cloned_files'],0)
        self.assertEqual(b.stat().st_ino,old)
        self.assertFalse((self.root/'.apfs-dedupe.jsonl').exists())
        for line in gzip.decompress((self.root/'.apfs-dedupe.jsonl.gz').read_bytes()).splitlines():json.loads(line)
    def test_gzip_default_and_complete_members(self):
        self.pair();result=self.run_cli('--apply')
        path=self.root/'.apfs-dedupe.jsonl.gz'
        self.assertEqual(result['journal'],str(path))
        data=path.read_bytes()
        lines=gzip.decompress(data).splitlines()
        self.assertEqual([json.loads(x)['event'] for x in lines],['header','stage','prepared','done'])
        self.assertLess(len(data),len(gzip.decompress(data)))
        subprocess.run(['/usr/bin/gzip','-t',str(path)],check=True)
    def test_torn_gzip_tail_recovery(self):
        self.pair();self.run_cli('--apply');path=self.root/'.apfs-dedupe.jsonl.gz'
        good=path.read_bytes()
        extra=gzip.compress(b'{"event":"aborted","id":"unused"}\n')
        # Cover partial header, deflate body, missing CRC and trailer.
        for cut in (1,9,len(extra)//2,len(extra)-8,len(extra)-1):
            path.write_bytes(good+extra[:cut])
            before=path.read_bytes()
            self.assertEqual(self.run_cli()['planned_files'],0)
            self.assertEqual(path.read_bytes(),before,'dry-run must not truncate')
            self.assertEqual(self.run_cli('--apply')['cloned_files'],0)
            self.assertEqual(path.read_bytes(),good)
    def test_corrupt_gzip_crc_retained(self):
        self.pair();self.run_cli('--apply');path=self.root/'.apfs-dedupe.jsonl.gz'
        data=bytearray(path.read_bytes());data[-8]^=1;path.write_bytes(data)
        result=self.run_cli('--apply',code=1)
        self.assertIn('Corrupt gzip journal',result['message'])
        self.assertEqual(path.read_bytes(),data)
    def test_migration_preserves_completed_state(self):
        a,b=self.pair();ident=uuid.uuid4().hex;plain=self.root/'.apfs-dedupe.jsonl'
        j=d.Journal(plain,self.root);_,sa=d.hash_file(a);digest,sb=d.hash_file(b)
        d.clone_one(self.native,self.root,a,b,digest,sa,sb,j);os.close(j.fd)
        old=plain.read_bytes();before=b.stat().st_ino
        self.assertEqual(self.run_cli()['planned_files'],0)
        self.assertEqual(plain.read_bytes(),old)
        result=self.run_cli('--apply')
        self.assertEqual(result['cloned_files'],0)
        self.assertEqual(b.stat().st_ino,before)
        self.assertFalse(plain.exists())
        self.assertEqual(gzip.decompress((self.root/'.apfs-dedupe.jsonl.gz').read_bytes()),old)
    def test_migration_resumes_after_publish(self):
        self.pair();plain=self.root/'.apfs-dedupe.jsonl';gz=self.root/'.apfs-dedupe.jsonl.gz'
        j=d.Journal(plain,self.root);os.close(j.fd)
        gz.write_bytes(gzip.compress(plain.read_bytes()))
        self.assertEqual(self.run_cli('--apply')['cloned_files'],1)
        self.assertFalse(plain.exists())
    def test_migration_rejects_conflicting_versions(self):
        self.pair();plain=self.root/'.apfs-dedupe.jsonl';gz=self.root/'.apfs-dedupe.jsonl.gz'
        j=d.Journal(plain,self.root);j.add(event='aborted',id='extra');os.close(j.fd)
        first=plain.read_bytes().splitlines(keepends=True)[0];gz.write_bytes(gzip.compress(first))
        before=(plain.read_bytes(),gz.read_bytes())
        self.assertIn('differ',self.run_cli('--apply',code=1)['message'])
        self.assertEqual((plain.read_bytes(),gz.read_bytes()),before)
    def test_custom_journal_adds_gzip_suffix(self):
        self.pair();path=self.root/'custom.jsonl'
        result=self.run_cli('--apply','--journal',str(path))
        self.assertEqual(result['journal'],str(path)+'.gz')
        self.assertFalse(path.exists())
        self.assertTrue(Path(str(path)+'.gz').exists())
        self.assertEqual(self.run_cli('--apply','--journal',str(path))['cloned_files'],0)
    def test_empty_legacy_journal_migration(self):
        self.pair();plain=self.root/'.apfs-dedupe.jsonl';plain.touch()
        self.assertEqual(self.run_cli('--apply')['cloned_files'],1)
        self.assertFalse(plain.exists())
        subprocess.run(['/usr/bin/gzip','-t',str(plain)+'.gz'],check=True)
    def test_large_bulk_and_concatenated_members(self):
        plain=self.root/'.apfs-dedupe.jsonl'
        header={'event':'header','version':1,'root':str(self.root)}
        records=[header]+[{'event':'aborted','id':str(uuid.uuid4()),'description':'long duplicate path/'*20} for _ in range(300)]
        data=b''.join((json.dumps(r,sort_keys=True)+'\n').encode() for r in records)
        plain.write_bytes(data)
        self.assertEqual(self.run_cli('--apply')['cloned_files'],0)
        gz=Path(str(plain)+'.gz');self.assertEqual(gzip.decompress(gz.read_bytes()),data)
        self.assertLess(gz.stat().st_size,len(data)//4)
        j=d.Journal(gz,self.root)
        try:
            for _ in range(300):j.add(event='aborted',id=str(uuid.uuid4()))
        finally:os.close(j.fd)
        self.assertEqual(self.run_cli('--apply')['cloned_files'],0)
        self.assertEqual(len(gzip.decompress(gz.read_bytes()).splitlines()),601)
    def test_concurrent_change_refused(self):
        a,b=self.pair();_,sa=d.hash_file(a);digest,sb=d.hash_file(b)
        b.write_bytes(b'updated')
        j=d.Journal(self.root/'.apfs-dedupe.jsonl',self.root)
        try:
            with self.assertRaises(d.Conflict):d.clone_one(self.native,self.root,a,b,digest,sa,sb,j)
            self.assertEqual(b.read_bytes(),b'updated')
        finally:os.close(j.fd)
    def holder(self, path, code="f=open(p,'r+b')"):
        # Another process that holds path, then appends b'late' on request.
        script=f"import sys;p={str(path)!r};{code};print('up',flush=True);sys.stdin.readline();f.seek(0,2);f.write(b'late');f.close();print('done',flush=True);sys.stdin.readline()"
        child=subprocess.Popen([sys.executable,'-c',script],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        self.addCleanup(lambda:(child.kill(),child.wait(),child.stdin.close(),child.stdout.close()))
        self.assertEqual(child.stdout.readline(),'up\n')
        return child
    def staged(self, journal='.apfs-dedupe.jsonl'):
        a,b=self.pair();_,sa=d.hash_file(a);digest,sb=d.hash_file(b)
        j=d.Journal(self.root/journal,self.root);self.addCleanup(lambda:os.close(j.fd))
        return a,b,j,d.prepare(self.native,self.root,a,b,digest,sa,sb,j)
    def events(self, journal='.apfs-dedupe.jsonl'):
        data=(self.root/journal).read_bytes()
        return [json.loads(x)['event'] for x in (gzip.decompress(data) if journal.endswith('.gz') else data).splitlines()]
    def assert_original_kept(self, b, item, outcome, reason):
        self.assertIsInstance(outcome,d.Skip)
        self.assertEqual(outcome.reason,reason)
        self.assertEqual(b.stat().st_ino,item.expected[1])
        self.assertFalse(list(self.root.glob(d.PREFIX+'*')))
        self.assertEqual(self.events()[-1],'aborted')
    def test_open_file_is_skipped_then_retried(self):
        a,b=self.pair();old=b.stat().st_ino;child=self.holder(b)
        result=self.run_cli('--apply')
        self.assertEqual((result['cloned_files'],result['skipped_files']),(0,[{'path':'b','reason':'Open in another process'}]))
        self.assertEqual(b.stat().st_ino,old)
        child.stdin.write('\n');child.stdin.flush();self.assertEqual(child.stdout.readline(),'done\n')
        self.assertTrue(b.read_bytes().endswith(b'late'))
        b.write_bytes(a.read_bytes())
        self.assertEqual(self.run_cli('--apply')['cloned_files'],1)
    def test_mapped_file_is_skipped(self):
        a,b=self.pair();_,sa=d.hash_file(a);digest,sb=d.hash_file(b)
        code="import ctypes as C,os;l=C.CDLL(None);l.mmap.restype=C.c_void_p;l.mmap.argtypes=[C.c_void_p,C.c_size_t,C.c_int,C.c_int,C.c_int,C.c_longlong];fd=os.open(p,os.O_RDWR);l.mmap(None,4096,3,1,fd,0);os.close(fd);f=open(os.devnull,'wb')"
        self.holder(b,code)
        self.assertIn((b.stat().st_dev,b.stat().st_ino),self.native.open_files())
        self.assertEqual(self.native.generation(b),0)
        self.assertNotIn(self.native.generation(a),(0,None))
        result=self.run_cli('--apply')
        self.assertEqual((result['cloned_files'],[x['path'] for x in result['skipped_files']]),(0,['b']))
    def test_open_after_staging_is_skipped(self):
        a,b,j,item=self.staged()
        self.holder(b)
        (_,outcome),=d.publish(self.native,j,[item])[0]
        self.assert_original_kept(b,item,outcome,'Open in another process')
    def test_open_during_replacement_restores_original(self):
        a,b,j,item=self.staged();real=d.Native.swap;children=[]
        def swap(native,x,y):
            # Opened after the first snapshot, immediately before the exchange.
            if Path(y).stat().st_ino == item.expected[1]:
                children.append(self.holder(y))
            real(native,x,y)
        with mock.patch.object(d.Native,'swap',swap):
            (_,outcome),=d.publish(self.native,j,[item])[0]
        child,=children
        self.assert_original_kept(b,item,outcome,'Opened by another process during replacement')
        child.stdin.write('\n');child.stdin.flush();self.assertEqual(child.stdout.readline(),'done\n')
        self.assertEqual(b.read_bytes(),a.read_bytes()+b'late')
    def test_write_during_replacement_restores_original(self):
        a,b,j,item=self.staged();real=d.Native.swap
        def swap(native,x,y):
            publishing = Path(y).stat().st_ino == item.expected[1]
            real(native,x,y)
            if publishing:
                with open(x,'ab') as f:f.write(b'late')
        with mock.patch.object(d.Native,'swap',swap):
            (_,outcome),=d.publish(self.native,j,[item])[0]
        self.assert_original_kept(b,item,outcome,'Changed during replacement')
        self.assertEqual(b.read_bytes(),a.read_bytes()+b'late')
    def test_mapped_write_before_replacement_is_detected(self):
        import mmap
        a,b,j,item=self.staged();mtime=b.stat().st_mtime_ns
        with open(b,'r+b') as f:
            m=mmap.mmap(f.fileno(),0);m[:4]=b'late';m.close()
        os.utime(b,ns=(mtime,mtime))
        (_,outcome),=d.publish(self.native,j,[item])[0]
        self.assert_original_kept(b,item,outcome,'Changed during verification')
        self.assertEqual(b.read_bytes()[:4],b'late')
    def test_path_write_while_original_in_use_keeps_both(self):
        a,b,j,item=self.staged();real=d.Native.swap
        def swap(native,x,y):
            real(native,x,y)
            with open(x,'ab') as f:f.write(b'held')
            with open(y,'ab') as f:f.write(b'path')
        with mock.patch.object(d.Native,'swap',swap),self.assertRaises(d.Conflict):
            d.publish(self.native,j,[item])
        self.assertEqual((b.read_bytes(),item.tmp.read_bytes()),(a.read_bytes()+b'path',a.read_bytes()+b'held'))
        self.assertEqual(self.events()[-1],'conflict')
        self.doCleanups()
        self.assertIn('both are kept',self.run_cli('--apply','--journal',str(self.root/'.apfs-dedupe.jsonl'),code=1)['message'])
        self.assertTrue(item.tmp.exists())
    def test_flock_holder_is_skipped(self):
        a,b,j,item=self.staged();fd=os.open(b,os.O_RDONLY)
        try:
            fcntl.flock(fd,fcntl.LOCK_SH)
            (_,outcome),=d.publish(self.native,j,[item])[0]
        finally:os.close(fd)
        self.assert_original_kept(b,item,outcome,'Locked by another process')
    def exchanged(self):
        a,b,j,item=self.staged('.apfs-dedupe.jsonl.gz')
        self.native.swap(item.tmp,b);os.close(item.fd)
        self.doCleanups()
        return a,b,item
    def test_recover_exchanged_restores_original(self):
        a,b,item=self.exchanged()
        self.assertEqual(self.run_cli('--apply')['cloned_files'],1)
        self.assertEqual(self.events('.apfs-dedupe.jsonl.gz'),['header','stage','prepared','aborted','stage','prepared','done'])
        self.assertFalse(list(self.root.glob(d.PREFIX+'*')))
        self.assertEqual(a.read_bytes(),b.read_bytes())
    def test_recover_exchanged_keeps_newer_path_data(self):
        a,b,item=self.exchanged();b.write_bytes(b'newer')
        self.assertEqual(self.run_cli('--apply')['cloned_files'],0)
        self.assertEqual(b.read_bytes(),b'newer')
        self.assertFalse(list(self.root.glob(d.PREFIX+'*')))
    def test_recover_exchanged_keeps_both_changed_versions(self):
        a,b,item=self.exchanged();b.write_bytes(b'newer');item.tmp.write_bytes(b'held')
        self.assertIn('both are kept',self.run_cli('--apply',code=1)['message'])
        self.assertIn('both are kept',self.run_cli('--apply',code=1)['message'])
        self.assertEqual(self.events('.apfs-dedupe.jsonl.gz')[-1],'conflict')
        self.assertEqual((b.read_bytes(),item.tmp.read_bytes()),(b'newer',b'held'))
        item.tmp.unlink()
        self.assertEqual(self.run_cli('--apply')['cloned_files'],0)
        self.assertEqual(b.read_bytes(),b'newer')

    def test_recovery_preserves_xattr_edits_on_both_inodes(self):
        a, b, item = self.exchanged()
        key = b'org.example.concurrent-edit'
        self.native.set_xattr(item.tmp, key, b'original edit')
        self.native.set_xattr(b, key, b'replacement edit')
        result = self.run_cli('--apply', code=1)
        self.assertIn('both are kept', result['message'])
        self.assertEqual(self.native.xattr(item.tmp, key), b'original edit')
        self.assertEqual(self.native.xattr(b, key), b'replacement edit')

    def test_rollback_keeps_xattr_written_to_replacement_during_exchange(self):
        a, b, journal, item = self.staged()
        real_swap = d.Native.swap
        key = b'org.example.concurrent-edit'

        def swap(native, work, target):
            if Path(target).stat().st_ino == item.expected[1]:
                native.set_xattr(target, key, b'original edit')
            else:
                native.set_xattr(target, key, b'replacement edit during rollback')
            real_swap(native, work, target)

        with mock.patch.object(d.Native, 'swap', swap), self.assertRaises(d.Conflict):
            d.publish(self.native, journal, [item])
        self.assertEqual(self.native.xattr(b, key), b'original edit')
        self.assertEqual(self.native.xattr(item.tmp, key), b'replacement edit during rollback')

    def test_stage_clears_inherited_access_before_clone(self):
        self.root.chmod(0o777)
        subprocess.run(['/bin/chmod', '+a', 'everyone allow list,search,directory_inherit',
                        str(self.root)], check=True)
        real_clone = d.Native.clone

        def clone(native, source, payload):
            self.assertEqual(payload.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(native.acl(payload.parent), b'!#acl 1\n')
            real_clone(native, source, payload)

        with mock.patch.object(d.Native, 'clone', clone):
            _, _, journal, item = self.staged()
            self.assertEqual(item.tmp.parent.stat().st_mode & 0o777, 0o700)
            d.publish(self.native, journal, [item])
        self.assertFalse(item.tmp.parent.exists())
    def test_changing_and_vanishing_files_are_skipped(self):
        a,b=self.pair();c=self.root/'c';c.write_bytes(a.read_bytes());e=self.root/'e';e.write_bytes(a.read_bytes())
        real=d.hash_fd
        def hash_fd(fd,on_bytes=None):
            digest=real(fd,on_bytes);ino=os.fstat(fd).st_ino
            if c.exists() and ino==c.stat().st_ino:
                with open(c,'ab') as f:f.write(b'x')
            if e.exists() and ino==e.stat().st_ino:e.unlink()
            return digest
        output=io.StringIO()
        with mock.patch.object(d,'hash_fd',hash_fd),contextlib.redirect_stdout(output):
            self.assertEqual(d.main([str(self.root),'--state-dir',str(self.state),'--apply','--json','--quiet']),0)
        result=json.loads(output.getvalue())
        self.assertEqual(result['cloned_files'],1)
        self.assertEqual(result['skipped_files'],[{'path':'c','reason':'File changed while hashing'},
                                                  {'path':'e','reason':'File changed while hashing'}])
        self.assertEqual(c.read_bytes(),a.read_bytes()+b'x')
    def test_lock_conflict(self):
        self.pair();fd=os.open(self.root,os.O_RDONLY)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertFalse(self.run_cli('--apply',code=1)['ok'])
        finally:os.close(fd)
    def test_progress_reports_counts_and_bytes(self):
        self.pair()
        result=subprocess.run([sys.executable,str(SCRIPT),str(self.root),'--state-dir',str(self.state),'--apply','--json'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])
        self.assertIn('Hashing: 2/2 files;',result.stderr)
        self.assertIn('(100.0% of bytes)',result.stderr)
        self.assertIn('Processing clone candidates: 1/1 files;',result.stderr)
        self.assertNotIn('Hashing files',result.stderr)
    def test_generated_provenance_preserves_original_and_resumes(self):
        a,b=self.pair();before=d.signature(b.stat())
        original=d.Native.metadata
        def metadata(native,path):
            value=original(native,path)
            if path.name == d.STAGE_PAYLOAD:
                attrs=dict(value[6]);attrs[b'com.apple.provenance']=b'generated'
                value=(*value[:6],attrs,value[7])
            return value
        output=io.StringIO()
        with mock.patch.object(d.Native,'metadata',metadata),contextlib.redirect_stdout(output):
            result=d.main([str(self.root),'--state-dir',str(self.state),'--apply','--json','--quiet'])
        self.assertEqual(result,0)
        result=json.loads(output.getvalue())
        self.assertEqual(result['cloned_files'],0)
        self.assertEqual(result['provenance_unmatched_files'],['b'])
        self.assertEqual(d.signature(b.stat()),before)
        self.assertFalse(list(self.root.glob(d.PREFIX+'*')))
        result=self.run_cli('--apply')
        self.assertEqual(result['cloned_files'],0)
        self.assertEqual(result['provenance_unmatched_files'],['b'])
        self.assertEqual(self.run_cli()['planned_files'],0)
    def test_incompatible_provenance_excluded_from_plan(self):
        a,b=self.pair();(self.root/'c').write_bytes(a.read_bytes())
        output=io.StringIO()
        def provenance(native, path, name, options):
            self.assertEqual((name, options), (d.PROVENANCE_XATTR, d.XATTR_NOFOLLOW))
            return b'original' if path.name=='a' else b'other'
        with mock.patch.object(d.Native,'xattr',provenance),contextlib.redirect_stdout(output):
            result=d.main([str(self.root),'--state-dir',str(self.state),'--json','--quiet'])
        self.assertEqual(result,0)
        result=json.loads(output.getvalue())
        self.assertEqual(result['provenance_unmatched_files'],['a'])
        self.assertEqual([(r['source'],r['destination']) for r in result['changes']],[('b','c')])
        self.assertEqual(a.read_bytes(),b.read_bytes())
    def top_fixture(self):
        # Reclaimable bytes: big 2x8000=16000, many 5x3000=15000, small 1x100.
        for name, size, copies in (('big', 8000, 3), ('many', 3000, 6), ('small', 100, 2)):
            for i in range(copies):
                (self.root/f'{name}{i}').write_bytes(name[0].encode()*size)
    def cli_text(self, *args, code=0):
        r=subprocess.run([sys.executable,str(SCRIPT),str(self.root),'--state-dir',str(self.state),*args],capture_output=True,text=True)
        self.assertEqual(r.returncode,code,r.stdout+r.stderr)
        return r
    def test_top_json_ranks_by_reclaimable_bytes(self):
        self.top_fixture()
        self.assertNotIn('top_groups',self.run_cli())
        groups=self.run_cli('--top','2')['top_groups']
        self.assertEqual([(g['rank'],g['size_bytes'],g['planned_files'],g['logical_duplicate_bytes'],g['cloned_files']) for g in groups],
                         [(1,8000,2,16000,0),(2,3000,5,15000,0)])
        self.assertEqual(groups[0]['source'],'big0')
        self.assertEqual({g['kind'] for g in groups},{'file'})
        self.assertEqual(groups[0]['destinations'],['big1','big2'])
        self.assertEqual(groups[0]['sha256'],hashlib.sha256(b'b'*8000).hexdigest())
        self.assertEqual(len(self.run_cli('--top','99')['top_groups']),3)
        preview=self.run_cli('--top','2')
        self.assertEqual((preview['planned_files'],preview['logical_duplicate_bytes']),(7,31000))
        self.assertEqual((preview['deferred_files'],preview['deferred_groups'],preview['deferred_logical_bytes']),(1,1,100))
        self.assertEqual({c['source'] for c in preview['changes']},{'big0','many0'})
        inodes={p.name:p.stat().st_ino for p in self.root.iterdir()}
        applied=self.run_cli('--apply','--top','1')
        self.assertEqual((applied['cloned_files'],applied['planned_files'],applied['deferred_files']),(2,2,6))
        self.assertEqual((applied['top_groups'][0]['planned_files'],applied['top_groups'][0]['cloned_files']),(2,2))
        changed={p.name for p in self.root.iterdir() if p.name in inodes and p.stat().st_ino!=inodes[p.name]}
        self.assertEqual(changed,{'big1','big2'})
        rerun=self.run_cli('--top','5')
        self.assertEqual([g['source'] for g in rerun['top_groups']],['many0','small0'])
        self.assertEqual((rerun['already_completed_files'],rerun['deferred_files']),(2,0))
        self.assertNotIn('deferred_files',self.run_cli())
    def test_top_human_output(self):
        self.top_fixture()
        r=self.cli_text('--top','2')
        lines=r.stdout.splitlines()
        start=lines.index('Top 2 duplicate groups by reclaimable size:')
        self.assertTrue(lines[0].startswith('Would clone 7 files'))
        self.assertIn('Outside --top 2: 1 files in 1 groups (100 logical bytes) left unchanged.',lines)
        self.assertRegex(lines[start+1],r'^   1\. 15\.6 KiB reclaimable \(~[0-9.]+ KiB physical\): 2 x 7\.8 KiB  sha256 [0-9a-f]{12}$')
        self.assertRegex(lines[start+2],r'; 2 more clones in --json$')
        self.assertEqual(lines[start+3:],['','   big0   #1 keep','   big1   #1 clone','   big2   #1 clone',
                                          '   many0  #2 keep','   many1  #2 clone','   many2  #2 clone','   many3  #2 clone'])
        self.assertNotIn('small',r.stdout)
        self.assertNotIn('Top ',r.stderr)
        quiet=self.cli_text('--top','1','--quiet')
        self.assertEqual(quiet.stdout.splitlines()[0],'Top 1 duplicate groups by reclaimable size:')
        self.assertEqual(quiet.stderr,'')
        applied=self.cli_text('--apply','--top','1','--quiet')
        self.assertIn('; cloned 2/2',applied.stdout)
        self.assertIn('   many0  #1 keep',self.cli_text('--top','1','--quiet').stdout)
        self.cli_text('--apply','--top','2','--quiet')
        self.assertEqual(self.cli_text('--top','1','--quiet').stdout,'No duplicate groups to rank.\n')
    def test_top_path_tree_shares_prefixes(self):
        base=self.root/'darwin-arm64'/'opt'/'gcc'
        for version in ('14.1.0','14.2.0'):
            (base/version).mkdir(parents=True)
            (base/version/'cc1').write_bytes(b'1'*9000)
            (base/version/'cc1plus').write_bytes(b'2'*8000)
        (self.root/'tools').mkdir()
        for name in ('lto','lto.bak'):(self.root/'tools'/name).write_bytes(b'3'*100)
        lines=self.cli_text('--top','3','--quiet').stdout.splitlines()
        self.assertEqual(lines[0],'Top 2 duplicate groups by reclaimable size:')
        self.assertTrue(lines[1].endswith(': 2 identical directories, 2 files (16.6 KiB) each; 2 files to clone'),lines[1])
        self.assertEqual(lines[3:],['',
            '   darwin-arm64/opt/gcc/',
            '     14.1.0/  #1 keep',
            '     14.2.0/  #1 clone',
            '   tools/',
            '     lto      #2 keep',
            '     lto.bak  #2 clone'])
    def tree(self,spec):
        for rel,content in spec.items():
            path=self.root/rel;path.parent.mkdir(parents=True,exist_ok=True)
            if content is None:path.mkdir()
            elif isinstance(content,str):path.symlink_to(content)
            else:path.write_bytes(content)
    def test_top_reports_outermost_identical_directories(self):
        same={'p/x':b'x'*5000,'p/sub/y':b'y'*3000,'p/empty':b'','p/link':'x'}
        self.tree({f'{side}/{k}':v for side in ('a','b') for k,v in same.items()})
        units=self.run_cli('--top','5')['top_groups']
        self.assertEqual(len(units),1)
        self.assertEqual(units[0]['kind'],'directory')
        self.assertEqual(units[0]['directories'],[{'path':'a','role':'keep'},{'path':'b','role':'clone'}])
        self.assertEqual((units[0]['files_per_directory'],units[0]['bytes_per_directory']),(2,8000))
        self.assertEqual(sorted(units[0]['destinations']),['b/p/sub/y','b/p/x'])
        lines=self.cli_text('--top','5','--quiet').stdout.splitlines()
        self.assertEqual(lines[2:],['','   a/  #1 keep','   b/  #1 clone'])
    def test_top_directory_requires_complete_equality(self):
        self.tree({'a/x':b'x'*5000,'a/y':b'y'*3000,'b/x':b'x'*5000,'b/y':b'y'*3000,'b/extra':b'unique',
                   'c/x':b'x'*5000,'c/y':b'y'*3000,'c/l':'one','d/x':b'x'*5000,'d/y':b'y'*3000,'d/l':'two'})
        units=self.run_cli('--top','9')['top_groups']
        self.assertEqual({u['kind'] for u in units},{'file'})
    def test_top_directory_apply_scope_and_outside_copy(self):
        self.tree({'a/x':b'x'*5000,'a/y':b'y'*3000,'b/x':b'x'*5000,'b/y':b'y'*3000,
                   'other/x':b'x'*5000,'big1':b'z'*20000,'big2':b'z'*20000})
        preview=self.run_cli('--top','2')
        self.assertEqual([(u['rank'],u['kind'],u['logical_duplicate_bytes']) for u in preview['top_groups']],
                         [(1,'file',20000),(2,'directory',8000)])
        self.assertEqual((preview['deferred_files'],preview['deferred_logical_bytes']),(1,5000))
        inodes={p:p.stat().st_ino for p in self.root.rglob('*') if p.is_file()}
        applied=self.run_cli('--apply','--top','2')
        self.assertEqual(applied['cloned_files'],3)
        changed={str(p.relative_to(self.root)) for p,i in inodes.items() if p.stat().st_ino!=i}
        self.assertEqual(changed,{'big2','b/x','b/y'})
        rest=self.run_cli('--top','5')['top_groups']
        self.assertEqual([(u['kind'],u['destinations']) for u in rest],[('file',['other/x'])])
    def test_help_layout(self):
        short=subprocess.run([sys.executable,str(SCRIPT)],capture_output=True,text=True)
        self.assertEqual((short.returncode,short.stderr),(0,''))
        self.assertIn("Run 'apfs-dedupe.py --help' for all options.",short.stdout)
        self.assertNotIn('exit status:',short.stdout)
        full=subprocess.run([sys.executable,str(SCRIPT),'--help'],capture_output=True,text=True,
                            env={**os.environ,'COLUMNS':'80'})
        self.assertEqual(full.returncode,0)
        for heading in ('what to do:','output:','state and control:','examples:','what is deduplicated:',
                        'safety:','--top N:','recovery:','results:','exit status:'):
            self.assertIn('\n'+heading+'\n',full.stdout)
        for text in (short.stdout,full.stdout):
            self.assertEqual([l for l in text.splitlines() if len(l)>79],[])
    def test_top_usage_errors(self):
        for args in (('--top','0'),('--top','x'),('--top','3','--control','status')):
            r=self.cli_text(*args,code=2)
            self.assertEqual(r.stdout,'')
            self.assertIn('--top',r.stderr)
    def make_compressed_copy(self,path,content):
        path.write_bytes(b'')
        value=struct.pack('<IIQ',0x636d7066,3,len(content))+zlib.compress(content)
        self.native.set_xattr(path,b'com.apple.decmpfs',value)
        os.chflags(path,d.COMPRESSED)
        self.assertEqual(path.read_bytes(),content)

    def test_compressed_clone_preserves_original_canonical_keeper(self):
        a,b=self.pair()
        c=self.root/'c'
        content=a.read_bytes()
        c.write_bytes(content)
        self.assertEqual(self.run_cli('--apply')['cloned_files'],2)
        journal=self.root/'.apfs-dedupe.jsonl.gz'
        # Older tools recorded the physical source without a canonical field.
        with gzip.open(journal,'rt') as stream:
            records=[json.loads(line) for line in stream]
        with gzip.open(journal,'wt') as stream:
            for record in records:
                record.pop('canonical_keeper',None)
                stream.write(json.dumps(record)+'\n')

        self.make_compressed_copy(b,content)
        donor_inode=b.stat().st_ino
        preview=self.run_cli('--top','1')
        self.assertEqual(preview['top_groups'][0]['canonical_keeper'],'a')
        self.assertEqual({(x['source'],x['destination'],x['canonical_keeper'])
                          for x in preview['changes']},
                         {('b','a','a'),('b','c','a')})
        self.assertEqual(self.run_cli('--apply')['cloned_files'],2)
        self.assertEqual(b.stat().st_ino,donor_inode)
        self.assertTrue(all(path.stat().st_flags & d.COMPRESSED for path in (a,b,c)))
        self.assertEqual(len({self.native.sharing(path).clone_id for path in (a,b,c)}),1)
        self.assertEqual(self.run_cli()['planned_files'],0)
        inspector=SCRIPT.with_name('apfs-inspect.py')
        report=json.loads(subprocess.check_output(
            [sys.executable,str(inspector),'--tree',str(self.root),'--json']))
        self.assertEqual(report['clone_shared_files'],3)

        (self.root/'new').write_bytes(content)
        # Rebuild the index from the journal alone, as on another invocation with
        # an empty state directory. Future duplicates are cloned directly from a.
        fresh_state=self.root.parent/'fresh-state'
        changes=self.run_cli('--state-dir',str(fresh_state))['changes']
        self.assertEqual([(x['source'],x['destination'],x['canonical_keeper'])
                          for x in changes],[('a','new','a')])

        # A path reused for unrelated content is no longer a valid keeper.
        a.unlink()
        a.write_bytes(b'X'*len(content))
        changes=self.run_cli()['changes']
        self.assertEqual([(x['source'],x['destination'],x['canonical_keeper'])
                          for x in changes],[('b','new','b')])

    def test_compressed_copy_is_kept(self):
        a=self.root/'a';a.write_bytes(b'compressible line of text\n'*2048)
        b=self.root/'b'
        self.make_compressed_copy(b,a.read_bytes())
        self.assertLess(b.stat().st_blocks,a.stat().st_blocks)
        preview=self.run_cli('--top','1')
        self.assertEqual([(c['source'],c['destination']) for c in preview['changes']],[('b','a')])
        self.assertEqual(preview['estimated_physical_bytes'],a.stat().st_blocks*512)
        self.assertEqual(preview['top_groups'][0]['estimated_physical_bytes'],a.stat().st_blocks*512)
        content=a.read_bytes()
        self.assertEqual(self.run_cli('--apply')['cloned_files'],1)
        self.assertTrue(a.stat().st_flags & d.COMPRESSED)
        self.assertEqual(a.stat().st_blocks,b.stat().st_blocks)
        self.assertEqual(a.read_bytes(),content)
        # The replaced file now ties with the keeper; it must not become the keeper.
        rerun=self.run_cli('--apply')
        self.assertEqual((rerun['cloned_files'],rerun['planned_files'],rerun['already_completed_files']),(0,0,1))
    def test_sparse_copy_is_kept(self):
        MB=1<<20;head=os.urandom(MB)
        a,b=self.root/'a',self.root/'b'
        with open(a,'wb') as f:
            f.write(head)
            for _ in range(63):f.write(bytes(MB))
        with open(b,'wb') as f:
            f.write(head);f.truncate(64*MB)
        self.assertLess(b.stat().st_blocks*512,4*MB,'fixture is not sparse')
        preview=self.run_cli()
        self.assertEqual([(c['source'],c['destination']) for c in preview['changes']],[('b','a')])
        self.assertGreater(preview['estimated_physical_bytes'],60*MB)
        self.assertEqual(self.run_cli('--apply')['cloned_files'],1)
        self.assertLess(a.stat().st_blocks*512,4*MB)
        self.assertEqual(hashlib.sha256(a.read_bytes()).hexdigest(),hashlib.sha256(b.read_bytes()).hexdigest())
    def test_existing_clone_family_is_kept(self):
        a,b=self.pair();c=self.root/'c'
        self.native.clone(b,c)
        # b and c are one clone family: keep b, so only a's private blocks are counted.
        result=self.run_cli()
        self.assertEqual([(x['source'],x['destination'],x['estimated_physical_bytes']) for x in result['changes']],
                         [('b','a',a.stat().st_blocks*512)])
        self.assertEqual(result['already_shared_files'],1)
        inode=c.stat().st_ino
        self.assertEqual(self.run_cli('--apply')['cloned_files'],1)
        self.assertEqual(c.stat().st_ino,inode)
        self.assertEqual(self.native.sharing(a)[1],self.native.sharing(b)[1])
    def test_smaller_keeper_can_replace_a_previously_completed_clone(self):
        content=b'repeatable content for APFS compression\n'*50_000
        a,b,c=(self.root/name for name in ('a','b','c'))
        a.write_bytes(content)
        b.write_bytes(content)
        self.assertEqual(self.run_cli('--apply')['cloned_files'],1)
        subprocess.run(['/usr/bin/ditto','--noclone','--hfsCompression',str(a),str(c)],check=True)
        if not c.stat().st_flags & d.COMPRESSED:
            self.assertEqual(self.native.decmpfs(c)[1],len(content))
            os.chflags(c,c.stat().st_flags | d.COMPRESSED)
        self.assertEqual(self.run_cli('--apply')['cloned_files'],2)
        keeper_id=self.native.sharing(c).clone_id
        for replaced in (a,b):
            self.assertEqual(replaced.read_bytes(),content)
            self.assertEqual(self.native.sharing(replaced).clone_id,keeper_id)
    def test_compressed_source(self):
        a,b=self.pair()
        # A decmpfs type-3 inline zlib stream, including the native header.
        content=a.read_bytes();a.write_bytes(b'')
        value=struct.pack('<IIQ',0x636d7066,3,len(content))+zlib.compress(content)
        self.native.set_xattr(a,'com.apple.decmpfs',value)
        os.chflags(a,d.COMPRESSED)
        self.assertEqual(a.read_bytes(),content)
        self.assertTrue(a.stat().st_flags & d.COMPRESSED,'compression fixture failed')
        before=self.native.metadata(b)
        self.assertEqual(self.run_cli('--apply')['cloned_files'],1)
        self.assertEqual(self.native.metadata(b),before)
        self.assertEqual(a.read_bytes(),b.read_bytes())
        self.assertTrue(b.stat().st_flags & d.COMPRESSED)

    def test_inline_compression_preserves_an_independent_hidden_resource_fork(self):
        content = b'logically identical data' * 100
        header = struct.pack('<IIQ', 0x636d7066, 3, len(content))
        fork = b'independent resource fork' * 1600
        a, b = self.root / 'a', self.root / 'b'
        for path in (a, b):
            path.touch()
            self.native.set_xattr(path, 'com.apple.decmpfs', header + zlib.compress(content))
        self.native.set_xattr(b, 'com.apple.ResourceFork', fork)
        for path in (a, b):
            os.chflags(path, d.COMPRESSED)
        self.assertEqual(b.read_bytes(), content)
        self.assertNotIn(b'com.apple.ResourceFork', self.native.xattr_names(b))
        original_inode = b.stat().st_ino
        result = self.run_cli('--apply')
        self.assertEqual(result['cloned_files'], 0)
        self.assertEqual(b.stat().st_ino, original_inode)
        self.assertEqual(self.native.xattr(b, 'com.apple.ResourceFork', XATTR_SHOWCOMPRESSION), fork)
        self.assertEqual(result['skipped_files'], [{
            'path': 'b',
            'reason': 'Compressed file has unverified hidden xattrs (com.apple.ResourceFork)',
        }])

    def test_renamed_root_keeps_its_journal(self):
        self.pair()
        self.assertEqual(self.run_cli('--apply')['cloned_files'], 1)
        moved = self.root.with_name('moved')
        self.root.rename(moved)
        # The state directory is bound to the root path; the journal is not.
        self.root, self.state = moved, moved.parent / 'moved-state'
        result = self.run_cli('--apply')
        self.assertEqual((result['cloned_files'], result['already_completed_files']), (0, 1))
        copy = moved.with_name('copy')
        subprocess.run(['/bin/cp', '-R', str(moved), str(copy)], check=True)
        self.root, self.state = copy, moved.parent / 'copy-state'
        self.assertIn('another root', self.run_cli(code=1)['message'])

    def test_apply_removes_an_orphan_stage_directory(self):
        self.pair()
        orphan = self.root / (d.PREFIX + '0' * 32)
        orphan.mkdir(mode=0o700)
        kept = self.root / (d.PREFIX + '1' * 32)
        kept.mkdir(mode=0o700)
        (kept / 'payload').write_bytes(b'unjournaled')
        self.run_cli()
        self.assertTrue(orphan.exists())
        self.assertEqual(self.run_cli('--apply')['cloned_files'], 1)
        self.assertFalse(orphan.exists())
        self.assertEqual((kept / 'payload').read_bytes(), b'unjournaled')

    def test_recovery_follows_a_changed_device_number(self):
        a, b, item = self.exchanged()
        journal = self.root / '.apfs-dedupe.jsonl.gz'
        records = [json.loads(line) for line in gzip.decompress(journal.read_bytes()).splitlines()]
        self.assertIn('volume', records[0])
        for record in records:
            for key in ('staging_directory', 'original', 'signature'):
                if record['event'] in ('stage', 'prepared') and key in record:
                    record[key][0] += 7
        journal.write_bytes(b''.join(gzip.compress((json.dumps(record, sort_keys=True) + '\n').encode(), mtime=0)
                                     for record in records))
        self.assertEqual(self.run_cli('--apply')['cloned_files'], 1)
        self.assertFalse(list(self.root.glob(d.PREFIX + '*')))
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_dataless_files_are_not_eligible(self):
        a, _ = self.pair()
        s = a.stat()
        fields = dict(st_mode=s.st_mode, st_nlink=1, st_size=s.st_size, st_uid=s.st_uid)
        self.assertTrue(d.eligible(d.SimpleNamespace(**fields, st_flags=0)))
        self.assertFalse(d.eligible(d.SimpleNamespace(**fields, st_flags=d.SF_DATALESS)))

if __name__=='__main__':unittest.main(verbosity=2)
