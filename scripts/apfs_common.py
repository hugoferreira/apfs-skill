"""Shared macOS/APFS primitives for the apfs-dev scripts.

One place for the libSystem bindings (clonefile, copyfile, renamex_np, getattrlist,
statfs, xattr, ACL, libproc) and for the small filesystem helpers every script needs.
Python 3.9+, stdlib only. Importing works on any platform; Native() requires macOS.

Layout:
  constants      flag values from <sys/stat.h>, <sys/attr.h>, <copyfile.h>, <sys/xattr.h>
  Native         one instance per process; every libSystem call goes through it
  free functions signatures, hashing, durable writes, tree removal, formatting
"""
import collections
import ctypes as C
import errno
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import struct
import sys
import uuid

# <sys/stat.h> st_flags
UF_IMMUTABLE, UF_APPEND, UF_COMPRESSED, SF_DATALESS = 0x2, 0x4, 0x20, 0x40000000
# Immutable, append-only, nounlink, restricted, datavault and system variants.
UNSAFE_FLAGS = 0x2 | 0x4 | 0x10 | 0x80 | 0x10000 | 0x20000 | 0x40000 | 0x80000 | 0x100000
# <sys/stat.h> extended flags from ATTR_CMNEXT_EXT_FLAGS
EF_MAY_SHARE_BLOCKS, EF_IS_PURGEABLE, EF_IS_SPARSE, EF_SHARES_ALL_BLOCKS = 0x1, 0x8, 0x10, 0x40
# <sys/clonefile.h>, <stdio.h> renamex_np
CLONE_NOFOLLOW, CLONE_NOOWNERCOPY, RENAME_SWAP = 0x1, 0x2, 0x2
RENAME_EXCL = 0x4
# <sys/xattr.h>
XATTR_NOFOLLOW, XATTR_SHOWCOMPRESSION = 0x1, 0x20
# <sys/attr.h>
FSOPT_NOFOLLOW, FSOPT_ATTR_CMN_EXTENDED = 0x1, 0x20
ATTR_CMN_CRTIME, ATTR_CMN_GEN_COUNT, ATTR_CMN_RETURNED_ATTRS = 0x200, 0x00080000, 0x80000000
ATTR_CMNEXT_PRIVATESIZE, ATTR_CMNEXT_CLONEID = 0x8, 0x100
ATTR_CMNEXT_EXT_FLAGS, ATTR_CMNEXT_CLONE_REFCNT = 0x200, 0x1000
ATTR_VOL_INFO, ATTR_VOL_CAPABILITIES, ATTR_VOL_UUID = 0x80000000, 0x00020000, 0x00040000
VOL_FORMAT = {0x40: 'sparse_files', 0x100: 'case_sensitive', 0x10000: 'decmpfs_compression',
              0x02000000: 'sealed', 0x04000000: 'clone_mapping'}
VOL_INTERFACES = {0x10000: 'clone', 0x20000: 'snapshot', 0x40000: 'rename_swap', 0x80000: 'rename_excl'}
# <copyfile.h>
COPYFILE_METADATA, COPYFILE_ALL = 7, 15
COPYFILE_EXCL, COPYFILE_NOFOLLOW, COPYFILE_CLONE = 1 << 17, (1 << 18) | (1 << 19), 1 << 24
COPYFILE_STATE_STATUS_CB, COPYFILE_STATE_COPIED = 6, 8
COPYFILE_COPY_DATA, COPYFILE_ERR, COPYFILE_PROGRESS, COPYFILE_QUIT = 4, 3, 4, 2
# <sys/acl.h>
ACL_TYPE_EXTENDED = 0x100
EMPTY_ACL = b'!#acl 1\n'
STAGE_PAYLOAD = 'payload'
STAGING_PREFIXES = ('.apfs-dedupe-stage-', '.apfs-transfer-stage-', '.apfs-compress-plus-')
# decmpfs: transparent compression metadata. Odd/even pairs are the same codec with the
# payload in the xattr or in the resource fork.
DECMPFS_XATTR, DECMPFS_MAGIC = 'com.apple.decmpfs', b'fpmc'
DECMPFS_CODECS = {3: 'zlib', 4: 'zlib', 7: 'lzvn', 8: 'lzvn', 11: 'lzfse', 12: 'lzfse'}
DECMPFS_RESOURCE_FORK_TYPES = frozenset({4, 8, 12})
RESOURCE_FORK_XATTR = b'com.apple.ResourceFork'
PROVENANCE_XATTR = b'com.apple.provenance'
BACKUP_EXCLUDE_XATTR = 'com.apple.metadata:com_apple_backup_excludeItem'

