import json
from pathlib import Path
import socketserver
import sys
import tempfile
import threading
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from search_cue import CachedCue, PHRASES


class SearchCueTests(unittest.TestCase):
    def test_wyoming_generates_localized_wav_once(self):
        requests = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                requests.append(json.loads(self.rfile.readline()))
                pcm = bytes(1600)
                data = {'channels': 1, 'width': 2, 'rate': 8000}
                for kind, metadata, payload in [('audio-start', data, b''), ('audio-chunk', data, pcm),
                                                 ('audio-stop', {}, b'')]:
                    encoded = json.dumps(metadata).encode()
                    self.wfile.write(json.dumps({'type': kind, 'data_length': len(encoded),
                                                 'payload_length': len(payload)}).encode() + b'\n' + encoded + payload)

        with tempfile.TemporaryDirectory() as tmp, socketserver.TCPServer(('127.0.0.1', 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            worker.start()
            self.addCleanup(worker.join, 2)
            try:
                ready = threading.Event()
                def player(*, cue_file):
                    ready.set()
                spec = (*server.server_address, *PHRASES['pl'])
                for _ in range(2):
                    ready.clear()
                    cue = CachedCue(Path(tmp), spec, player=player)
                    try:
                        self.assertTrue(ready.wait(3))
                        with wave.open(str(cue.path)) as audio:
                            self.assertEqual(audio.getnframes(), 800)
                    finally:
                        cue.close()
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]['data']['text'], 'Sprawdzam w internecie.')
                self.assertEqual(requests[0]['data']['voice']['name'], 'shefowl')
            finally:
                server.shutdown()
