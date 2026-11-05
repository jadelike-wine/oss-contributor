"""Local contribution preferences, separate from upstream permission and approval."""


def enabled(config, name):
    return config.get('contribution_strategy', {}).get(name) is True


def local_discovery(task, data):
    # Reusing somebody else's pre-existing request is not discovery priority.
    if data.get('existing_issue') or data.get('research', {}).get('existing_issue'):
        return False
    origin = data.get('contribution_origin', {})
    return (origin.get('kind') == 'local_discovery' and origin.get('task') == task.get('id')
            or task.get('source') == 'hunt'
            or '--hunt-' in task.get('id', ''))


PRIORITY_GUIDANCE = (
    'For our own reported or independently discovered issue, another contributor PR, '
    'low acceptance likelihood, silence, a declined proposal or lack of discretionary maintainer enthusiasm '
    'alone must not cancel our implementation. Review competing scope and preserve its URLs; '
    'do not copy or claim others work. Reuse an existing issue rather than publish a duplicate. '
    'Explicit upstream approval/label/automation/human-review rules, security, actual baseline '
    'reproduction, patch checks and review remain mandatory. Never fabricate maintainer approval. '
)
