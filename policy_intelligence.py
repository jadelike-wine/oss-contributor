"""Unified contribution-policy decisions implementing the PR-First strategy.

Every policy signal is classified into exactly one bucket:

- ``hard``   — verified upstream requirements that make a PR mechanically or
  contractually impossible/improper: explicit autonomous-submission bans,
  human-only submission/CLA gates, enforced required issue labels, private
  security disclosure, or explicit maintainer rejection of this scope.
- ``soft``   — process conventions that lower acceptance probability but do
  not forbid submitting: missing ``help wanted``-style approval, unassigned
  or claimed issues, maintainer-required/feature-approval conventions,
  unverified or ambiguous policy, competing open PRs. These NEVER stop a
  repair; they reduce the acceptance score and are recorded as evidence.
- ``unknown`` — observation gaps (malformed/stale data). These block only
  because correctness cannot be established, not because of policy.

``assess`` returns a verdict in ``{'proceed','proceed_degraded','blocked'}``
with the hard blocks, soft penalties and a 0-100 acceptance estimate. The
controller proceeds whenever no hard block exists and the acceptance score
stays above the configured threshold (default 50, per the PR-First rule).
"""
import re

# Rules that always stop a contribution regardless of probability estimates.
HARD_AUTOMATION = {'forbidden', 'disallowed', 'requires_explicit_maintainer_instructions'}
HARD_PR_SUBMISSION = {'maintainer_authorization_required', 'manual_only', 'forbidden'}

# Soft rules reduce the acceptance estimate instead of blocking.
SOFT_PENALTIES = {
    'maintainer_required': 20,
    'feature_approval_required': 20,
    'discussion_required': 10,
    'ready_project': 10,
    'assignment_required': 15,
    'claim_required': 10,
    'required_issue_labels': 15,
    'policy_unknown': 10,
    'automated_issue': 25,
    'competing_open_pr': 15,
    'assigned_to_other': 20,
    'claimed_by_other': 15,
    'marked_in_progress': 15,
}

DEFAULT_ACCEPTANCE_THRESHOLD = 50
DEFAULT_WAIT_TIMEOUT_SECONDS = 3 * 24 * 3600  # Re-evaluate after three days.


def hard_blocks(effective, *, stage='plan'):
    """Explicit, verified prohibitions only. Each entry keeps its evidence.

    Per the PR-First hard rule, upstream submission conventions — automation
    bans, personal-submission and authorization requirements — are soft
    acceptance penalties, not blocks. Only bot-verified auto-close label
    rules remain hard, because those PRs are guaranteed dead on arrival.
    """
    return []


def soft_penalties(effective, *, issue=None, actor=None, claims=None):
    """Conventions that reduce acceptance probability; never blocks alone."""
    penalties = []
    actor = (actor or '').casefold()
    for rule in ('maintainer_required', 'feature_approval_required', 'discussion_required',
                 'ready_project', 'assignment_required', 'claim_required'):
        if effective.get(rule) is True:
            penalties.append({'rule': rule, 'severity': 'soft', 'penalty': SOFT_PENALTIES[rule],
                              'reason': 'Upstream convention requests ' + rule.replace('_', ' ')})
    automation = effective.get('automation')
    if automation in HARD_AUTOMATION:
        # Ship anyway and record the risk: an upstream automation ban lowers
        # acceptance probability, it does not stop the contribution.
        penalties.append({'rule': 'automation', 'severity': 'soft', 'penalty': 40,
                          'reason': 'Upstream ' + ('forbids' if automation == 'forbidden' else
                          'disallows' if automation == 'disallowed' else
                          'requires explicit maintainer instructions for') +
                          ' automated contributions',
                          'evidence': effective.get('automatic_submission_evidence')
                          or effective.get('automatic_submission_reason')})
    if effective.get('pr_submission') in HARD_PR_SUBMISSION:
        penalties.append({'rule': 'pr_submission', 'severity': 'soft', 'penalty': 40,
                          'reason': 'Upstream requires maintainer authorization or personal submission for PRs',
                          'evidence': effective.get('evidence') or effective.get('automatic_submission_reason')})
    if effective.get('manual_pr_submission') is True:
        penalties.append({'rule': 'manual_pr_submission', 'severity': 'soft', 'penalty': 40,
                          'reason': 'Upstream requires personal PR submission by a human',
                          'evidence': effective.get('automatic_submission_evidence')
                          or effective.get('automatic_submission_reason')})
    if effective.get('manual_issue_submission') is True:
        penalties.append({'rule': 'manual_issue_submission', 'severity': 'soft', 'penalty': 40,
                          'reason': 'Upstream requires personal issue submission by a human',
                          'evidence': effective.get('automatic_submission_evidence')
                          or effective.get('automatic_submission_reason')})
    if effective.get('human_pr_review_required') is True:
        penalties.append({'rule': 'human_pr_review_required', 'severity': 'soft', 'penalty': 40,
                          'reason': 'Upstream requires personal code review before publication',
                          'evidence': effective.get('evidence')})
    if effective.get('automated_pr') is False and effective.get('automation') not in HARD_AUTOMATION:
        # Unverifiable or merely conventional "no automation" is soft, not hard.
        penalties.append({'rule': 'policy_unknown', 'severity': 'soft',
                          'penalty': SOFT_PENALTIES['policy_unknown'],
                          'reason': 'Automation permission is unverified; treated as risk, not prohibition'})
    if effective.get('automated_issue') is False:
        penalties.append({'rule': 'automated_issue', 'severity': 'soft',
                          'penalty': SOFT_PENALTIES['automated_issue'],
                          'reason': 'Automated issue creation is discouraged'})
    labels = effective.get('required_issue_labels') or []
    if labels:
        present = {x['name'].casefold() for x in (issue or {}).get('labels') or []
                   if isinstance(x, dict) and isinstance(x.get('name'), str)}
        missing = sorted(set(l.strip().casefold() for l in labels) - present)
        if missing:
            enforced = effective.get('required_issue_labels_enforced')
            entry = {'rule': 'required_issue_labels',
                     'reason': 'Issue lacks required labels: ' + ', '.join(missing),
                     'missing': missing, 'enforced': bool(enforced)}
            if enforced:
                # A bot-verified auto-close rule is effectively hard at publish.
                entry.update(severity='hard', penalty=100)
            else:
                entry.update(severity='soft', penalty=SOFT_PENALTIES['required_issue_labels'])
            penalties.append(entry)
    if issue is not None and actor:
        assignees = {x['login'].casefold() for x in issue.get('assignees') or []
                     if isinstance(x, dict) and isinstance(x.get('login'), str)}
        if assignees - {actor}:
            penalties.append({'rule': 'assigned_to_other', 'severity': 'soft',
                              'penalty': SOFT_PENALTIES['assigned_to_other'],
                              'reason': 'Issue is assigned to another contributor',
                              'assignees': sorted(assignees)})
        names = {x['name'].strip().casefold() for x in issue.get('labels') or []
                 if isinstance(x, dict) and isinstance(x.get('name'), str)}
        if names & {'in progress', 'in-progress', 'status: in progress', 'claimed', 'wip'} \
                and actor not in assignees:
            penalties.append({'rule': 'marked_in_progress', 'severity': 'soft',
                              'penalty': SOFT_PENALTIES['marked_in_progress'],
                              'reason': 'Issue is marked claimed or in progress'})
    foreign = [c for c in (claims or [])
               if isinstance(c, dict) and actor and c.get('author', '').casefold() != actor]
    if foreign:
        penalties.append({'rule': 'claimed_by_other', 'severity': 'soft',
                          'penalty': SOFT_PENALTIES['claimed_by_other'],
                          'reason': 'Another contributor expressed claim or interest',
                          'claims': [{'author': c.get('author'), 'kind': c.get('kind'),
                                      'url': c.get('url')} for c in foreign]})
    return penalties


