"""Fail-closed graph contract for the pinned Whisper Large-v3-Turbo W8A16 artifact."""
import json
import math
from pathlib import Path

MODEL_ID = 'whisper_large_v3_turbo_quantized'
MEL_BINS = 128
VOCAB_SIZE = 51866
LAYERS = 4
HEADS = 20
SPECIAL_IDS = (50258, 50269, 50360, 50364, 50257)
SCOPE = ('w8a16', 'precompiled_qnn_onnx',
         {'qairt': '2.50.0.260828221209', 'onnx_runtime': '1.30.0'},
         'qualcomm-qcs8275')


class Profile:
    def __init__(self, tensors):
        self.tensors = tensors
        self.mel_bins = MEL_BINS
        self.vocab_size = VOCAB_SIZE
        self.special_ids = SPECIAL_IDS

    def validate_tokenizer(self, tokenizer, ids):
        if tuple(ids) != self.special_ids:
            raise ValueError('Invalid multilingual tokenizer special IDs for profile')
        if any(i is None or not 0 <= i < self.vocab_size for i in ids):
            raise ValueError('Invalid multilingual Whisper tokenizer')
        if (tokenizer.get_vocab_size() != VOCAB_SIZE
                or tokenizer.token_to_id('<|yue|>') != 50358):
            raise ValueError('Turbo requires matching full multilingual v3 tokenizer')

    def prepare_features(self, np, features):
        return quantize(np, features, self.tensors[0]['input_features'])

    def initial_cache(self, np):
        return {name: np.full(shape,
                              self.tensors[2][name]['quantization_parameters']['zero_point'],
                              dtype=dtype[7:-1])
                for name, (shape, dtype) in self.contract()[2].items() if '_self_' in name}

    def mask(self, np, step):
        if not 0 <= step < 199:
            raise ValueError('Decoder position outside rolling-cache contract')
        value = np.full((1, 1, 1, 200), -100.0, dtype=np.float32)
        value[..., 199 - step:] = 0
        return quantize(np, value, self.tensors[2]['attention_mask'])

    def ids(self, np, name, values):
        return np.asarray(values, dtype=self.tensors[2][name]['dtype'])

    def contract(self):
        return tuple({n: (tuple(t['shape']), 'tensor(' + t['dtype'] + ')')
                      for n, t in group.items()} for group in self.tensors)


def quantize(np, values, tensor):
    """Per-tensor affine quantization, nearest-even rounding then saturation."""
    values = np.asarray(values, dtype=np.float64)
    if not np.isfinite(values).all():
        raise ValueError('Nonfinite quantization input')
    q = tensor['quantization_parameters']
    if (tensor['dtype'] not in ('uint8', 'uint16') or type(q['scale']) not in (int, float)
            or not math.isfinite(q['scale']) or q['scale'] <= 0
            or type(q['zero_point']) is not int):
        raise ValueError('Expected positive per-tensor affine quantization')
    limit = np.iinfo(tensor['dtype'])
    if not limit.min <= q['zero_point'] <= limit.max:
        raise ValueError('Quantization zero point outside dtype range')
    with np.errstate(over='ignore'):
        values = np.rint(values / q['scale']) + q['zero_point']
    return np.clip(values, limit.min, limit.max).astype(tensor['dtype'])


def load_profile(metadata):
    """Validate the pinned Turbo artifact metadata and return its graph contract."""
    if metadata is None:
        raise ValueError('Artifact metadata required')
    if not isinstance(metadata, dict):
        metadata = json.loads(Path(metadata).read_text())
    # Copy so callers cannot change a validated contract underneath the engine.
    d = json.loads(json.dumps(metadata))
    try:
        if (d['model_id'], d['precision'], d['runtime'], d['tool_versions'],
                d['chipset_attributes']['name']) != (MODEL_ID, *SCOPE):
            raise ValueError('Unsupported artifact scope')
        files = d['model_files']
        if set(files) != {'encoder.onnx', 'decoder.onnx'}:
            raise ValueError('Unexpected model files')
        groups = tuple(files[f][io] for f, io in
                       [('encoder.onnx', 'inputs'), ('encoder.onnx', 'outputs'),
                        ('decoder.onnx', 'inputs'), ('decoder.onnx', 'outputs')])
        cross, cache_in, cache_out = {}, {}, {}
        for i in range(LAYERS):
            for kind in ('k', 'v'):
                cross[f'{kind}_cache_cross_{i}'] = (
                    (HEADS, 1, 64, 1500) if kind == 'k' else (HEADS, 1, 1500, 64), 'uint8')
                shape = (HEADS, 1, 64, 199) if kind == 'k' else (HEADS, 1, 199, 64)
                cache_in[f'{kind}_cache_self_{i}_in'] = (shape, 'uint8')
                cache_out[f'{kind}_cache_self_{i}_out'] = (shape, 'uint8')
        expected = (
            {'input_features': ((1, MEL_BINS, 3000), 'uint16')},
            cross,
            {'input_ids': ((1, 1), 'int32'), 'position_ids': ((1,), 'int32'),
             'attention_mask': ((1, 1, 1, 200), 'uint16'), **cache_in, **cross},
            {'logits': ((1, VOCAB_SIZE, 1, 1), 'uint16'), **cache_out},
        )
        for actual, exp in zip(groups, expected):
            if set(actual) != set(exp):
                raise ValueError('Unexpected tensor names')
            for name, (shape, dtype) in exp.items():
                t = actual[name]
                if tuple(t['shape']) != shape or t['dtype'] != dtype:
                    raise ValueError('Invalid tensor contract: ' + name)
                if dtype.startswith('uint'):
                    q = t['quantization_parameters']
                    scale, zero = q['scale'], q['zero_point']
                    if (set(q) != {'scale', 'zero_point'} or type(scale) not in (float, int)
                            or not math.isfinite(scale) or scale <= 0
                            or type(zero) is not int
                            or not 0 <= zero <= (255 if dtype == 'uint8' else 65535)):
                        raise ValueError('Invalid per-tensor quantization: ' + name)
                elif 'quantization_parameters' in t:
                    raise ValueError('Positional/token IDs must be unquantized')
        for name in cross:
            if groups[1][name] != groups[2][name]:
                raise ValueError('Cross cache quantization mismatch: ' + name)
        for name in cache_in:
            if groups[2][name] != groups[3][name.removesuffix('_in') + '_out']:
                raise ValueError('Self cache quantization mismatch: ' + name)
        return Profile(groups)
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError('Invalid artifact metadata schema') from exc
