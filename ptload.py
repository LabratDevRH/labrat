"""Read a torch.save() checkpoint without importing torch, and run the policy in numpy.

Checkpoints are zip files: <root>/data.pkl plus raw tensor storages in <root>/data/<key>.
Replay only needs numpy + mujoco this way (and it dodges loading torch's CUDA DLLs).
"""
import pickle, zipfile
import numpy as np

_DT = {'FloatStorage': np.float32, 'DoubleStorage': np.float64, 'LongStorage': np.int64,
       'IntStorage': np.int32, 'HalfStorage': np.float16, 'BoolStorage': np.bool_}


class _Storage:
    def __init__(self, name):
        self.name = name


def load(path):
    z = zipfile.ZipFile(path)
    pkl = next(n for n in z.namelist() if n.endswith('data.pkl'))
    root = pkl[:-len('data.pkl')]
    cache = {}

    def storage(key, dtype):
        if key not in cache:
            cache[key] = np.frombuffer(z.read(f'{root}data/{key}'), dtype=dtype)
        return cache[key]

    def rebuild_tensor(st, offset, size, stride, *rest):
        arr = st
        if not size:
            return arr[offset].copy()
        item = arr.itemsize
        return np.lib.stride_tricks.as_strided(arr[offset:], shape=tuple(size),
                                               strides=tuple(s * item for s in stride)).copy()

    def rebuild_parameter(data, requires_grad, backward_hooks, *rest):
        return data

    class U(pickle.Unpickler):
        def find_class(self, mod, name):
            if name == '_rebuild_tensor_v2':
                return rebuild_tensor
            if name == '_rebuild_parameter':
                return rebuild_parameter
            if mod == 'torch' and name in _DT:
                return name
            if mod == 'collections' and name == 'OrderedDict':
                import collections
                return collections.OrderedDict
            if mod.startswith('numpy') or (mod, name) == ('_codecs', 'encode'):
                return super().find_class(mod, name)
            raise pickle.UnpicklingError(f'refusing {mod}.{name}')

        def persistent_load(self, pid):
            _, kind, key, _loc, _n = pid
            return storage(key, _DT[kind if isinstance(kind, str) else kind])

    return U(z.open(pkl)).load()


def widen(ck, obs_dim):
    """Pad a checkpoint trained on a shorter observation (new features appended at the end):
    zero input weights for the new columns and identity normalisation, so behaviour is unchanged."""
    old = len(ck['mean'])
    if old == obs_dim:
        return ck
    assert old < obs_dim, (old, obs_dim)
    pad = obs_dim - old
    for k in ('pi.0.weight', 'v.0.weight'):
        w = ck['net'][k]
        ck['net'][k] = np.concatenate([w, np.zeros((w.shape[0], pad), w.dtype)], 1)
    ck['mean'] = np.concatenate([ck['mean'], np.zeros(pad)])
    ck['var'] = np.concatenate([ck['var'], np.ones(pad)])
    return ck


class NumpyPolicy:
    """Mean action of train.Policy: Linear-ELU x3 -> Linear."""

    def __init__(self, ck, obs_dim=None):
        if obs_dim is not None:
            ck = widen(ck, obs_dim)
        sd = ck['net']
        self.layers = [(sd[f'pi.{i}.weight'].astype(np.float32), sd[f'pi.{i}.bias'].astype(np.float32)) for i in (0, 2, 4, 6)]
        self.std = np.exp(sd['log_std'].astype(np.float32))
        self.mean = np.asarray(ck['mean']); self.sd = np.sqrt(np.asarray(ck['var']) + 1e-8)

    def __call__(self, obs):
        x = np.clip((obs - self.mean) / self.sd, -10, 10).astype(np.float32)
        for i, (w, b) in enumerate(self.layers):
            x = x @ w.T + b
            if i < 3:
                x = np.where(x > 0, x, np.expm1(np.minimum(x, 0)))
        return x
