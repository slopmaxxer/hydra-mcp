"""Tools against an in-process fake Hydra (httpx.MockTransport)."""
import json
from pathlib import Path

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from hydra_mcp.client import HydraClient
from hydra_mcp.config import Config, Instance
from hydra_mcp.server import create_server

BASE = 'https://hydra.test'
BUILD_HTML = (Path(__file__).parent / 'fixtures' / 'build.html').read_text()


class FakeHydra:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.puts: dict[str, dict] = {}
        self.jobset = {'project': 'p', 'name': 'j', 'type': 1, 'flake': 'git+https://example.org/r.git', 'enabled': 1,
                       'visible': True, 'enableemail': False, 'checkinterval': 300, 'schedulingshares': 100, 'keepnr': 3,
                       'emailoverride': '', 'description': 'd', 'errormsg': 'error: \x1b[31mboom\x1b[0m', 'errortime': 1,
                       'fetcherrormsg': '', 'lastcheckedtime': 1700000000, 'starttime': None, 'triggertime': None}

    def logged_in(self, request):
        return 'hydra_session=ok' in request.headers.get('cookie', '')

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        wants_json = 'json' in request.headers.get('accept', '')
        if method == 'POST' and path == '/login':
            assert request.headers['referer'] == BASE + '/'
            if json.loads(request.content) == {'username': 'alice', 'password': 'pw'}:
                return httpx.Response(200, json={'username': 'alice'}, headers={'set-cookie': 'hydra_session=ok; Path=/'})
            return httpx.Response(403, json={'error': 'Bad username or password.'})
        if method == 'POST' and path == '/api/push':
            if not self.logged_in(request):
                return httpx.Response(403, json={'error': 'Only the project members can restart.'})
            return httpx.Response(200, json={'jobsetsTriggered': [request.url.params['jobsets']]})
        if method == 'PUT':
            if not self.logged_in(request):
                return httpx.Response(403, json={'error': 'not allowed'})
            self.puts[path] = json.loads(request.content)
            return httpx.Response(201, json={'redirect': BASE + path})
        if path in ('/build/10/restart', '/build/10/cancel', '/eval/1/restart-failed'):
            if method != 'POST' or request.headers.get('referer') != BASE + '/':
                return httpx.Response(405, json={'error': 'Request must be POSTed.'})
            if not self.logged_in(request):
                return httpx.Response(403, json={'error': 'Only the project members can restart.'})
            return httpx.Response(302, headers={'location': BASE + path.rsplit('/', 1)[0]})
        if path == '/':
            return httpx.Response(200, json=[{'name': 'p', 'displayname': 'P', 'owner': 'alice', 'enabled': True,
                                              'jobsets': ['j'], 'hidden': False}, {'name': 'secret', 'hidden': True}])
        if path == '/jobset/p/j':
            return httpx.Response(200, json=self.jobset)
        if path == '/jobset/p/new':
            if path in self.puts:
                return httpx.Response(200, json={'project': 'p', 'type': 1, 'enabled': 2} | self.puts[path])
            return httpx.Response(404, json={'error': 'Jobset p:new doesn\'t exist.'})
        if path == '/jobset/p/j/evals':
            return httpx.Response(200, json={'evals': [{'id': 1, 'builds': [10, 11, 12], 'timestamp': 1700000000,
                                                        'flake': 'git+https://example.org/r.git?rev=abc', 'hasnewbuilds': 1,
                                                        'jobsetevalinputs': {}}], 'last': '?page=4'})
        if path == '/eval/1':
            return httpx.Response(200, json={'id': 1, 'builds': [10, 11, 12], 'timestamp': 1700000000, 'flake': 'f'})
        if path == '/eval/1/builds':
            return httpx.Response(200, json=[
                {'id': 10, 'job': 'b', 'finished': 1, 'buildstatus': 2, 'starttime': 5, 'stoptime': 9},
                {'id': 11, 'job': 'a', 'finished': 1, 'buildstatus': 0},
                {'id': 12, 'job': 'c', 'finished': 0, 'buildstatus': None},
            ])
        if path == '/build/10':
            if not wants_json:
                return httpx.Response(200, text=BUILD_HTML, headers={'content-type': 'text/html'})
            return httpx.Response(200, json={'id': 10, 'job': 'b', 'project': 'p', 'jobset': 'j', 'finished': 1,
                                             'buildstatus': 2, 'drvpath': '/nix/store/zzz-b.drv',
                                             'buildoutputs': {'out': {'path': '/nix/store/yyy-b'}}, 'jobsetevals': [1]})
        if path == '/log/zzz-b.drv':
            return httpx.Response(404, json={'error': 'The build log of /nix/store/zzz-b.drv is not available.'})
        if path == '/build/10/nixlog/3/raw':
            return httpx.Response(302, headers={'location': BASE + '/log/dep.drv'})
        if path == '/build/10/nixlog/9/raw':
            return httpx.Response(302, headers={'location': 'https://evil.test/log/x.drv'})
        if path == '/log/dep.drv':
            tail = int(request.url.params['tail'])
            lines = [f'line {i}' for i in range(1, 101)] + ['\x1b[1merror:\x1b[0m make failed']
            return httpx.Response(200, text='\n'.join(lines[-tail:]), headers={'content-type': 'text/plain'})
        if path == '/redirect-away':
            return httpx.Response(302, headers={'location': 'https://evil.test/'})
        return httpx.Response(404, json={'error': f'no route {path}'})


