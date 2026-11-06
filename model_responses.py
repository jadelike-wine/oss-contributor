"""Narrow syntax normalization for completed, auditable research responses."""
import hashlib
import json
import math


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON key')
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError('Non-finite JSON number')


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Non-finite JSON number')
    return number


def _strict_json(text):
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant,
                      parse_float=_finite_float)


def _only_root_is_open(text):
    stack, quoted, escaped = [], False, False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in '{[':
            stack.append(character)
        elif character in '}]':
            if not stack or stack.pop() != ('{' if character == '}' else '['):
                return False
    return not quoted and stack == ['{']


def recover_research_json(stdout, purpose, returncode):
    """Return an answer and receipt only for one missing final root-object brace.

    This never fills fields, changes values, or grants research/review approval.
    Callers still run the original schema, exact evidence and policy validation.
    A complete successful no-tool stream must independently reproduce the result
    text. Partial streams, ambiguous JSON and all other syntax errors fail closed.
    """
    if purpose not in ('research', 'research-correction') or returncode != 0:
        return None
    try:
        events, banners = [], []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            # The existing explicit empty-toolset invocation emits this banner
            # before JSONL. No other warning or malformed stream is accepted.
            if line == 'Warning: Unknown toolsets: none' and not events and not banners:
                banners.append(line)
                continue
            events.append(_strict_json(line))
        if (not events or any(not isinstance(event, dict) for event in events)
                or any(event.get('type') not in ('system', 'text', 'result') for event in events)):
            return None
        results = [event for event in events if event.get('type') == 'result']
        if len(results) != 1 or events[-1] is not results[0]:
            return None
        result = results[0]
        if type(result.get('exit_code')) is not int or result['exit_code'] != 0:
            return None
        raw = result.get('text')
        fragments = [event.get('text') for event in events if event.get('type') == 'text']
        if (not isinstance(raw, str) or not fragments
                or any(not isinstance(fragment, str) for fragment in fragments)
                or ''.join(fragments) != raw):
            return None
        text = raw.strip()
        if not text.startswith('{') or not _only_root_is_open(text):
            return None
        try:
            _strict_json(text)
        except json.JSONDecodeError as error:
            if error.msg != "Expecting ',' delimiter" or error.pos != len(text):
                return None
        else:
            return None
        normalized = text + '}'
        answer = _strict_json(normalized)
        if not isinstance(answer, dict):
            return None
    except (ValueError, TypeError):
        return None
    return answer, {
        'normalization': 'append_missing_root_object_brace',
        'purpose': purpose,
        'raw_text_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
        'normalized_text_sha256': hashlib.sha256(normalized.encode('utf-8')).hexdigest(),
        'stream_sha256': hashlib.sha256(stdout.encode('utf-8')).hexdigest(),
        'raw_text_characters': len(raw),
        'appended_text': '}',
        'stream_result_agreement': True,
        'ignored_cli_banners': banners,
        'schema_validation': 'pending',
        'evidence_validation': 'pending',
        'approval': 'not_granted',
    }