Sharing = collections.namedtuple('Sharing', 'private clone_id ext_flags refcnt')
Mount = collections.namedtuple('Mount', 'point device fstype')
OpenUse = collections.namedtuple('OpenUse', 'pid access')   # access: r, w, u, - or mapped


class Conflict(Exception):
    """State on disk contradicts what an operation recorded or expects."""


class AttrList(C.Structure):
    _fields_ = [('count', C.c_uint16), ('reserved', C.c_uint16),
                ('common', C.c_uint32), ('vol', C.c_uint32),
                ('dir', C.c_uint32), ('file', C.c_uint32), ('fork', C.c_uint32)]


class StatFS(C.Structure):
    _fields_ = [('f_bsize', C.c_uint32), ('f_iosize', C.c_int32), ('f_blocks', C.c_uint64),
                ('f_bfree', C.c_uint64), ('f_bavail', C.c_uint64), ('f_files', C.c_uint64),
                ('f_ffree', C.c_uint64), ('f_fsid', C.c_int32 * 2), ('f_owner', C.c_uint32),
                ('f_type', C.c_uint32), ('f_flags', C.c_uint32), ('f_fssubtype', C.c_uint32),
                ('f_fstypename', C.c_char * 16), ('f_mntonname', C.c_char * 1024),
                ('f_mntfromname', C.c_char * 1024), ('f_flags_ext', C.c_uint32),
                ('f_reserved', C.c_uint32 * 7)]


COPYFILE_CALLBACK = C.CFUNCTYPE(C.c_int, C.c_int, C.c_int, C.c_void_p, C.c_char_p, C.c_char_p, C.c_void_p)

_SIGNATURES = {
    'clonefile': ([C.c_char_p, C.c_char_p, C.c_uint32], C.c_int),
    'copyfile': ([C.c_char_p, C.c_char_p, C.c_void_p, C.c_uint32], C.c_int),
    'copyfile_state_alloc': ([], C.c_void_p),
    'copyfile_state_free': ([C.c_void_p], C.c_int),
    'copyfile_state_set': ([C.c_void_p, C.c_uint32, C.c_void_p], C.c_int),
    'copyfile_state_get': ([C.c_void_p, C.c_uint32, C.c_void_p], C.c_int),
    'renamex_np': ([C.c_char_p, C.c_char_p, C.c_uint], C.c_int),
    'statfs': ([C.c_char_p, C.POINTER(StatFS)], C.c_int),
    'listxattr': ([C.c_char_p, C.c_void_p, C.c_size_t, C.c_int], C.c_ssize_t),
    'getxattr': ([C.c_char_p, C.c_char_p, C.c_void_p, C.c_size_t, C.c_uint32, C.c_int], C.c_ssize_t),
    'setxattr': ([C.c_char_p, C.c_char_p, C.c_void_p, C.c_size_t, C.c_uint32, C.c_int], C.c_int),
    'removexattr': ([C.c_char_p, C.c_char_p, C.c_int], C.c_int),
    'getattrlist': ([C.c_char_p, C.c_void_p, C.c_void_p, C.c_size_t, C.c_uint32], C.c_int),
    'setattrlist': ([C.c_char_p, C.c_void_p, C.c_void_p, C.c_size_t, C.c_uint32], C.c_int),
    'acl_get_link_np': ([C.c_char_p, C.c_int], C.c_void_p),
    'acl_get_file': ([C.c_char_p, C.c_int], C.c_void_p),
    'acl_init': ([C.c_int], C.c_void_p),
    'acl_set_file': ([C.c_char_p, C.c_int, C.c_void_p], C.c_int),
    'acl_set_fd_np': ([C.c_int, C.c_void_p, C.c_int], C.c_int),
    'acl_to_text': ([C.c_void_p, C.c_void_p], C.c_void_p),
    'acl_from_text': ([C.c_char_p], C.c_void_p),
    'acl_free': ([C.c_void_p], C.c_int),
    'proc_listallpids': ([C.c_void_p, C.c_int], C.c_int),
    'proc_pidinfo': ([C.c_int, C.c_int, C.c_uint64, C.c_void_p, C.c_int], C.c_int),
    'proc_pidfdinfo': ([C.c_int, C.c_int, C.c_int, C.c_void_p, C.c_int], C.c_int),
    'proc_name': ([C.c_int, C.c_void_p, C.c_uint32], C.c_int),
}


