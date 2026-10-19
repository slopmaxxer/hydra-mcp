"""Launch the real stdio entry point and speak MCP JSON-RPC through the SDK."""
import os
import sys

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def test_stdio_handshake_and_tools(tmp_path):
    config = tmp_path / 'config.toml'
    config.write_text('[instances.local]\nurl = "http://127.0.0.1:9"\n')  # nothing listens on the discard port
    env = dict(os.environ, HYDRA_MCP_CONFIG=str(config))
    params = StdioServerParameters(command=sys.executable, args=['-m', 'hydra_mcp'], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.server_info.name == 'hydra'
            listed = await session.list_tools()
            assert {'get_jobset', 'get_eval', 'get_build', 'get_build_log', 'trigger_evaluation', 'put_jobset'} <= {t.name for t in listed.tools}
            instances = await session.call_tool('list_instances', {})
            assert instances.structured_content['instances'][0]['name'] == 'local'
            failed = await session.call_tool('get_build', {'build_id': 1})
            assert failed.is_error
            assert 'request to http://127.0.0.1:9/build/1 failed' in failed.content[0].text
            prompts = await session.list_prompts()
            assert prompts.prompts[0].name == 'debug_jobset'


@pytest.mark.live
async def test_live_read_only():
    from hydra_mcp.config import Config, Instance
    from hydra_mcp.server import create_server
    url = os.environ.get('HYDRA_LIVE_URL', 'https://hydra.nixos.org')
    server = create_server(Config({'live': Instance(name='live', url=url)}, 'live'))
    projects = await server.call_tool('list_projects', {})
    assert projects.structured_content['projects']
    queue = await server.call_tool('get_queue', {'limit': 1})
    assert 'queued_total' in queue.structured_content
