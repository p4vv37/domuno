#!/usr/bin/env python3
"""Summarize payload-free gateway JSONL and STT/TTS Docker log exports."""
import argparse
from collections import Counter, defaultdict
from contextlib import nullcontext
import json
import math
import statistics
import sys
from datetime import datetime

from qwen_metrics import number

TIMINGS = ('total_ms', 'router_ms', 'audit_ms', 'capture_ms', 'budget_ms', 'queue_ms', 'reset_ms', 'wait_sound_ms',
           'backend_ms', 'ttft_ms', 'prefill_ms', 'decode_ms',
           'preprocess_ms', 'encoder_ms', 'engine_ms', 'first_pcm_ms',
           'first_text_ms', 'first_sentence_ms', 'text_stream_ms', 'sentence_to_first_pcm_ms')


def stat(values):
    values = sorted(value for value in values if number(value) is not None)
    if not values:
        return None
    return {'n': len(values), 'median': round(statistics.median(values), 3),
            'p95': round(values[math.ceil(.95 * len(values)) - 1], 3),
            'min': round(values[0], 3), 'max': round(values[-1], 3)}


def read_records(lines):
    for line in lines:
        for marker in ('stt_metric ', 'pocket_tts_metric '):
            if marker in line:
                line = line.split(marker, 1)[1]
                break
        try:
            record = json.loads(line)
        except ValueError:
            continue  # Includes an incomplete last line during live collection.
        if isinstance(record, dict) and record.get('schema_version') == 1:
            yield record


def summarize(records):
    groups, tts = defaultdict(list), {}
    for record in records:
        if record.get('service') == 'tts':
            # TTS emits bounded lifecycle events, possibly interleaved by request.
            identity = record.get('request_id')
            if not isinstance(identity, str):
                continue
            item = tts.setdefault(identity, {'service': 'tts', 'outcome': 'unfinished'})
            event = record.get('event')
            elapsed = number(record.get('elapsed_ms'))
            if event in ('first_text_chunk', 'first_sentence_ready', 'text_stream_stop'):
                item[{'first_text_chunk': 'first_text_ms', 'first_sentence_ready': 'first_sentence_ms',
                      'text_stream_stop': 'text_stream_ms'}[event]] = elapsed
            if event == 'render_start':
                item['queue_ms'] = elapsed
            elif event == 'first_pcm_send_complete':
                item['first_pcm_ms'] = elapsed
            if event in ('request_complete', 'request_failed', 'request_busy', 'request_empty') or (
                    isinstance(event, str) and event.startswith('request_cancelled')):
                outcome = ('completed' if event == 'request_complete' else
                           'cancelled' if event.startswith('request_cancelled') else
                           event.removeprefix('request_'))
                priority = {'unfinished': 0, 'completed': 1, 'empty': 2, 'busy': 2,
                            'cancelled': 3, 'failed': 4}
                if priority.get(outcome, 4) >= priority.get(item['outcome'], 0):
                    item['outcome'] = outcome
                item['total_ms'] = elapsed
        elif record.get('service') in ('gateway', 'stt'):
            key = (record['service'], record.get('backend_version', 'unknown'),
                   record.get('profile', 'default'), record.get('route', 'stt'),
                   record.get('cache_policy', 'unknown'), record.get('response_style', 'unknown'),
                   record.get('agent_language', 'unknown'))
            groups[key].append(record)
    for item in tts.values():
        if number(item.get('first_pcm_ms')) is not None and number(item.get('first_sentence_ms')) is not None:
            item['sentence_to_first_pcm_ms'] = max(0, item['first_pcm_ms'] - item['first_sentence_ms'])
    if tts:
        groups[('tts', 'unknown', 'default', 'tts', 'unknown', 'unknown', 'unknown')].extend(tts.values())
    result = []
    for (service, version, profile, route, policy, style, language), rows in sorted(groups.items()):
        completed = [row for row in rows if row.get('outcome') == 'completed']
        cache_rows = [row for row in completed
                      if number(row.get('cached_tokens')) is not None
                      and number(row.get('prompt_tokens')) is not None
                      and 0 <= row['cached_tokens'] <= row['prompt_tokens']]
        prompt = sum(row['prompt_tokens'] for row in cache_rows)
        result.append({
            'service': service, 'backend_version': version, 'profile': profile,
            'route': route, 'cache_policy': policy, 'response_style': style, 'agent_language': language, 'requests': len(rows),
            'outcomes': dict(Counter(row.get('outcome', 'unknown') for row in rows)),
            'completed_timings_ms': {key: value for key in TIMINGS
                                     if (value := stat([row.get(key) for row in completed])) is not None},
            'reported_cache': {
                'requests_with_usage': len(cache_rows),
                'hit_requests': sum(row['cached_tokens'] > 0 for row in cache_rows),
                'token_reuse_fraction': sum(row['cached_tokens'] for row in cache_rows) / prompt if prompt else None,
            },
            'tokens': {key: stat([row.get(key) for row in completed]) for key in (
                'estimated_prompt_tokens', 'prompt_tokens', 'cached_tokens',
                'evaluated_prompt_tokens', 'completion_tokens')},
        })
    return result


