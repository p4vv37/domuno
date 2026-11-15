"""Fail-closed graph contracts. Metadata is a contract, not executable model code."""
import json
import math
from pathlib import Path


def _small_contract():
    cross, cache_in, cache_out = {}, {}, {}
    for i in range(12):
        for kind in ('k', 'v'):
            cross[f'{kind}_cache_cross_{i}'] = ((12, 1, 64, 1500) if kind == 'k' else (12, 1, 1500, 64), 'tensor(float16)')
            shape = (12, 1, 64, 199) if kind == 'k' else (12, 1, 199, 64)
            cache_in[f'{kind}_cache_self_{i}_in'] = (shape, 'tensor(float16)')
            cache_out[f'{kind}_cache_self_{i}_out'] = (shape, 'tensor(float16)')
    enc = {'input_features': ((1, 80, 3000), 'tensor(float16)')}
    dec = {'input_ids': ((1, 1), 'tensor(int32)'), 'position_ids': ((1,), 'tensor(int32)'),
           'attention_mask': ((1, 1, 1, 200), 'tensor(float16)'), **cache_in, **cross}
    return enc, cross, dec, {'logits': ((1, 51865, 1, 1), 'tensor(float16)'), **cache_out}



class Profile:
    def __init__(self, tensors=None, *, mel_bins=80, vocab_size=51865, special_ids=(50258,50269,50359,50363,50257)):
        self.tensors = tensors
        self.mel_bins = mel_bins
        self.vocab_size = vocab_size
        self.special_ids = special_ids

    def validate_tokenizer(self, tokenizer, ids):
        if self.tensors and tuple(ids) != self.special_ids:
            raise ValueError('Invalid multilingual tokenizer special IDs for profile')
        if any(i is None or not 0 <= i < self.vocab_size for i in ids):
            raise ValueError('Invalid multilingual Whisper tokenizer')
        if self.mel_bins == 128 and (tokenizer.get_vocab_size() != 51866 or tokenizer.token_to_id('<|yue|>') != 50358):
            raise ValueError('Turbo requires matching full multilingual v3 tokenizer')

    def prepare_features(self, np, features):
        return quantize(np, features, self.tensors[0]['input_features']) if self.tensors else np.asarray(features, dtype=np.float16)

    def initial_cache(self, np):
        return {n: np.full(shape, self.tensors[2][n]['quantization_parameters']['zero_point'] if self.tensors else 0,
                           dtype=dtype[7:-1]) for n,(shape,dtype) in self.contract()[2].items() if '_self_' in n}

    def mask(self, np, step):
        if not 0 <= step < 199: raise ValueError('Decoder position outside rolling-cache contract')
        value = np.full((1,1,1,200), -100.0, dtype=np.float32)
        value[...,199-step:] = 0
        return quantize(np,value,self.tensors[2]['attention_mask']) if self.tensors else value.astype(np.float16)

    def ids(self, np, name, values):
        return np.asarray(values, dtype=self.tensors[2][name]['dtype'] if self.tensors else np.int32)

    def contract(self):
        if self.tensors is not None:
            return tuple({n: (tuple(t['shape']), 'tensor('+t['dtype']+')') for n,t in group.items()} for group in self.tensors)
        return _small_contract()


def quantize(np, values, tensor):
    """Per-tensor affine quantization, nearest-even rounding then saturation."""
    values = np.asarray(values, dtype=np.float64)
    if not np.isfinite(values).all(): raise ValueError('Nonfinite quantization input')
    q = tensor['quantization_parameters']
    if (tensor['dtype'] not in ('uint8','uint16') or type(q['scale']) not in (int,float)
            or not math.isfinite(q['scale']) or q['scale'] <= 0
            or type(q['zero_point']) is not int):
        raise ValueError('Expected positive per-tensor affine quantization')
    limit = np.iinfo(tensor['dtype'])
    if not limit.min <= q['zero_point'] <= limit.max:
        raise ValueError('Quantization zero point outside dtype range')
    with np.errstate(over='ignore'):
        values = np.rint(values / q['scale']) + q['zero_point']
    return np.clip(values, limit.min, limit.max).astype(tensor['dtype'])


