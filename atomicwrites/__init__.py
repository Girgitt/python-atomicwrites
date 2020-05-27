import contextlib
import io
import os
import sys
import time
import errno
import ctypes
import random
import tempfile

try:
    import fcntl
except ImportError:
    fcntl = None

__version__ = '1.3.0'


PY2 = sys.version_info[0] == 2

text_type = unicode if PY2 else str  # noqa

transactional_ntfs_supported = True


def _path_to_unicode(x):
    if not isinstance(x, text_type):
        return x.decode(sys.getfilesystemencoding())
    return x


DEFAULT_MODE = "wb" if PY2 else "w"


_proper_fsync = os.fsync


if sys.platform != 'win32':
    if hasattr(fcntl, 'F_FULLFSYNC'):
        def _proper_fsync(fd):
            # https://lists.apple.com/archives/darwin-dev/2005/Feb/msg00072.html
            # https://developer.apple.com/library/mac/documentation/Darwin/Reference/ManPages/man2/fsync.2.html
            # https://github.com/untitaker/python-atomicwrites/issues/6
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)

    def _sync_directory(directory):
        # Ensure that filenames are written to disk
        fd = os.open(directory, 0)
        try:
            _proper_fsync(fd)
        finally:
            os.close(fd)

    def _replace_atomic(src, dst):
        os.rename(src, dst)
        _sync_directory(os.path.normpath(os.path.dirname(dst)))

    def _move_atomic(src, dst):
        os.link(src, dst)
        os.unlink(src)

        src_dir = os.path.normpath(os.path.dirname(src))
        dst_dir = os.path.normpath(os.path.dirname(dst))
        _sync_directory(dst_dir)
        if src_dir != dst_dir:
            _sync_directory(src_dir)
else:
    from ctypes import windll, WinError

    _MOVEFILE_REPLACE_EXISTING = 0x1
    _MOVEFILE_WRITE_THROUGH = 0x8
    _windows_default_flags = _MOVEFILE_WRITE_THROUGH

    try:
        _CreateTransaction = ctypes.windll.ktmw32.CreateTransaction
        _CommitTransaction = ctypes.windll.ktmw32.CommitTransaction
        _MoveFileTransacted = ctypes.windll.kernel32.MoveFileTransactedW
        _CloseHandle = ctypes.windll.kernel32.CloseHandle
    except WindowsError:
        transactional_ntfs_supported = False

    def _handle_errors(rv):
        if not rv:
            raise WinError()
        return True

    def _rename_atomic(src, dst):
        #  Depreciation warning: transactional NTFS may be dropped in the future:
        #  https://docs.microsoft.com/en-us/windows/win32/fileio/deprecation-of-txf
        #  Probably replaceFileW should be used in the future (however, per doc, it is not atomic):
        #  https://docs.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew

        if not transactional_ntfs_supported:
            return False

        ta = _CreateTransaction(None, 0, 0, 0, 0, 1000, 'Atomic rename')
        if ta == -1:
            return False
        try:
            retry = 0
            rv = False
            while not rv and retry < 100:
                rv = _MoveFileTransacted(_path_to_unicode(src), _path_to_unicode(dst), None, None,
                                         _MOVEFILE_REPLACE_EXISTING |
                                         _MOVEFILE_WRITE_THROUGH, ta)
                if rv:
                    rv = _CommitTransaction(ta)
                    break
                else:
                    time.sleep(0.001)
                    retry += 1
            return rv
        finally:
            _CloseHandle(ta)

    def _rename(src, dst):

        if _rename_atomic(src, dst):
            return True

        # Fall back to "move"
        retry = 0
        rv = False
        while not rv and retry < 100:
            rv = windll.kernel32.MoveFileExW(
                _path_to_unicode(src), _path_to_unicode(dst),
                _windows_default_flags | _MOVEFILE_REPLACE_EXISTING
            )
            if not rv:
                time.sleep(0.001)
                retry += 1
        return rv

    def _replace_atomic(src, dst):
        # Try atomic or pseudo-atomic rename
        if _handle_errors(_rename(src, dst)):
            return

        # Fall back to "move away and replace"
        try:
            os.rename(src, dst)
        except OSError, e:
            if e.errno != errno.EEXIST:
                raise
            old = "%s-%08x" % (dst, random.randint(0, sys.maxint))
            os.rename(dst, old)
            os.rename(src, dst)
            try:
                os.unlink(old)
            except Exception:
                pass

    def _move_atomic(src, dst):
        _handle_errors(windll.kernel32.MoveFileExW(
            _path_to_unicode(src), _path_to_unicode(dst),
            _windows_default_flags
        ))


