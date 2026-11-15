"""Persistent, explicitly constructed QNN Whisper Small engine (16 kHz mono PCM).

Importing this module does not import ORT, NumPy or a backend. One engine owns
process-wide plugin registration. No automatic CPU fallback or model reload.
"""
import gc
import importlib
from pathlib import Path
import threading
from time import perf_counter

try:
    from .qnn_whisper_profile import load_profile
except ImportError:
    from qnn_whisper_profile import load_profile

def validate(actual, expected):
    by_name = {x.name: x for x in actual}
    if set(by_name) != set(expected):
        raise ValueError(f'Unexpected IO names: missing={set(expected)-set(by_name)}, extra={set(by_name)-set(expected)}')
    for name, (shape, dtype) in expected.items():
        value = by_name[name]
        if tuple(value.shape) != shape or value.type != dtype:
            raise ValueError(f'{name}: got {value.shape}/{value.type}, expected {shape}/{dtype}')


_REGISTRY_LOCK = threading.RLock()
_OWNER = None


class QnnWhisperEngine:
    """Constructor opens NPU sessions; call close(), or use a context manager.

    transcribe accepts normalized float32 mono audio at exactly 16000 Hz.
    Clips shorter than 100 ms, effectively silent, or longer than 30 s fail.
    This engine does not resample, trim, silently truncate or change host env.
    """
    def __init__(self, model_dir, tokenizer, backend, max_tokens=128, *, model_profile='small-fp16', metadata=None):
        global _OWNER
        self._lock = threading.RLock()
        self.encoder = self.decoder = self.tokenizer = None
        self._registered = False
        self._closed = True
        self._ort = None
        if not 4 <= max_tokens <= 199:
            raise ValueError('max_tokens must be in [4,199] including prefix steps')
        self.profile = load_profile(model_profile, metadata)
        self.max_tokens = max_tokens
        root = Path(model_dir)
        paths = {}
        for name in ('encoder', 'decoder'):
            matches = list(root.rglob(name+'.onnx'))
            if len(matches) != 1:
                raise ValueError('Expected exactly one '+name+'.onnx')
            paths[name] = str(matches[0])
        if not Path(backend).is_file() or not Path(tokenizer).is_file():
            raise ValueError('Explicit backend and tokenizer files required')
        if Path(backend).name != 'libQnnHtp.so':
            raise ValueError('Only explicit libQnnHtp.so backend is permitted')
        self.backend = str(Path(backend).resolve())
        with _REGISTRY_LOCK:
            if _OWNER is not None:
                raise RuntimeError('QNN plugin already owned by another engine')
            self.np = importlib.import_module('numpy')
            self.tokenizer = importlib.import_module('tokenizers').Tokenizer.from_file(str(tokenizer))
            special = ['<|startoftranscript|>', '<|pl|>', '<|transcribe|>', '<|notimestamps|>', '<|endoftext|>']
            self.ids = [self.tokenizer.token_to_id(s) for s in special]
            self.profile.validate_tokenizer(self.tokenizer, self.ids)
            self.extractor = importlib.import_module('faster_whisper.feature_extractor').FeatureExtractor(feature_size=self.profile.mel_bins)
            self._ort = importlib.import_module('onnxruntime')
            qnn = importlib.import_module('onnxruntime_qnn')
            self._ort.register_execution_provider_library('QNNExecutionProvider', qnn.get_library_path())
            self._registered = True
            _OWNER = self
            try:
                devices = [d for d in self._ort.get_ep_devices() if d.ep_name == 'QNNExecutionProvider']
                if not devices:
                    raise RuntimeError('No QNN device; CPU fallback forbidden')
                options = self._ort.SessionOptions()
                options.intra_op_num_threads = 1
                options.inter_op_num_threads = 1
                options.add_session_config_entry('session.intra_op.allow_spinning', '0')
                options.add_session_config_entry('session.inter_op.allow_spinning', '0')
                options.add_session_config_entry('session.disable_cpu_ep_fallback', '1')
                options.add_provider_for_devices(devices, {'backend_path': self.backend})
                start = perf_counter()
                self.encoder = self._ort.InferenceSession(paths['encoder'], sess_options=options)
                self.decoder = self._ort.InferenceSession(paths['decoder'], sess_options=options)
                ei, eo, di, do = self.profile.contract()
                for actual, expected in ((self.encoder.get_inputs(), ei), (self.encoder.get_outputs(), eo), (self.decoder.get_inputs(), di), (self.decoder.get_outputs(), do)):
                    validate(actual, expected)
                self.init_seconds = perf_counter()-start
                self._closed = False
            except BaseException as exc:
                try:
                    self.close()
                except Exception as cleanup:
                    exc.add_note('QNN cleanup also failed: '+repr(cleanup))
                raise

    def transcribe(self, audio):
        with self._lock:
            if self._closed:
                raise RuntimeError('Engine is closed')
            np = self.np
            start = perf_counter()
            audio = np.asarray(audio)
            if audio.dtype != np.float32 or audio.ndim != 1:
                raise ValueError('Expected one-dimensional normalized float32 mono PCM at 16000 Hz')
            if not 1600 <= len(audio) <= 480000:
                raise ValueError('Audio must be 0.1 to 30 seconds; no silent clipping')
            if not np.isfinite(audio).all() or np.max(np.abs(audio)) > 1.0:
                raise ValueError('Audio must be finite and normalized to [-1,1]')
            if np.max(np.abs(audio)) < 1e-6:
                raise ValueError('Audio is effectively silent')
            seconds = len(audio)/16000
            features = np.asarray(self.extractor(np.pad(audio, (0,480000-len(audio))))[:, :3000], dtype=np.float32)
            if features.shape != (self.profile.mel_bins,3000) or not np.isfinite(features).all():
                raise ValueError('Invalid mel features')
            prepared = perf_counter()
            cross = dict(zip([x.name for x in self.encoder.get_outputs()], self.encoder.run(None, {'input_features': self.profile.prepare_features(np, features[None])})))
            encoded = perf_counter()
            di = self.profile.contract()[2]
            cache = self.profile.initial_cache(np)
            prefix, eot = self.ids[:4], self.ids[4]
            tokens = [prefix[0]]
            times = []
            for step in range(self.max_tokens):
                feed = {**cross, **cache, 'input_ids':self.profile.ids(np, 'input_ids', [[tokens[-1]]]), 'position_ids':self.profile.ids(np, 'position_ids', [step]), 'attention_mask':self.profile.mask(np,step)}
                then = perf_counter()
                out = dict(zip([x.name for x in self.decoder.get_outputs()],self.decoder.run(None,feed)))
                times.append(perf_counter()-then)
                if not np.isfinite(out['logits']).all():
                    raise RuntimeError('Nonfinite decoder logits')
                cache = {name:out[name.removesuffix('_in')+'_out'] for name in cache}
                token = prefix[step+1] if step < 3 else int(np.argmax(out['logits'][0,:,0,0]))
                tokens.append(token)
                if token == eot:
                    break
            if tokens[-1] != eot:
                raise RuntimeError('Decoder reached token limit without EOT; transcript rejected')
            text = self.tokenizer.decode(tokens,skip_special_tokens=True).strip()
            if not text:
                raise RuntimeError('Empty transcript')
            return {'text':text,'tokens':tokens,'eot_reached':True,'truncated':False,'status':'EOT','audio_s':seconds,'timings_seconds':{'preprocess':prepared-start,'encoder':encoded-prepared,'decoder_steps':times,'decoder_total':sum(times),'total':perf_counter()-start,'session_init':self.init_seconds}}

    def close(self):
        global _OWNER
        with self._lock, _REGISTRY_LOCK:
            self._closed = True
            self.decoder = None
            self.encoder = None
            gc.collect()
            if self._registered:
                # If unregister fails retain ownership and allow an explicit retry;
                # never pretend that another engine may safely re-register.
                self._ort.unregister_execution_provider_library('QNNExecutionProvider')
                self._registered = False
                if _OWNER is self:
                    _OWNER = None
            self.tokenizer = None

    def __enter__(self):
        if self._closed:
            raise RuntimeError('Engine is closed')
        return self

    def __exit__(self, *exc):
        self.close()