def load_profile(name='small-fp16', metadata=None):
    if name == 'small-fp16' and metadata is None:
        return Profile()
    scopes = {
        'medium-w8a16': ('whisper_medium_quantized', 80, 51865, 24, 16, (50258,50269,50359,50363,50257)),
        'large-v3-turbo-w8a16': ('whisper_large_v3_turbo_quantized', 128, 51866, 4, 20, (50258,50269,50360,50364,50257)),
    }
    if name not in scopes or metadata is None:
        raise ValueError('Unsupported profile or missing artifact metadata')
    model_id, mel_bins, vocab_size, layers, heads, special_ids = scopes[name]
    if not isinstance(metadata, dict):
        metadata = json.loads(Path(metadata).read_text())
    # Copy so callers cannot change a validated contract underneath the engine.
    d = json.loads(json.dumps(metadata))
    try:
        if (d['model_id'],d['precision'],d['runtime'],d['tool_versions'],d['chipset_attributes']['name']) != (
            model_id,'w8a16','precompiled_qnn_onnx',
            {'qairt':'2.50.0.260828221209','onnx_runtime':'1.30.0'},'qualcomm-qcs8275'):
            raise ValueError('Unsupported artifact scope')
        files = d['model_files']
        if set(files) != {'encoder.onnx','decoder.onnx'}:
            raise ValueError('Unexpected model files')
        groups = tuple(files[f][io] for f,io in [('encoder.onnx','inputs'),('encoder.onnx','outputs'),('decoder.onnx','inputs'),('decoder.onnx','outputs')])
        cross, ci, co = {}, {}, {}
        for i in range(layers):
            for k in ('k','v'):
                cross[f'{k}_cache_cross_{i}'] = ((heads,1,64,1500) if k == 'k' else (heads,1,1500,64), 'uint8')
                shape = (heads,1,64,199) if k == 'k' else (heads,1,199,64)
                ci[f'{k}_cache_self_{i}_in'] = (shape,'uint8')
                co[f'{k}_cache_self_{i}_out'] = (shape,'uint8')
        expected = ({'input_features':((1,mel_bins,3000),'uint16')},cross,
                    {'input_ids':((1,1),'int32'),'position_ids':((1,),'int32'), 'attention_mask':((1,1,1,200),'uint16'),**ci,**cross},
                    {'logits':((1,vocab_size,1,1),'uint16'),**co})
        for actual, exp in zip(groups,expected):
            if set(actual) != set(exp): raise ValueError('Unexpected tensor names')
            for n,(shape,dtype) in exp.items():
                t = actual[n]
                if tuple(t['shape']) != shape or t['dtype'] != dtype: raise ValueError('Invalid tensor contract: '+n)
                if dtype.startswith('uint'):
                    q = t['quantization_parameters']; s,z = q['scale'],q['zero_point']
                    if set(q) != {'scale','zero_point'} or type(s) not in (float,int) or not math.isfinite(s) or s <= 0 or type(z) is not int or not 0 <= z <= (255 if dtype == 'uint8' else 65535):
                        raise ValueError('Invalid per-tensor quantization: '+n)
                elif 'quantization_parameters' in t:
                    raise ValueError('Positional/token IDs must be unquantized')
        for n in cross:
            if groups[1][n] != groups[2][n]: raise ValueError('Cross cache quantization mismatch: '+n)
        for n in ci:
            if groups[2][n] != groups[3][n.removesuffix('_in')+'_out']: raise ValueError('Self cache quantization mismatch: '+n)
        return Profile(groups, mel_bins=mel_bins, vocab_size=vocab_size, special_ids=special_ids)
    except (KeyError,TypeError,OverflowError) as exc:
        raise ValueError('Invalid artifact metadata schema') from exc