@pytest.fixture
async def hydra(tmp_path):
    (tmp_path / 'pw').write_text('pw\n')
    fake = FakeHydra()
    instance = Instance(name='test', url=BASE, username='alice', password_file=tmp_path / 'pw')
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
        client = HydraClient(instance, http)
        server = create_server(Config({'test': instance}, 'test'), {'test': client})
        yield server, fake, client


async def call(server, name, args):
    result = await server.call_tool(name, args)
    assert not result.is_error, result.content
    return result.structured_content


async def test_read_tools(hydra):
    server, fake, _ = hydra
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools['get_build'].annotations.read_only_hint is True
    assert tools['cancel_build'].annotations.destructive_hint is True
    projects = await call(server, 'list_projects', {})
    assert [p['name'] for p in projects['projects']] == ['p']
    jobset = await call(server, 'get_jobset', {'project': 'p', 'jobset': 'j'})
    assert jobset['eval_error'] == 'error: boom'
    assert jobset['recent_evals'][0]['build_count'] == 3
    evals = await call(server, 'list_evals', {'project': 'p', 'jobset': 'j'})
    assert evals['last_page'] == 4
    evaluation = await call(server, 'get_eval', {'eval_id': 1, 'status': 'failed'})
    assert evaluation['counts'] == {'succeeded': 1, 'failed': 1, 'queued': 1}
    assert [(b['id'], b['status']) for b in evaluation['builds']] == [(10, 'dependency failed')]
    build = await call(server, 'get_build', {'build_id': 10, 'steps': 'all'})
    assert build['outputs'] == {'out': '/nix/store/yyy-b'}
    assert [s['nr'] for s in build['steps']] == [4, 3, 2, 1]
    assert build['steps'][1]['outputs'] == ['/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-example-lib',
                                           '/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-example-lib-dev']
    assert build['steps'][1]['status'] == 'Failed: builder failed with exit code 2'
    assert build['steps'][2]['kind'] == 'substitution'
    assert build['steps'][3]['propagated_from_build'] == 7
    assert (build['failed_steps'], build['running_steps'], build['steps_total']) == ([3, 1], [4], 4)
    relevant = await call(server, 'get_build', {'build_id': 10, 'recent_steps': 0})
    assert [s['nr'] for s in relevant['steps']] == [4, 3, 1]  # running + failed, no succeeded substitution
    bare = await call(server, 'get_build', {'build_id': 10, 'steps': 'none'})
    assert 'steps' not in bare and bare['status'] == 'dependency failed'
    assert not any(r.method != 'GET' for r in fake.requests)


async def test_logs(hydra):
    server, _, _ = hydra
    log = await call(server, 'get_build_log', {'build_id': 10, 'step': 3, 'tail_lines': 2})
    assert log['log'] == 'line 100\nerror: make failed'
    assert log['url'] == BASE + '/log/dep.drv'
    with pytest.raises(ToolError, match='failed_steps'):
        await server.call_tool('get_build_log', {'build_id': 10})
    with pytest.raises(ToolError, match='No log location|Refusing'):
        await server.call_tool('get_build_log', {'build_id': 10, 'step': 9})


async def test_client_refuses_off_site_redirects(hydra):
    _, _, client = hydra
    with pytest.raises(Exception, match='Refusing to follow a redirect'):
        await client.get('/redirect-away')


async def test_write_tools_log_in_once(hydra):
    server, fake, _ = hydra
    triggered = await call(server, 'trigger_evaluation', {'project': 'p', 'jobset': 'j'})
    assert triggered['triggered'] == ['p:j']
    await call(server, 'restart_build', {'build_id': 10})
    await call(server, 'manage_eval', {'eval_id': 1, 'action': 'restart-failed'})
    assert [r.url.path for r in fake.requests if r.method == 'POST'] == [
        '/login', '/api/push', '/build/10/restart', '/eval/1/restart-failed']
    push = next(r for r in fake.requests if r.url.path == '/api/push')
    assert push.headers['origin'] == BASE


async def test_put_jobset_merges_and_omits_false_flags(hydra):
    server, fake, _ = hydra
    await call(server, 'put_jobset', {'project': 'p', 'jobset': 'j', 'check_interval_s': 60})
    body = fake.puts['/jobset/p/j']
    assert body['checkinterval'] == '60'
    assert body['flake'] == 'git+https://example.org/r.git'
    assert body['visible'] == '1'
    assert 'enableemail' not in body  # any value would turn email on
    created = await call(server, 'put_jobset', {'project': 'p', 'jobset': 'new', 'flake': 'git+https://example.org/n.git',
                                                'visible': False, 'state': 'one-shot'})
    assert created['created'] is True
    body = fake.puts['/jobset/p/new']
    assert 'visible' not in body and body['enabled'] == '2' and body['type'] == '1'
    with pytest.raises(ToolError, match='needs `flake`'):
        await server.call_tool('put_jobset', {'project': 'p', 'jobset': 'other'})


async def test_bad_credentials(tmp_path):
    (tmp_path / 'pw').write_text('wrong')
    instance = Instance(name='test', url=BASE, username='alice', password_file=tmp_path / 'pw')
    async with httpx.AsyncClient(transport=httpx.MockTransport(FakeHydra())) as http:
        server = create_server(Config({'test': instance}, 'test'), {'test': HydraClient(instance, http)})
        with pytest.raises(ToolError, match='Login to test as .alice. failed: HTTP 403: Bad username'):
            await server.call_tool('restart_build', {'build_id': 10})
