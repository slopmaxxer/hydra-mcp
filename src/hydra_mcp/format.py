"""Turn Hydra's JSON and HTML into compact, labelled results."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import re

from bs4 import BeautifulSoup

# Builds.buildstatus (src/hydra-queue-runner/state.hh); null while unfinished.
BUILD_STATUS = {
    0: 'succeeded',
    1: 'failed',
    2: 'dependency failed',
    3: 'aborted',
    4: 'cancelled',
    6: 'failed with output',
    7: 'timed out',
    8: 'cached failure',
    9: 'unsupported system',
    10: 'log limit exceeded',
    11: 'output size limit exceeded',
    12: 'not deterministic',
}
# ANSI sequences first: the control-character class would otherwise eat just their ESC byte.
CONTROL = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]')


def iso(timestamp: int | None) -> str | None:
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace('+00:00', 'Z')


def build_status(build: dict[str, Any]) -> str:
    if not build.get('finished'):
        return 'running' if build.get('starttime') else 'queued'
    return BUILD_STATUS.get(build.get('buildstatus'), f'status {build.get("buildstatus")}')


def status_group(build: dict[str, Any]) -> str:
    if not build.get('finished'):
        return 'queued'
    return 'succeeded' if build.get('buildstatus') == 0 else 'failed'


def build_summary(build: dict[str, Any], base: str, full: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        'id': build['id'],
        'url': f'{base}/build/{build["id"]}',
        'job': build.get('job'),
        'project': build.get('project'),
        'jobset': build.get('jobset'),
        'system': build.get('system'),
        'nixname': build.get('nixname'),
        'status': build_status(build),
    }
    start, stop = build.get('starttime'), build.get('stoptime')
    if start and stop:
        result['duration_s'] = stop - start
    if full:
        result |= {
            'finished': bool(build.get('finished')),
            'buildstatus': build.get('buildstatus'),
            'queued_at': iso(build.get('timestamp')),
            'started_at': iso(start),
            'stopped_at': iso(stop),
            'drvpath': build.get('drvpath'),
            'outputs': {name: out.get('path') for name, out in (build.get('buildoutputs') or {}).items()},
            'products': [
                {k: product.get(k) for k in ('name', 'type', 'subtype', 'path', 'filesize', 'defaultpath') if product.get(k) is not None}
                for product in (build.get('buildproducts') or {}).values()
            ],
            'metrics': build.get('buildmetrics') or {},
            'evals': build.get('jobsetevals') or [],
            'priority': build.get('priority'),
            'releasename': build.get('releasename'),
        }
    return result


def eval_summary(evaluation: dict[str, Any], base: str) -> dict[str, Any]:
    return {
        'id': evaluation['id'],
        'url': f'{base}/eval/{evaluation["id"]}',
        'evaluated_at': iso(evaluation.get('timestamp')),
        'flake': evaluation.get('flake'),
        'inputs': {
            name: {k: value.get(k) for k in ('type', 'uri', 'revision', 'value', 'dependency') if value.get(k) is not None}
            for name, value in (evaluation.get('jobsetevalinputs') or {}).items()
        },
        'has_new_builds': bool(evaluation.get('hasnewbuilds')),
        'eval_time_s': evaluation.get('evaltime'),
        'build_count': len(evaluation.get('builds') or []),
    }


def jobset_summary(jobset: dict[str, Any], base: str) -> dict[str, Any]:
    project, name = jobset.get('project'), jobset.get('name')
    result = {
        'project': project,
        'name': name,
        'url': f'{base}/jobset/{project}/{name}',
        'description': jobset.get('description') or '',
        'type': 'flake' if jobset.get('type') == 1 else 'legacy',
        'enabled': {0: 'disabled', 1: 'enabled', 2: 'one-shot', 3: 'one-at-a-time'}.get(jobset.get('enabled'), jobset.get('enabled')),
        'visible': bool(jobset.get('visible')),
        'check_interval_s': jobset.get('checkinterval'),
        'scheduling_shares': jobset.get('schedulingshares'),
        'keep_evals': jobset.get('keepnr'),
        'email_notifications': bool(jobset.get('enableemail')),
        'last_checked_at': iso(jobset.get('lastcheckedtime')),
        'evaluation_running_since': iso(jobset.get('starttime')),
        'triggered_at': iso(jobset.get('triggertime')),
        'last_error_at': iso(jobset.get('errortime')),
        'eval_error': clean(jobset.get('errormsg') or '').strip() or None,
        'fetch_error': clean(jobset.get('fetcherrormsg') or '').strip() or None,
    }
    if result['type'] == 'flake':
        result['flake'] = jobset.get('flake')
    else:
        result['nix_expression'] = {'input': jobset.get('nixexprinput'), 'path': jobset.get('nixexprpath')}
        result['inputs'] = {
            name: {'type': value.get('type'), 'value': value.get('value'), 'email_responsible': bool(value.get('emailresponsible'))}
            for name, value in (jobset.get('inputs') or {}).items()
        }
    return result


def clean(text: str) -> str:
    """Drop ANSI escapes and control characters that build logs often contain."""
    return CONTROL.sub('', text)


RUNNING_STEP = ('Preparing', 'Connecting', 'Sending', 'Building', 'Waiting', 'Receiving', 'Post-processing')


def step_state(step: dict[str, Any]) -> str:
    """succeeded, running or failed, from the status text of a parsed build step."""
    status = step.get('status') or ''
    if status.startswith('Succeeded'):
        return 'succeeded'
    if status.startswith(RUNNING_STEP):
        return 'running'
    return 'failed'


def parse_steps(html: str, base: str) -> list[dict[str, Any]]:
    """Build steps from the build page's "Build steps" tab (Hydra has no JSON for them)."""
    soup = BeautifulSoup(html, 'html.parser')
    tab = soup.find(id='tabs-buildsteps')
    if tab is None:
        return []
    steps = []
    for row in tab.select('tbody > tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) < 5:
            continue
        status_cell = cells[4]
        log = next((a['href'] for a in status_cell.find_all('a', href=True) if a.get_text(strip=True) == 'log'), None)
        propagated = next((a.get_text(strip=True) for a in status_cell.find_all('a', href=True) if '/build/' in a['href']
                           and a.get_text(strip=True).startswith('build ')), None)
        status_text = re.sub(r'\s+:', ':', ' '.join(status_cell.get_text(' ', strip=True).split()))
        status_text = re.sub(r'\s*\(\s*log\s*,\s*raw\s*,\s*tail\s*\)', '', status_text)
        status_text = re.sub(r'\s*\(propagated from build \d+\s*\)', '', status_text).strip()
        what = ' '.join(cells[1].get_text(' ', strip=True).split())
        step = {
            'nr': int(cells[0].get_text(strip=True)) if cells[0].get_text(strip=True).isdigit() else cells[0].get_text(strip=True),
            'kind': 'substitution' if what.startswith('Substitution') else 'build',
            'outputs': [path for path in re.findall(r'/nix/store/[^\s,]+', what)] or [what],
            'duration': ' '.join(cells[2].get_text(' ', strip=True).split()),
            'machine': ' '.join(cells[3].get_text(' ', strip=True).split()),
            'status': status_text,
            'has_log': log is not None,
        }
        if propagated:
            step['propagated_from_build'] = int(propagated.split()[1])
        steps.append(step)
    return steps
