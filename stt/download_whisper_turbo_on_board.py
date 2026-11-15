#!/usr/bin/env python3
"""One-shot board-only pinned Turbo download. No inference or automatic retries."""
from pathlib import Path, PurePosixPath
import hashlib
import argparse
import json
import os
import shutil
import stat
import time
import urllib.request
import zipfile

PREFIX = 'whisper_large_v3_turbo_quantized-precompiled_qnn_onnx-w8a16-qualcomm_qcs8275'
URL = 'https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/whisper_large_v3_turbo_quantized/releases/v0.66.0/' + PREFIX + '.zip'
ZIP_BYTES = 823711823
RAW_BYTES = 981982899
METADATA_SHA = 'b82144de1f75651d136f0b6fedf18c75a12ad9709af7999ee8bee427b58999eb'
TOKENIZER_URL = 'https://huggingface.co/openai/whisper-large-v3-turbo/resolve/41f01f3fe87f28c78e2fbf8b568835947dd65ed9/tokenizer.json'
TOKENIZER_SHA = '297b13372ac43916285644fb9687add3cc62ee2a1adb60da3dc25cc94c1871fd'
EXPECTED = {'encoder.bin':686092040, 'decoder.bin':295856032,
            'encoder.onnx':4644, 'decoder.onnx':13849, 'metadata.json':16334}


def fetch(url, dest, expected):
    digest = hashlib.sha256(); count = 0
    with urllib.request.urlopen(url, timeout=120) as response, dest.open('xb') as out:
        os.chmod(dest, 0o600)
        assert response.status == 200
        if response.headers.get('Content-Length'):
            assert int(response.headers['Content-Length']) == expected
        while True:
            block = response.read(1024 * 1024)
            if not block: break
            count += len(block); assert count <= expected
            out.write(block); digest.update(block)
            if count // (128*1024**2) != (count-len(block)) // (128*1024**2):
                print(json.dumps({'downloaded':count,'expected':expected}),flush=True)
    assert count == expected
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New model/tokenizer directory')
    parser.add_argument('--staging', type=Path, required=True, help='New temporary directory, preferably on tmpfs')
    args = parser.parse_args()
    ROOT, STAGING = args.output.absolute(), args.staging.absolute()
    assert ROOT.resolve() == ROOT and STAGING.resolve() == STAGING
    assert not ROOT.exists() and not STAGING.exists(), 'Fresh paths required, no retry'
    assert shutil.disk_usage(ROOT.parent).free >= RAW_BYTES + 1024**3
    assert shutil.disk_usage(STAGING.parent).free >= ZIP_BYTES + 256*1024**2
    available = next(int(l.split()[1])*1024 for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:'))
    assert available >= ZIP_BYTES + 3*1024**3
    ROOT.mkdir(mode=0o700); STAGING.mkdir(mode=0o700)
    archive = STAGING/'turbo-w8a16.zip'; began = time.monotonic()
    archive_sha = fetch(URL, archive, ZIP_BYTES)
    model = ROOT/'model'; model.mkdir(mode=0o700)
    checksums = {}
    with zipfile.ZipFile(archive) as source:
        infos = source.infolist()
        assert len(infos) == 6 and sum(i.file_size for i in infos) == RAW_BYTES
        assert {PurePosixPath(i.filename).name:i.file_size for i in infos if not i.is_dir()} == EXPECTED
        for info in infos:
            rel = PurePosixPath(info.filename)
            assert not rel.is_absolute() and '..' not in rel.parts and rel.parts[0] == PREFIX
            assert not stat.S_ISLNK(info.external_attr >> 16)
            dest = model.joinpath(*rel.parts)
            assert dest.resolve().is_relative_to(model.resolve())
            if info.is_dir(): dest.mkdir(mode=0o700); continue
            assert shutil.disk_usage(ROOT).free >= info.file_size + 1024**3
            digest = hashlib.sha256(); count = 0
            with source.open(info) as inp, dest.open('xb') as out:
                os.chmod(dest, 0o600)
                while True:
                    chunk = inp.read(1024*1024)
                    if not chunk: break
                    count += len(chunk); assert count <= info.file_size
                    out.write(chunk); digest.update(chunk)
            assert count == info.file_size
            checksums[rel.name] = digest.hexdigest()
            print(json.dumps({'extracted':rel.name,'bytes':count,'crc_verified':True}),flush=True)
    assert checksums['metadata.json'] == METADATA_SHA
    tokenizer_sha = fetch(TOKENIZER_URL, ROOT/'tokenizer.json', 2710337)
    assert tokenizer_sha == TOKENIZER_SHA
    receipt = {'source':URL,'archive_bytes':ZIP_BYTES,'raw_bytes':RAW_BYTES,
               'archive_sha256_local':archive_sha,'publisher_archive_sha256_verified':False,
               'files_sha256_local':checksums,'tokenizer_source':TOKENIZER_URL,
               'tokenizer_sha256':tokenizer_sha,'elapsed_seconds':time.monotonic()-began}
    with (ROOT/'download_receipt.json').open('x') as out: json.dump(receipt,out,indent=2)
    os.chmod(ROOT/'download_receipt.json',0o600)
    assert archive.resolve() == STAGING/'turbo-w8a16.zip'
    assert archive.is_file() and not archive.is_symlink() and archive.stat().st_size == ZIP_BYTES
    archive.unlink()  # Only our checked, freshly downloaded staging archive.
    assert STAGING.resolve() == STAGING
    STAGING.rmdir()  # Empty staging directory only, never old model data.
    print(json.dumps({'complete':True,**receipt}),flush=True)


if __name__ == '__main__': main()