def check(result):
    """Raise OSError from errno when a libSystem call returned a negative value."""
    if result < 0:
        e = C.get_errno()
        raise OSError(e, os.strerror(e))
    return result


def _fail(path):
    e = C.get_errno()
    return OSError(e, os.strerror(e), os.fsdecode(path))


class Native:
    """libSystem calls with declared signatures. Stateless; create one per process."""

    def __init__(self):
        if sys.platform != 'darwin':
            raise OSError('macOS required')
        self.lib = C.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        for name, (args, result) in _SIGNATURES.items():
            fn = getattr(self.lib, name)
            fn.argtypes, fn.restype = args, result

    # ------------------------------------------------------------ volumes
    def mount(self, path):
        """Mount(point, device, fstype) of the volume holding path, from statfs(2).

        st_dev cannot tell volumes apart: `/` (the sealed system volume) and the Data
        volume report the same value.
        """
        sf = StatFS()
        if self.lib.statfs(os.fsencode(path), C.byref(sf)) < 0:
            raise _fail(path)
        return Mount(os.fsdecode(sf.f_mntonname), os.fsdecode(sf.f_mntfromname),
                     os.fsdecode(sf.f_fstypename))

    def capabilities(self, path):
        """(format, interfaces) capability dicts of the volume holding path."""
        attr = AttrList(5, 0, 0, ATTR_VOL_INFO | ATTR_VOL_CAPABILITIES, 0, 0, 0)
        buf = C.create_string_buffer(64)
        if self.lib.getattrlist(os.fsencode(path), C.byref(attr), buf, 64, 0) < 0:
            raise _fail(path)
        caps = struct.unpack_from('8I', buf, 4)   # capabilities[4], then valid[4]
        return ({name: bool(caps[0] & bit) for bit, name in VOL_FORMAT.items() if caps[4] & bit},
                {name: bool(caps[1] & bit) for bit, name in VOL_INTERFACES.items() if caps[5] & bit})

    def volume_identity(self, path):
        """{'uuid', 'root_inode'} of the APFS volume holding path; ValueError otherwise."""
        if self.mount(path).fstype != 'apfs':
            raise ValueError('APFS required: ' + str(path))
        attr = AttrList(5, 0, 0, ATTR_VOL_INFO | ATTR_VOL_UUID, 0, 0, 0)
        buf = C.create_string_buffer(64)
        if self.lib.getattrlist(os.fsencode(path), C.byref(attr), buf, 64, 0) < 0:
            raise _fail(path)
        return {'uuid': str(uuid.UUID(bytes=buf.raw[4:20])), 'root_inode': os.stat(path).st_ino}

    # ------------------------------------------------------------ clone, copy, swap
    def clone(self, source, destination, flags=0):
        """clonefile(2). A directory source clones the whole hierarchy in one call."""
        if self.lib.clonefile(os.fsencode(source), os.fsencode(destination), flags) != 0:
            raise _fail(source)

    def copyfile(self, source, destination, flags, on_progress=None):
        """copyfile(3). on_progress(bytes_copied or None) runs at every callback stage
        and may raise to abort the copy; the exception is re-raised here."""
        src, dst = os.fsencode(source), os.fsencode(destination)
        if on_progress is None:
            if self.lib.copyfile(src, dst, None, flags) < 0:
                raise _fail(source)
            return
        lib = self.lib
        state = lib.copyfile_state_alloc()
        if not state:
            raise OSError(errno.ENOMEM, 'Cannot allocate copyfile state')
        raised = []

        @COPYFILE_CALLBACK
        def callback(what, stage, current, _src, _dst, _context):
            try:
                copied = None
                if what == COPYFILE_COPY_DATA and stage == COPYFILE_PROGRESS:
                    value = C.c_int64()
                    check(lib.copyfile_state_get(current, COPYFILE_STATE_COPIED, C.byref(value)))
                    copied = value.value
                on_progress(copied)
                return COPYFILE_QUIT if stage == COPYFILE_ERR else 0
            except BaseException as exc:   # must not propagate through the C frame
                raised.append(exc)
                return COPYFILE_QUIT
        try:
            check(lib.copyfile_state_set(state, COPYFILE_STATE_STATUS_CB, C.cast(callback, C.c_void_p)))
            result = lib.copyfile(src, dst, state, flags)
            failure = _fail(source) if result < 0 else None
            if raised:
                raise raised[0]
            if failure:
                raise failure
        finally:
            lib.copyfile_state_free(state)

    def swap(self, a, b):
        """RENAME_SWAP exchanges two names atomically; neither is ever missing."""
        check(self.lib.renamex_np(os.fsencode(a), os.fsencode(b), RENAME_SWAP))

    def rename_exclusive(self, source, destination):
        """Publish atomically only if the destination name is absent."""
        check(self.lib.renamex_np(os.fsencode(source), os.fsencode(destination), RENAME_EXCL))

    # ------------------------------------------------------------ open files
    def _open_uses(self, mappings):
        """[(device, inode, pid, access)] for files other processes hold open or mapped.

        sys/proc_info.h: PROC_PIDLISTFDS and PROC_PIDFDVNODEINFO for descriptors;
        flavor 22 (PROC_PIDREGIONPATHINFO2, as used by libproc) walks the file-backed
        memory regions, which finds mappings whose descriptor is closed. That walk
        takes most of the time; mappings=False omits it. Processes this user may not
        inspect (other users', unless root) contribute nothing.
        """
        lib = self.lib
        count = check(lib.proc_listallpids(None, 0)) + 64
        pids = (C.c_int * count)()
        count = check(lib.proc_listallpids(pids, C.sizeof(pids)))
        found, me = [], os.getpid()
        fds = C.create_string_buffer(8 * 1024)
        vnode = C.create_string_buffer(176)      # struct vnode_fdinfo
        region = C.create_string_buffer(1272)    # struct proc_regionwithpathinfo
        for pid in pids[:count]:
            if pid in (0, me):
                continue
            size = lib.proc_pidinfo(pid, 1, 0, None, 0)
            if size <= 0:
                continue
            if 2 * size > len(fds):
                fds = C.create_string_buffer(2 * size)
            used = lib.proc_pidinfo(pid, 1, 0, fds, len(fds))
            for offset in range(0, max(used, 0), 8):
                fd, kind = struct.unpack_from('iI', fds, offset)
                # PROX_FDTYPE_VNODE. proc_fileinfo (24 bytes) starts with fi_openflags
                # (FREAD 1, FWRITE 2; neither for an event-only open); vinfo_stat follows.
                if kind == 1 and lib.proc_pidfdinfo(pid, fd, 1, vnode, 176) == 176:
                    flags, device, inode = struct.unpack_from('I20xI4xQ', vnode, 0)
                    found.append((device, inode, pid, ('-', 'r', 'w', 'u')[flags & 3]))
            address = 0
            while mappings and lib.proc_pidinfo(pid, 22, address, region, 1272) > 0:
                # pri_address and pri_size end the 96-byte proc_regioninfo.
                base, length = struct.unpack_from('QQ', region, 80)
                device, inode = struct.unpack_from('I4xQ', region, 96)
                if inode:
                    found.append((device, inode, pid, 'mapped'))
                address = base + length
        return found

    def open_files(self, mappings=True):
        """{(device, inode)} of files other processes hold open or mapped."""
        return {use[:2] for use in self._open_uses(mappings)}

    def open_file_users(self, mappings=True):
        """{(device, inode): [OpenUse(pid, access)]} for the same files."""
        users = {}
        for device, inode, pid, access in self._open_uses(mappings):
            users.setdefault((device, inode), []).append(OpenUse(pid, access))
        return users

    def process_name(self, pid):
        buf = C.create_string_buffer(64)
        return buf.value.decode(errors='replace') if self.lib.proc_name(pid, buf, 64) > 0 else None

    # ------------------------------------------------------------ per-file attributes
    def generation(self, path):
        """Return ATTR_CMN_GEN_COUNT, or None where the volume omits it.

        The count changes when file data is written, including through a mapping once
        it is unmapped, and reads 0 while a writable shared mapping exists. Renames and
        metadata changes leave it alone.
        """
        attr = AttrList(5, 0, ATTR_CMN_RETURNED_ATTRS | ATTR_CMN_GEN_COUNT, 0, 0, 0, 0)
        buf = C.create_string_buffer(32)
        check(self.lib.getattrlist(os.fsencode(path), C.byref(attr), buf, 32,
                                   FSOPT_ATTR_CMN_EXTENDED | FSOPT_NOFOLLOW))
        if int.from_bytes(buf.raw[4:8], sys.byteorder) & ATTR_CMN_GEN_COUNT:
            return int.from_bytes(buf.raw[24:28], sys.byteorder)

    def sharing(self, path):
        """Sharing(private, clone_id, ext_flags, refcnt); None where APFS omits a value.

        private    bytes freed if this file alone were deleted (reported as 0 for
                   UF_COMPRESSED files).
        clone_id   equal ids mean one shared data stream; a write gives the written
                   file a new id.
        ext_flags  EF_* bits.
        refcnt     number of files sharing the data stream.
        Values are packed in bit order after the returned-attributes set.
        """
        wanted = ((ATTR_CMNEXT_PRIVATESIZE, 8), (ATTR_CMNEXT_CLONEID, 8),
                  (ATTR_CMNEXT_EXT_FLAGS, 8), (ATTR_CMNEXT_CLONE_REFCNT, 4))
        attr = AttrList(5, 0, ATTR_CMN_RETURNED_ATTRS, 0, 0, 0, sum(bit for bit, _ in wanted))
        buf = C.create_string_buffer(64)
        if self.lib.getattrlist(os.fsencode(path), C.byref(attr), buf, 64,
                                FSOPT_ATTR_CMN_EXTENDED | FSOPT_NOFOLLOW) < 0:
            if C.get_errno() in (errno.EINVAL, errno.ENOTSUP):
                return Sharing(None, None, None, None)
            raise _fail(path)
        returned = int.from_bytes(buf.raw[20:24], sys.byteorder)
        values, offset = [], 24
        for bit, width in wanted:
            if returned & bit:
                values.append(int.from_bytes(buf.raw[offset:offset + width], sys.byteorder))
                offset += width
            else:
                values.append(None)
        return Sharing(*values)

    def xattr_names(self, path, options=0):
        p = os.fsencode(path)
        size = self.lib.listxattr(p, None, 0, options)
        if size < 0:
            raise _fail(path)
        buf = C.create_string_buffer(max(size, 1))
        used = self.lib.listxattr(p, buf, size, options)
        if used < 0:
            raise _fail(path)
        return [name for name in buf.raw[:used].split(b'\0') if name]

    def xattr(self, path, name, options=0):
        """Value of one extended attribute, or None if the file has none by that name."""
        p, key = os.fsencode(path), os.fsencode(name)
        size = self.lib.getxattr(p, key, None, 0, 0, options)
        if size >= 0:
            buf = C.create_string_buffer(max(size, 1))
            size = self.lib.getxattr(p, key, buf, size, 0, options)
            if size >= 0:
                return buf.raw[:size]
        if C.get_errno() == errno.ENOATTR:
            return None
        raise _fail(path)

    def xattrs(self, path, options=0):
        """{name: value} of all extended attributes; names and values are bytes."""
        result = {}
        for name in self.xattr_names(path, options):
            value = self.xattr(path, name, options)
            if value is not None:   # removed between the two calls
                result[name] = value
        return result

    def set_xattr(self, path, name, value, options=0):
        check(self.lib.setxattr(os.fsencode(path), os.fsencode(name), value, len(value), 0, options))

    def remove_xattr(self, path, name, options=0):
        check(self.lib.removexattr(os.fsencode(path), os.fsencode(name), options))

    def decmpfs(self, path):
        """(compression type, uncompressed size) from the decmpfs header, or None.

        The attribute is hidden while UF_COMPRESSED is set; XATTR_SHOWCOMPRESSION
        reveals it. It is also returned for a file that lost the flag.
        """
        try:
            header = self.xattr(path, DECMPFS_XATTR, XATTR_NOFOLLOW | XATTR_SHOWCOMPRESSION)
        except OSError:
            return None
        if header is None or len(header) < 16 or header[:4] != DECMPFS_MAGIC:
            return None
        return struct.unpack('<IQ', header[4:16])

    def codec(self, path):
        """'zlib', 'lzvn' or 'lzfse' for a transparently compressed file, else None."""
        header = self.decmpfs(path)
        return DECMPFS_CODECS.get(header[0]) if header else None

    def require_verified_compression_xattrs(self, path, visible=None):
        """Reject compressed files whose user metadata cannot be verified."""
        if visible is None:
            visible = self.xattr_names(path, XATTR_NOFOLLOW)
        hidden = set(self.xattr_names(path, XATTR_NOFOLLOW | XATTR_SHOWCOMPRESSION))
        hidden.difference_update(visible)
        hidden.discard(os.fsencode(DECMPFS_XATTR))
        if RESOURCE_FORK_XATTR in hidden:
            header = self.decmpfs(path)
            if header and header[0] in DECMPFS_RESOURCE_FORK_TYPES:
                hidden.remove(RESOURCE_FORK_XATTR)
        if hidden:
            names = ', '.join(os.fsdecode(name) for name in sorted(hidden))
            raise OSError(errno.ENOTSUP, f'Compressed file has unverified hidden xattrs ({names})',
                          os.fspath(path))

    def exclude_from_backups(self, path):
        """Same effect as `tmutil addexclusion PATH`, which takes seconds to run."""
        self.set_xattr(path, BACKUP_EXCLUDE_XATTR,
                       plistlib.dumps('com.apple.backupd', fmt=plistlib.FMT_BINARY))

    def acl(self, path, nofollow=False):
        fn = self.lib.acl_get_link_np if nofollow else self.lib.acl_get_file
        ptr = fn(os.fsencode(path), ACL_TYPE_EXTENDED)
        if not ptr:
            if C.get_errno() != errno.ENOENT:   # ENOENT: the file has no ACL
                check(-1)
            ptr = self.lib.acl_init(0)
        if not ptr:
            check(-1)
        try:
            text = self.lib.acl_to_text(ptr, None)
            if not text:
                check(-1)
            try:
                return C.string_at(text)
            finally:
                self.lib.acl_free(text)
        finally:
            self.lib.acl_free(ptr)

    def clear_fd_acl(self, fd):
        acl = self.lib.acl_init(0)
        if not acl:
            check(-1)
        try:
            check(self.lib.acl_set_fd_np(fd, acl, ACL_TYPE_EXTENDED))
        finally:
            self.lib.acl_free(acl)

    def birth(self, path, nofollow=False):
        """Creation time as the raw 16-byte timespec getattrlist returns."""
        attr = AttrList(5, 0, ATTR_CMN_CRTIME, 0, 0, 0, 0)
        buf = C.create_string_buffer(20)
        check(self.lib.getattrlist(os.fsencode(path), C.byref(attr), buf, 20, int(nofollow)))
        return buf.raw[4:20]

    def set_birth(self, path, value, nofollow=False):
        attr = AttrList(5, 0, ATTR_CMN_CRTIME, 0, 0, 0, 0)
        buf = C.create_string_buffer(value)
        check(self.lib.setattrlist(os.fsencode(path), C.byref(attr), buf, len(value), int(nofollow)))

    def metadata(self, path, nofollow=False):
        """(mode, uid, gid, flags, mtime_ns, birthtime, xattrs, ACL text).

        UF_COMPRESSED is masked out: it describes storage, not user metadata.
        """
        s = os.lstat(path)
        attrs = self.xattrs(path, XATTR_NOFOLLOW if nofollow else 0)
        if s.st_flags & UF_COMPRESSED:
            self.require_verified_compression_xattrs(path, attrs)
        return (s.st_mode, s.st_uid, s.st_gid, s.st_flags & ~UF_COMPRESSED,
                s.st_mtime_ns, self.birth(path, nofollow), attrs, self.acl(path, nofollow))

    def restore(self, path, metadata, atime):
        """Apply metadata() of another file to the regular file at path."""
        mode, uid, gid, flags, mtime, birth, attrs, acl = metadata
        p = os.fsencode(path)
        # Hidden APFS compression xattrs must stay paired with UF_COMPRESSED.
        current_attrs = self.xattr_names(path)
        if os.lstat(path).st_flags & UF_COMPRESSED:
            self.require_verified_compression_xattrs(path, current_attrs)
        for name in current_attrs:
            if name not in attrs:
                self.remove_xattr(path, name)
        for name, value in attrs.items():
            self.set_xattr(path, name, value)
        os.chown(path, uid, gid)
        os.chmod(path, stat.S_IMODE(mode))
        ptr = self.lib.acl_from_text(acl)
        if not ptr:
            check(-1)
        try:
            check(self.lib.acl_set_file(p, ACL_TYPE_EXTENDED, ptr))
        finally:
            self.lib.acl_free(ptr)
        os.chflags(path, flags | (os.stat(path).st_flags & UF_COMPRESSED))
        os.utime(path, ns=(atime, mtime))
        self.set_birth(path, birth)