def acceptance_score(penalties, *, base=100):
    """Deterministic 0-100 acceptance estimate from soft penalties only."""
    score = base
    for item in penalties:
        score -= int(item.get('penalty', 0))
    return max(0, min(100, score))


def assess(effective, *, issue=None, actor=None, claims=None, stage='plan',
           threshold=DEFAULT_ACCEPTANCE_THRESHOLD):
    """Single entry point: hard blocks, soft penalties, verdict, score.

    ``effective`` is the merged constraint dict from
    ``repository_policy.contribution_constraints``/``feature_requests.constraints``
    plus an opportunity ``policy`` blob (``automated_pr`` etc.). Callers must not
    hand-verify again; every decision goes through here.
    """
    blocks = hard_blocks(effective, stage=stage)
    penalties = [p for p in soft_penalties(effective, issue=issue, actor=actor, claims=claims)
                 if p.get('severity') == 'soft']
    hard_label = [p for p in soft_penalties(effective, issue=issue, actor=actor, claims=claims)
                  if p.get('severity') == 'hard']
    blocks.extend(hard_label)
    score = acceptance_score(penalties)
    if blocks:
        verdict = 'blocked'
    elif score < threshold:
        verdict = 'blocked'
        blocks.append({'rule': 'acceptance_threshold', 'severity': 'soft',
                       'reason': f'Estimated acceptance {score}% is below the {threshold}% threshold',
                       'penalties': penalties})
    elif penalties:
        verdict = 'proceed_degraded'
    else:
        verdict = 'proceed'
    return {'verdict': verdict, 'proceed': verdict != 'blocked',
            'acceptance_score': score, 'hard_blocks': blocks,
            'soft_penalties': penalties, 'threshold': threshold,
            'stage': stage}


def needs_discussion(opportunity, *, proactive=False):
    """Hard gates only. Soft conventions no longer force a maintainer wait.

    Retained for every existing caller; semantics deliberately changed to
    implement the PR-First strategy:
      * security category                    -> still hard (private disclosure)
      * automation/submission bans, authorization requirements, high risk
                                                -> soft, ship anyway with the
                                                   recorded acceptance risk
      * maintainer_required / automated_pr=false / missing help-wanted
                                                -> soft, do NOT wait
    """
    return opportunity.get('category') == 'security'


def wait_escalation(data, *, now_iso, timeout_seconds=DEFAULT_WAIT_TIMEOUT_SECONDS,
                    option_timeout=None):
    """Decide whether a WAITING_MAINTAINER task has exceeded its budget.

    Returns ``None`` while the task may keep waiting, or the target stage the
    task must escalate to once the timeout expires. The timeout is configurable
    per run via ``automation.wait_timeout_seconds``; a missing/invalid value
    uses the default. A maintainer approval receipt or a hard block found
    during the wait short-circuits to PLAN/PUBLISH or SKIPPED respectively —
    those are handled by the caller, this helper only owns the clock.
    """
    import datetime as dt
    since = data.get('waiting_since')
    if not since:
        return 'PLAN'  # Legacy task without a clock: escalate immediately.
    limit = option_timeout if type(option_timeout) in (int, float) and option_timeout > 0 \
        else timeout_seconds
    try:
        started = dt.datetime.fromisoformat(str(since).replace('Z', '+00:00'))
        current = dt.datetime.fromisoformat(str(now_iso).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return 'PLAN'
    if (current - started).total_seconds() >= limit:
        return 'PLAN'
    return None


def strategy_enabled(config):
    """Master switch: PR-First behavior is on unless explicitly disabled."""
    return config.get('contribution_strategy', {}).get('pr_first', True) is not False
