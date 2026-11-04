"""Cross-process maintenance leases and durable, inspectable job records."""
import contextlib, fcntl, json, os, time, uuid
from pathlib import Path

BASE = Path(os.environ.get('RPG_STATE_DIR', Path(__file__).parent))

@contextlib.contextmanager
def lease(name='maintenance', blocking=False):
    BASE.mkdir(parents=True, exist_ok=True)
    path = BASE / (name + '.lock')
    inherited = os.environ.get('RPG_LEASE_FD') if name == 'maintenance' else None
    if inherited:
        try:
            fd = int(inherited)
            if (os.fstat(fd).st_dev,os.fstat(fd).st_ino) == (path.stat().st_dev,path.stat().st_ino):
                yield fd
                return
        except (ValueError, OSError):
            pass
    with path.open('a+') as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise RuntimeError('Another library maintenance job is running. Try again after it finishes.')
        try:
            yield fh.fileno()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

def record(kind, **fields):
    path = BASE / ('job-' + kind + '.json')
    state = read(kind)
    state.update(fields, updated=time.time())
    temp = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(state))
    os.replace(temp, path)
    return state

def read(kind):
    try:
        return json.loads((BASE / ('job-' + kind + '.json')).read_text())
    except (OSError, ValueError):
        return {}

def busy():
    try:
        with lease():
            return False
    except RuntimeError:
        return True

def run(kind, callback):
    with lease() as fd:
        record(kind, running=True, started=time.time(), finished=None, error=None, pid=os.getpid())
        try:
            result = callback(fd)
            record(kind, running=False, finished=time.time(), returncode=result or 0)
            return result
        except Exception as exc:
            record(kind, running=False, finished=time.time(), returncode=1, error=str(exc))
            raise

def child_options(fd):
    return dict(env={**os.environ, 'RPG_LEASE_FD':str(fd)}, pass_fds=(fd,))