def replace_atomic(src, dst):
    '''
    Move ``src`` to ``dst``. If ``dst`` exists, it will be silently
    overwritten.

    Both paths must reside on the same filesystem for the operation to be
    atomic.
    '''
    return _replace_atomic(src, dst)


def move_atomic(src, dst):
    '''
    Move ``src`` to ``dst``. There might a timewindow where both filesystem
    entries exist. If ``dst`` already exists, :py:exc:`FileExistsError` will be
    raised.

    Both paths must reside on the same filesystem for the operation to be
    atomic.
    '''
    return _move_atomic(src, dst)


class AtomicWriter(object):
    '''
    A helper class for performing atomic writes. Usage::

        with AtomicWriter(path).open() as f:
            f.write(...)

    :param path: The destination filepath. May or may not exist.
    :param mode: The filemode for the temporary file. This defaults to `wb` in
        Python 2 and `w` in Python 3.
    :param overwrite: If set to false, an error is raised if ``path`` exists.
        Errors are only raised after the file has been written to.  Either way,
        the operation is atomic.

    If you need further control over the exact behavior, you are encouraged to
    subclass.
    '''

    def __init__(self, path, mode=DEFAULT_MODE, overwrite=False,
                 **open_kwargs):
        if 'a' in mode:
            raise ValueError(
                'Appending to an existing file is not supported, because that '
                'would involve an expensive `copy`-operation to a temporary '
                'file. Open the file in normal `w`-mode and copy explicitly '
                'if that\'s what you\'re after.'
            )
        if 'x' in mode:
            raise ValueError('Use the `overwrite`-parameter instead.')
        if 'w' not in mode:
            raise ValueError('AtomicWriters can only be written to.')

        self._path = path
        self._mode = mode
        self._overwrite = overwrite
        self._open_kwargs = open_kwargs

    def open(self):
        '''
        Open the temporary file.
        '''
        return self._open(self.get_fileobject)

    @contextlib.contextmanager
    def _open(self, get_fileobject):
        f = None  # make sure f exists even if get_fileobject() fails
        success = False
        try:
            with get_fileobject(**self._open_kwargs) as f:
                yield f
                self.sync(f)
                f.close()
                self.commit(f)
            success = True
        finally:
            if not success:
                try:
                    self.rollback(f)
                except Exception:
                    pass

    def get_fileobject(self, suffix=".atm_tmp", prefix=tempfile.template, dir=None,
                       **kwargs):
        '''Return the temporary file to use.'''
        if dir is None:
            dir = os.path.normpath(os.path.dirname(self._path))
        descriptor, name = tempfile.mkstemp(suffix=suffix, prefix=prefix,
                                            dir=dir)
        # io.open() will take either the descriptor or the name, but we need
        # the name later for commit()/replace_atomic() and couldn't find a way
        # to get the filename from the descriptor.
        os.close(descriptor)
        kwargs['mode'] = self._mode
        kwargs['file'] = name
        return io.open(**kwargs)

    def sync(self, f):
        '''responsible for clearing as many file caches as possible before
        commit'''
        f.flush()
        _proper_fsync(f.fileno())

    def commit(self, f):
        '''Move the temporary file to the target location.'''
        if self._overwrite:
            replace_atomic(f.name, self._path)
        else:
            move_atomic(f.name, self._path)

    def rollback(self, f):
        '''Clean up all temporary resources.'''
        os.unlink(f.name)


def atomic_write(path, writer_cls=AtomicWriter, **cls_kwargs):
    '''
    Simple atomic writes. This wraps :py:class:`AtomicWriter`::

        with atomic_write(path) as f:
            f.write(...)

    :param path: The target path to write to.
    :param writer_cls: The writer class to use. This parameter is useful if you
        subclassed :py:class:`AtomicWriter` to change some behavior and want to
        use that new subclass.

    Additional keyword arguments are passed to the writer class. See
    :py:class:`AtomicWriter`.
    '''
    return writer_cls(path, **cls_kwargs).open()
