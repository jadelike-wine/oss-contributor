"""Shared, side-effect-free model and executor selection."""

DEFAULT_MODEL = 'tokenking/devin/swe-2'
RESEARCH_PURPOSES = frozenset(('research', 'research-correction', 'survey', 'survey-correction'))
IMPLEMENTATION_CHANNELS = frozenset(('opencode', 'dsh'))


def is_research_purpose(purpose):
    return (purpose in RESEARCH_PURPOSES
            or isinstance(purpose, str) and purpose.startswith('survey-'))


def _research(config):
    options = config.get('research') or {}
    if not isinstance(options, dict):
        raise ValueError('research must be an object')
    return options


def _model(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Selected model must be a nonempty string')
    return value


def implementation_channel(config):
    channel = config.get('implementation_channel', 'opencode')
    if not isinstance(channel, str) or channel not in IMPLEMENTATION_CHANNELS:
        raise ValueError('implementation_channel must be opencode or dsh')
    return channel


def execution_channel(config, purpose):
    """Honor the existing research executor independently of implementation.

    Hermes research has its own adapter/decorator. This helper selects the
    execution adapter used by model(), including explicitly configured research
    through OpenCode or DSH. Old configs retain OpenCode for that research path.
    """
    if is_research_purpose(purpose):
        channel = _research(config).get('model_channel', 'opencode')
        if isinstance(channel, str) and channel in IMPLEMENTATION_CHANNELS:
            return channel
        if channel != 'hermes':
            raise ValueError('research.model_channel must be opencode, dsh or hermes')
    return implementation_channel(config)


def research_model(config):
    return _model(_research(config).get('model') or config.get('model', DEFAULT_MODEL))


def selected_model(config, purpose, review=False):
    # Review takes precedence even when a caller labels its purpose research.
    if review:
        return _model(config.get('review_model') or config.get('model', DEFAULT_MODEL))
    if is_research_purpose(purpose):
        return research_model(config)
    return _model(config.get('model', DEFAULT_MODEL))
