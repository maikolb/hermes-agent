"""Bounded, loopback-only Laya SystemOne service, separate from Hermes runtime."""
from __future__ import annotations

import argparse
import json
import resource
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SOURCE_REVISION = '42626c348753fbb17572a813127df2278a1ec527'
MODEL_REVISION = '052592a15d198d9ad47da779604259b10b47b7aa'
MODEL = 'convaiinnovations/laya-multilingual'


def validate_context(agent, state, questions):
    """Reject every truncation performed by the pinned SDK, before inference."""
    from laya.common import render_options, serialize_state
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 32:
        raise ValueError('unsupported_question')
    tok = agent.tok
    count = lambda text: len(tok(text, add_special_tokens=False)['input_ids'])
    state_text = serialize_state(state)
    # The upstream SDK replaces this token; never call that a full-context review.
    if tok.mask_token in state_text:
        raise ValueError('inconclusive')
    lengths = {}
    for qid, definition in questions.items():
        if not isinstance(definition, dict) or definition.get('type') not in {'choice', 'score', 'noul'}:
            raise ValueError('unsupported_question')
        if not isinstance(definition.get('instructions'), str):
            raise ValueError('unsupported_question')
        q = agent._to_internal(definition)
        opts = render_options(q)
        if not 2 <= len(opts) <= 16:
            raise ValueError('unsupported_question')
        if tok.mask_token in q['ins'] or any(tok.mask_token in o for o in opts):
            raise ValueError('inconclusive')
        option_lengths = [count(' ' + o) for o in opts]
        if any(n > 48 for n in option_lengths):
            raise ValueError('context_too_large')
        option_total = sum(n + 1 for n in option_lengths)
        head_budget = agent.cfg['head_max_len'] - option_total
        head_length = count('%s question: %s' % (q['t'], q['ins']))
        if head_budget < 16 or head_length > max(8, head_budget):
            raise ValueError('context_too_large')
        total = 4 + head_length + option_total + count(state_text)
        if total > agent.cfg['max_len']:
            raise ValueError('context_too_large')
        lengths[qid] = total
    return lengths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-dir', required=True)
    parser.add_argument('--port', type=int, default=18991)
    parser.add_argument('--threads', type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.threads <= 2:
        parser.error('threads must be 1 or 2')
    import torch
    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    from laya import Agent
    started = time.monotonic()
    agent = Agent(args.model_dir, device='cpu')
    # Keep the checkpoint's trained context limit, not the encoder's larger ceiling.
    assert agent.cfg['max_len'] == 1024
    load_seconds = time.monotonic() - started
    warm_questions = {'urgencia': {'type': 'choice', 'instructions': 'Qual a urgência?',
        'criteria': {'normal': 'sem prazo', 'urgente': 'serviço indisponível'}}}
    validate_context(agent, 'O serviço está indisponível para todos os usuários.', warm_questions)
    warm_started = time.monotonic()
    agent.system_one('O serviço está indisponível para todos os usuários.', warm_questions)
    warm_seconds = time.monotonic() - warm_started
    identity = {'engine': 'laya', 'model': MODEL, 'model_revision': MODEL_REVISION,
                'source_revision': SOURCE_REVISION, 'device': 'cpu', 'threads': args.threads,
                'max_len': agent.cfg['max_len'], 'head_max_len': agent.cfg['head_max_len'],
                'encoder_max_positions': agent.model.encoder.config.max_position_embeddings,
                'load_seconds': round(load_seconds, 4), 'cold_inference_seconds': round(warm_seconds, 4)}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Request state can contain customer data; no access/body logging.

        def reply(self, status, result):
            data = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path != '/health':
                return self.reply(404, {'reason': 'not_found'})
            return self.reply(200, dict(identity, ready=True,
                peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))

        def do_POST(self):
            if self.path != '/systemone':
                return self.reply(404, {'reason': 'not_found'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 48000:
                    return self.reply(422, {'reason': 'context_too_large'})
                self.connection.settimeout(5)
                request = json.loads(self.rfile.read(size))
                if request.get('model') != MODEL:
                    return self.reply(422, {'reason': 'unsupported_model'})
                state, questions = request['state'], request['questions']
                lengths = validate_context(agent, state, questions)
                began = time.monotonic()
                answers, tokens = {}, 0
                # Sequential question batches bound memory even for many criteria.
                for qid, definition in questions.items():
                    result = agent.system_one(state, {qid: definition})
                    answers.update(result['answers'])
                    tokens += result['usage']['input_tokens']
                result = dict(identity, answers=answers, usage={'input_tokens': tokens, 'output_tokens': 0},
                    latency_ms=round((time.monotonic() - began) * 1000, 3), token_lengths=lengths,
                    peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
                self.reply(200, result)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                reason = str(exc) if str(exc) in {'context_too_large', 'unsupported_question', 'inconclusive'} else 'unsupported_question'
                self.reply(422, {'reason': reason, **identity})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except Exception:
                self.reply(503, {'reason': 'inference_failed', **identity})

    print(json.dumps(dict(identity, ready=True)), flush=True)
    HTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
