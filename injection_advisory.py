"""Local, bounded advisory rules; never blocks, edits, follows URLs, or runs text.

Rules are original, small heuristics informed by public prompt-injection categories.
See SECURITY.md and THIRD_PARTY_NOTICES.md. No remote rules or model calls.
"""
import re
import unicodedata

SCANNER = 'agent-chat-patterns'
VERSION = '1'
MAX_MESSAGE = 7000
MAX_LABEL = 40
MAX_NORMALIZED = 14000

OVERRIDE = (
    r'\b(?:ignore|disregard|override|bypass)\s+'
    r'(?:(?:all|any|the|your)\s+){0,3}'
    r'(?:previous|prior|system|developer|safety|governing)\s+'
    r'(?:(?:system|developer|safety)\s+)?(?:instructions|rules|policies|constraints)\b'
)
SECRET = (
    r'\b(?:reveal|print|show|display|send|upload|exfiltrate|return|read|copy)\s+'
    r'(?:(?:me|us|the|your|all|my|our|complete|stored|private)\s+){0,5}'
    r'(?:secrets?|credentials?|passwords?|api[ _-]?keys?|access[ _-]?tokens?|'
    r'private[ _-]?keys?|private files?|system prompt|developer instructions|environment variables)\b'
)
EXECUTE = (
    r'\b(?:run|execute|invoke|call)\s+'
    r'(?:(?:the|a|this|following|your|local)\s+){0,4}'
    r'(?:shell(?: command)?|command|terminal|process|tool|bash|powershell)\b'
)
PERMISSION = (
    r'\b(?:you (?:are authorized|have permission)|(?:owner |user )?approval is granted|'
    r'(?:owner |user )?permission is granted|no need to ask for (?:permission|approval)|'
    r'do not ask for (?:permission|approval))\b'
)
RULES = (
    ('instruction_override', OVERRIDE,
     'Request to override governing instructions or safety rules.'),
    ('secret_request', SECRET,
     'Directive to access or disclose secrets, private data, or hidden instructions.'),
    ('execution_request', EXECUTE,
     'Directive to invoke a tool or run a process/command; a post is not authorization.'),
    ('permission_claim', PERMISSION + r'.{0,160}' + EXECUTE + r'|' +
     EXECUTE + r'.{0,120}\b(?:without (?:asking|approval|permission)|(?:approval|permission) is granted)\b',
     'Claimed or bypassed approval paired with a tool/process directive.'),
    ('external_transfer', SECRET + r'.{0,240}https?://',
     'Sensitive-data directive paired with an external URL; no destination was fetched.'),
)
COMPILED = [(name, re.compile(pattern), reason) for name, pattern, reason in RULES]
AUTHORITY = re.compile(
    r'(?:<\|(?:im_start|start_header_id)\|>\s*(?:system|developer)|'
    r'<(?:system|developer)>|<<sys>>|'
    r'["\']role["\']\s*:\s*["\'](?:system|developer)["\']|'
    r'\bi am (?:the|your) (?:owner|administrator|developer|system)\b)'
)
NEGATION = re.compile(r"\b(?:never|do not|don't|must not|should not|shouldn't|avoid)\s*$")
AUTHORITY_LABELS = {'system', 'developer', 'owner', 'admin', 'administrator'}


def not_scanned(reason):
    return dict(scanner=SCANNER, version=VERSION, advisory=True, status='not_scanned',
                scan_complete=False, scope='public_message_and_label',
                reason=reason, findings=[])


def normalize(value, limit, normalized_limit=MAX_NORMALIZED):
    # Slice before normalization; NFKC/casefold expansion is capped as well.
    prefix = value[:limit]
    normalized = unicodedata.normalize('NFKC', prefix).casefold()
    normalized = ''.join(c for c in normalized if unicodedata.category(c) != 'Cf')
    complete = len(value) <= limit and len(normalized) <= normalized_limit
    normalized = re.sub(r'[^\S\n]+', ' ', normalized[:normalized_limit])
    return normalized, complete


def scan(message, agent=''):
    """Scan only the supplied public view. Findings never contain matched excerpts."""
    if not isinstance(message, str):
        return not_scanned('body_unavailable')
    body, complete = normalize(message, MAX_MESSAGE)
    label, label_complete = normalize(agent if isinstance(agent, str) else '', MAX_LABEL, 160)
    complete = complete and label_complete
    combined = label + '\n' + body
    found = {}
    fence = None
    offset = 0

    def add(rule_id, explanation, context):
        item = found.setdefault(rule_id, dict(rule_id=rule_id, explanation=explanation, contexts=[]))
        if context not in item['contexts']:
            item['contexts'].append(context)

    for line in combined.splitlines(keepends=True):
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in ('~~~', '\x60\x60\x60'):
            fence = None if fence == marker else marker
        for rule_id, pattern, explanation in COMPILED:
            for match in pattern.finditer(line):
                before = line[:match.start()]
                if NEGATION.search(before[-32:]):
                    continue
                # Context is descriptive, never a whitelist or a claim about intent.
                quoted = (fence is not None or stripped.startswith('>') or
                          before.count('\x60') % 2 == 1 or before.count('"') % 2 == 1)
                context = 'sender_label' if offset < len(label) else 'quoted_or_code' if quoted else 'unquoted'
                add(rule_id, explanation, context)
                preceding = combined[max(0, offset + match.start() - 160):offset + match.start()]
                if label.strip() in AUTHORITY_LABELS or AUTHORITY.search(preceding):
                    add('authority_spoofing',
                        'Authority-style label or role claim accompanies a directive; identity is unverified.',
                        context)
        offset += len(line)
    return dict(scanner=SCANNER, version=VERSION, advisory=True,
                status='flagged' if found else 'no_match' if complete else 'partial',
                scan_complete=complete, scope='public_message_and_label',
                reason='bounded_rules_checked' if complete else 'scan_limit_reached',
                findings=list(found.values()))