def pipeline_summary(trace, first_pcm_at=None, playback_at=None):
    """Summarize an exported Assist JSON trace, without exposing its payloads."""
    def timestamp(value):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('Timestamps require an explicit timezone')
        return parsed.timestamp()

    events = trace.get('events')
    if not isinstance(events, list):
        raise ValueError('Expected an Assist JSON object with an events list')
    times = {}
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get('timestamp'), str):
            continue
        kind = event.get('type')
        at = timestamp(event['timestamp'])
        times.setdefault(kind, at)
        if kind == 'intent-progress':
            delta = (event.get('data') or {}).get('chat_log_delta') or {}
            content = delta.get('content')
            if isinstance(content, str) and content.strip():
                times.setdefault('first-text', at)
            if any(call.get('tool_args') or (call.get('function') or {}).get('arguments')
                   for call in delta.get('tool_calls') or []):
                times.setdefault('first-tool-arguments', at)
    if first_pcm_at:
        times['first-pcm'] = timestamp(first_pcm_at)
    if playback_at:
        times['playback-start'] = timestamp(playback_at)
    def duration(start, end):
        if start not in times or end not in times:
            return None
        value = (times[end] - times[start]) * 1000
        if value < 0:
            raise ValueError('Observation timestamp precedes its measurement boundary')
        return round(value, 3)
    return {
        'scope': 'assist-pipeline',
        'error_events': sum(isinstance(e, dict) and e.get('type') == 'error' for e in events),
        'vad_end_to_stt_ms': duration('stt-vad-end', 'stt-end'),
        'intent_ms': duration('intent-start', 'intent-end'),
        'intent_first_text_ms': duration('intent-start', 'first-text'),
        'intent_first_tool_arguments_ms': duration('intent-start', 'first-tool-arguments'),
        'tts_url_ready_ms': duration('tts-start', 'tts-end'),
        'vad_end_to_run_end_ms': duration('stt-vad-end', 'run-end'),
        'vad_end_to_first_pcm_ms': duration('stt-vad-end', 'first-pcm'),
        'vad_end_to_playback_ms': duration('stt-vad-end', 'playback-start'),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='*', help='JSONL files or Docker log exports. Use - for stdin.')
    parser.add_argument('--pipeline', help='Exported Assist pipeline JSON trace')
    parser.add_argument('--first-pcm-at', help='Observed first PCM UTC timestamp, explicitly matched to this run')
    parser.add_argument('--playback-at', help='Observed satellite playback-start UTC timestamp for this run')
    parser.add_argument('--json', action='store_true', help='Print the complete machine-readable report')
    args = parser.parse_args(argv)
    if args.pipeline:
        if args.files:
            parser.error('Use --pipeline separately from metric log files')
        try:
            with open(args.pipeline, encoding='utf-8') as stream:
                summary = pipeline_summary(json.load(stream), args.first_pcm_at, args.playback_at)
        except (OSError, ValueError, TypeError) as exc:
            parser.error(str(exc))
        print(json.dumps(summary, indent=2, allow_nan=False))
        return 0
    if not args.files or args.first_pcm_at or args.playback_at:
        parser.error('Provide log files, or --pipeline with optional PCM/playback observations')
    records = []
    try:
        for filename in args.files:
            with (nullcontext(sys.stdin) if filename == '-' else open(filename, encoding='utf-8')) as stream:
                records.extend(read_records(stream))
    except OSError as exc:
        parser.error(str(exc))
    report = summarize(records)
    if args.json:
        print(json.dumps(report, indent=2, allow_nan=False))
    else:
        for group in report:
            print('%s / %s / %s / %s / cache=%s / style=%s / language=%s: %d requests' % (
                group['service'], group['backend_version'], group['profile'], group['route'], group['cache_policy'], group['response_style'], group['agent_language'], group['requests']))
            print('  outcomes: ' + json.dumps(group['outcomes'], sort_keys=True))
            for metric, values in group['completed_timings_ms'].items():
                print('  %-16s median=%9.3f ms  p95=%9.3f ms  n=%d' % (
                    metric, values['median'], values['p95'], values['n']))
            print('  reported_cache: ' + json.dumps(group['reported_cache']))
        if not report:
            print('No supported metrics found.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