# ---------------------------------------------------------------- signatures, hashing
def signature(s):
    """Identity of one file state: any write, rename-over or metadata change alters it."""
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns,
            s.st_mode, s.st_uid, s.st_gid, s.st_flags, s.st_nlink]


def on_device(recorded, device):
    """A recorded signature or [device, inode] identity with the current device number.

    Device numbers follow attach order and change across remounts and reboots.
    Use this only after the volume UUID has been matched.
    """
    return [device, *recorded[1:]] if recorded else recorded


def unchanged(current, recorded):
    """Compare signatures except ctime, which every rename updates."""
    return current[:4] == recorded[:4] and current[5:] == recorded[5:]


def metadata_digest(value):
    mode, uid, gid, flags, mtime, birthtime, attrs, acl = value
    payload = [mode, uid, gid, flags, mtime, birthtime.hex(),
               sorted((key.hex(), data.hex()) for key, data in attrs.items()), acl.hex()]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def file_checkpoint(native, path):
    before = signature(path.lstat())
    if not stat.S_ISREG(before[5]) or before[-1] != 1:
        raise Conflict(f'Expected an independently linked regular file: {path}')
    generation = native.generation(path)
    metadata = metadata_digest(native.metadata(path, nofollow=True))
    if generation == 0 or native.generation(path) != generation or signature(path.lstat()) != before:
        raise Conflict(f'File changed while verifying: {path}')
    return {'signature': before, 'generation': generation, 'metadata': metadata}


