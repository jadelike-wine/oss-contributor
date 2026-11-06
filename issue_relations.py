"""Exact GitHub identities and conservative scan-time PR relation hints.

A reference is not proof of implementation coverage. Hints admit normal scope
and duplicate review; they never authorize a contribution or publication.
"""
import re
from urllib.parse import urlsplit

from scheduling import canonical_repo


def github_identity(url, kind='issues'):
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or parsed.netloc.casefold() != 'github.com':
        return None
    match = re.fullmatch(r'/([\w.-]+/[\w.-]+)/' + re.escape(kind) + r'/([1-9]\d*)/?', parsed.path)
    return (canonical_repo(match[1]), int(match[2])) if match else None


def issue_references(body, repo):
    """Resolve Markdown links before their display text and ignore URL fragments."""
    text = re.sub(r'```[\s\S]*?```|<!--[\s\S]*?-->', lambda m: ' ' * len(m[0]), body or '')
    references = []
    masked = list(text)
    for match in re.finditer(r'\[[^]\n]*\]\((https?://[^\s)]+)\)', text):
        identity = github_identity(match[1])
        if identity:
            references.append((identity, match.start(), match.end()))
        masked[match.start():match.end()] = ' ' * len(match[0])
    remaining = ''.join(masked)
    for match in re.finditer(r'https?://[^\s<>\])]+', remaining):
        identity = github_identity(match[0].rstrip('.,;:'))
        if identity:
            references.append((identity, match.start(), match.end()))
        masked[match.start():match.end()] = ' ' * len(match[0])
    remaining = ''.join(masked)
    for match in re.finditer(r'(?<![\w/.-])(?:([\w.-]+/[\w.-]+))?#([1-9]\d*)(?![\w])', remaining):
        references.append(((canonical_repo(match[1] or repo), int(match[2])), match.start(), match.end()))
    return [{'identity': identity, 'closing': bool(re.search(
                r'\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s+$', text[max(0, start - 30):start], re.I)),
             'text': text[start:end],
             'context': text[text.rfind('\n', 0, start) + 1:text.find('\n', end) if '\n' in text[end:] else len(text)]}
            for identity, start, end in sorted(references, key=lambda x: x[1])]


def classify_pr(issue, pr, repo):
    """Return no relation, a review hint, or exact same-repository competition."""
    if (type(issue.get('number')) is not int or issue['number'] <= 0
            or type(pr.get('number')) is not int or pr['number'] <= 0):
        raise ValueError('Invalid issue or PR number')
    closing_refs = pr.get('closingIssuesReferences', [])
    if (not isinstance(closing_refs, list) or any(not isinstance(ref, dict)
            or type(ref.get('number')) is not int or ref['number'] <= 0 for ref in closing_refs)):
        raise ValueError('Invalid PR closing issue references')
    target = github_identity(issue.get('url') or issue.get('html_url'))
    expected = (canonical_repo(repo), issue['number'])
    if target != expected:
        raise ValueError('Issue URL does not match repository and number')
    source = github_identity(pr.get('url') or pr.get('html_url'), 'pull')
    if source != (expected[0], pr['number']):
        raise ValueError('PR URL does not match repository and number')
    references = [item for item in issue_references(pr.get('body'), repo) if item['identity'] == target]
    closing = [ref for ref in closing_refs
               if github_identity(ref.get('url') or ref.get('html_url')) == target]
    ambiguous = [ref for ref in closing_refs
                 if ref.get('number') == issue['number'] and not (ref.get('url') or ref.get('html_url'))]
    if not references and not closing and not ambiguous:
        return None
    body = pr.get('body') or ''
    # A partial fix for another mentioned issue must not erase this issue's
    # explicit closing reference (large PRs often close several independent bugs).
    partial = r'\bpartially (?:address|cover)|\bpart of\s+#|\bdoes not (?:fix|address|cover|change)\b|\bremaining .{0,40}\b(?:scope|issues?)\b'
    limited_scope = bool(re.search(r'\btest[ -]only\b|\bthis PR (?:only )?partially (?:address|cover)', body, re.I)
                         or re.match(r'^test(?:\([^)]*\))?:', pr.get('title') or '', re.I)
                         or any(re.search(partial, ref['context'], re.I) for ref in references))
    inactive = str(pr.get('state', 'open')).casefold() in ('closed', 'merged')
    definitive = bool(closing or any(ref['closing'] for ref in references))
    relation = 'inactive' if inactive else 'competing' if definitive and not limited_scope else 'scope_review'
    return {'url': pr.get('url') or pr.get('html_url'), 'number': pr['number'], 'relation': relation,
            'reason': ('Closed/merged PR requires baseline and scope review' if inactive else
                       'Explicit partial or test-only scope needs review' if limited_scope else
                       'Exact same-repository closing reference' if definitive else
                       'Reference does not establish implementation coverage'),
            'head_sha': pr.get('headRefOid') or pr.get('head_sha'),
            'review_required': relation != 'competing'}
