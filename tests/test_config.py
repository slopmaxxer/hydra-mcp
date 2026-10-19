import pytest

from hydra_mcp.config import ConfigError, load_config, normalize_url


def test_toml_instances_and_credentials(tmp_path):
    (tmp_path / 'user').write_text('alice\n')
    (tmp_path / 'pass').write_text('s3cret\n')
    config = tmp_path / 'config.toml'
    config.write_text(f'''
default = "mine"
[instances.public]
url = "https://hydra.example.org/"
[instances.mine]
url = "https://ci.example.org/hydra"
username_file = "{tmp_path / 'user'}"
password_file = "{tmp_path / 'pass'}"
''')
    loaded = load_config(config)
    assert loaded.default == 'mine'
    assert loaded.get(None).url == 'https://ci.example.org/hydra'
    assert loaded.get('public').url == 'https://hydra.example.org'
    assert not loaded.get('public').can_login
    assert loaded.get('mine').credentials() == ('alice', 's3cret')
    assert loaded.get('mine').public() == {'name': 'mine', 'url': 'https://ci.example.org/hydra', 'can_login': True}
    with pytest.raises(ConfigError, match='Unknown Hydra instance'):
        loaded.get('nope')


def test_environment_fallback(tmp_path):
    loaded = load_config(tmp_path / 'missing.toml', env={'HYDRA_URL': 'http://127.0.0.1:3000', 'HYDRA_USERNAME': 'bob',
                                                         'HYDRA_PASSWORD_FILE': str(tmp_path / 'pw')})
    instance = loaded.get(None)
    assert instance.url == 'http://127.0.0.1:3000'
    assert instance.can_login
    with pytest.raises(ConfigError, match='Cannot read credentials'):
        instance.credentials()
    assert load_config(tmp_path / 'missing.toml', env={}).get(None).url == 'https://hydra.nixos.org'


@pytest.mark.parametrize('url', ['http://hydra.example.org', 'ftp://x', 'https://u:p@hydra.example.org', 'https://h.example/?q=1'])
def test_rejects_unsafe_urls(url):
    with pytest.raises(ConfigError):
        normalize_url(url)


def test_rejects_inline_passwords_and_unknown_keys(tmp_path):
    config = tmp_path / 'config.toml'
    config.write_text('[instances.a]\nurl = "https://h.example"\npassword = "x"\n')
    with pytest.raises(ConfigError, match='password_file'):
        load_config(config)
    config.write_text('[instances.a]\nurl = "https://h.example"\ntoken = "x"\n')
    with pytest.raises(ConfigError, match='unknown keys'):
        load_config(config)
    config.write_text('default = "b"\n[instances.a]\nurl = "https://h.example"\n')
    with pytest.raises(ConfigError, match='default instance'):
        load_config(config)


async def test_server_reloads_changed_config(tmp_path):
    from hydra_mcp.server import create_server
    first, second = tmp_path / 'a.toml', tmp_path / 'b.toml'
    first.write_text('[instances.one]\nurl = "https://one.example"\n')
    second.write_text('default = "two"\n[instances.two]\nurl = "https://two.example"\n')
    link = tmp_path / 'config.toml'
    link.symlink_to(first)  # Home Manager's way: a symlink that a switch repoints
    server = create_server(config_file=link)

    async def instances():
        result = await server.call_tool('list_instances', {})
        return result.structured_content['default'], [i['url'] for i in result.structured_content['instances']]

    assert await instances() == ('one', ['https://one.example'])
    link.unlink()
    link.symlink_to(second)
    assert await instances() == ('two', ['https://two.example'])
    second.write_text('default = "two"\n[instances.two]\nurl = "https://two.example/hydra"\n')
    assert await instances() == ('two', ['https://two.example/hydra'])
    link.unlink()
    assert await instances() == ('default', ['https://hydra.nixos.org'])  # no file: env defaults