def matches_checkpoint(native, path, expected):
    try:
        observed = file_checkpoint(native, path)
    except (OSError, Conflict):
        return False
    return (unchanged(observed['signature'], expected['signature'])
            and observed['generation'] == expected['generation']
            and observed['metadata'] == expected['metadata'])


def lstat_or_none(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def hash_fd(fd, on_bytes=None):
    """SHA-256 of the whole file behind fd; on_bytes(n) after each chunk."""
    os.lseek(fd, 0, os.SEEK_SET)
    h = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return h.hexdigest()
        h.update(chunk)
        if on_bytes:
            on_bytes(len(chunk))


# ---------------------------------------------------------------- paths and durability
def safe_path(root, relative):
    """root/relative for a recorded relative path; reject escapes and symlinked parents."""
    p = root / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise Conflict('Recorded path lies outside its root: ' + str(relative))
    if p.parent.resolve() != p.parent:
        raise Conflict('Symlink parent changed: ' + str(p))
    return p


def existing_ancestor(path):
    """Nearest existing ancestor of an absolute path (the path itself if it exists)."""
    p = os.path.abspath(path)
    while not os.path.lexists(p):
        p = os.path.dirname(p)
    return p


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_stage_directory(native, directory, identity, missing_ok=False):
    """Return the inode-addressed path to a verified owner-only stage.

    Use Darwin volfs so a rename in a shared parent cannot redirect a clone
    or metadata write into a substituted directory.
    """
    if (not isinstance(identity, (tuple, list)) or len(identity) != 2
            or any(type(value) is not int or value <= 0 for value in identity)):
        raise Conflict('Invalid staging directory identity')
    device, inode = identity
    anchor = Path('/.vol') / str(device) / str(inode)
    named, anchored = lstat_or_none(directory), lstat_or_none(anchor)
    if named is None and anchored is None and missing_ok:
        return None
    for status in (named, anchored):
        if (status is None or (status.st_dev, status.st_ino) != (device, inode)
                or status.st_uid != os.geteuid() or not stat.S_ISDIR(status.st_mode)
                or stat.S_IMODE(status.st_mode) != 0o700):
            raise Conflict(f'Staging directory moved, changed or unavailable: {directory} ({anchor})')
    if native.acl(anchor, nofollow=True) != EMPTY_ACL:
        raise Conflict(f'Staging directory has an access ACL: {directory}')
    return anchor


def make_private_stage(native, directory):
    """Create an empty private stage, removing any inherited ACL before use."""
    directory.mkdir(mode=0o700)
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        remove_orphan_stage(directory)
        raise
    st = os.fstat(fd)
    try:
        identity = [st.st_dev, st.st_ino]
        anchor = Path('/.vol') / str(st.st_dev) / str(st.st_ino)
        if st.st_uid != os.geteuid():
            raise Conflict(f'Staging directory is not owned by this user: {directory}')
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
        if native.acl(anchor, nofollow=True) != EMPTY_ACL:
            native.clear_fd_acl(fd)
        private_stage_directory(native, directory, identity)
        sync_dir(directory.parent)
        return identity
    except BaseException:
        if (st.st_dev, st.st_ino) == _identity(lstat_or_none(directory)):
            remove_orphan_stage(directory)
        raise
    finally:
        os.close(fd)


def _identity(status):
    return (status.st_dev, status.st_ino) if status is not None else None


def remove_orphan_stage(directory):
    """Remove an empty stage directory of this user; True if it is gone.

    A crash between mkdir and the journal record leaves one. rmdir refuses a
    directory with entries, so a stage in use or awaiting recovery is kept.
    """
    status = lstat_or_none(directory)
    if status is None:
        return True
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
        return False
    try:
        os.rmdir(directory)
    except OSError:
        return False
    return True


def tracked_staging_path(native, root, destination, record, prefix):
    """Return the journaled name and the accessible payload, or None if the stage is gone."""
    temporary = safe_path(root, record['temporary'])
    if 'staging_directory' not in record:
        raise Conflict('Staging record of an earlier format; delete the state directory or journal that holds it')
    directory = temporary.parent
    if (directory.parent != destination.parent or not directory.name.startswith(prefix)
            or temporary.name != STAGE_PAYLOAD):
        raise Conflict('Invalid private staging path in journal')
    anchor = private_stage_directory(native, directory, record['staging_directory'], missing_ok=True)
    return temporary, anchor / STAGE_PAYLOAD if anchor is not None else None


def remove_private_stage(native, temporary, identity):
    """Remove an empty stage, preserving unexpected entries for inspection."""
    if private_stage_directory(native, temporary.parent, identity, missing_ok=True) is not None:
        temporary.parent.rmdir()
        sync_dir(temporary.parent.parent)


def atomic_write(path, data, durable=False):
    """Replace path with data (str or bytes) through a sibling temporary file.

    Readers see the old or the new content, never a partial file. durable=True also
    fsyncs the data before the rename.
    """
    path = Path(path)
    temporary = path.with_name(f'{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        text = isinstance(data, str)
        with temporary.open('x' if text else 'xb', encoding='utf-8' if text else None) as out:
            out.write(data)
            if durable:
                out.flush()
                os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_json(path, value, **options):
    atomic_write(path, json.dumps(value, **options) + '\n')


def unlock(path):
    """Clear what stops the owner from deleting path: uchg/uappnd flags, missing u+rwx."""
    st = os.lstat(path)
    if st.st_flags & (UF_IMMUTABLE | UF_APPEND):
        os.lchflags(path, st.st_flags & ~(UF_IMMUTABLE | UF_APPEND))
    if stat.S_ISDIR(st.st_mode) and st.st_mode & 0o700 != 0o700:
        os.chmod(path, st.st_mode | 0o700)


def force_rmtree(path):
    """Remove a file or tree, including read-only directories and uchg files."""
    path = str(path)
    if not os.path.lexists(path):
        return
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
        return
    try:
        shutil.rmtree(path)
        return
    except OSError:
        pass
    stack = [path]
    while stack:
        directory = stack.pop()
        unlock(directory)
        with os.scandir(directory) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                else:
                    unlock(entry.path)
    shutil.rmtree(path)


# ---------------------------------------------------------------- numbers
def allocated_bytes(s):
    """st_blocks counts compressed storage and omits sparse holes."""
    return s.st_blocks * 512


def free_bytes(path):
    """Free space as df reports it; excludes purgeable space."""
    status = os.statvfs(path)
    return status.f_bavail * status.f_frsize


def human_bytes(value, compact=False):
    """'1.5 MiB' (or '1.5M' with compact=True for table columns); None prints as '-'."""
    if value is None:
        return '-'
    amount = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(amount) < 1024 or unit == 'TiB':
            label = unit[0] if compact else ' ' + unit
            return f'{amount:.0f}{label}' if unit == 'B' else f'{amount:.1f}{label}'
        amount /= 1024
