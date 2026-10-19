"""Hydra instances the server may talk to, from a TOML file or environment variables."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
import os
import tomllib

DEFAULT_URL = 'https://hydra.nixos.org'
LOOPBACK_HOSTS = {'localhost', '127.0.0.1', '::1'}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Instance:
    name: str
    url: str
    username: str | None = None
    username_file: Path | None = None
    password_file: Path | None = None

    @property
    def can_login(self) -> bool:
        return (self.username is not None or self.username_file is not None) and self.password_file is not None

    def credentials(self) -> tuple[str, str]:
        """Read the login at call time, so secrets never sit in the config or the process args."""
        if not self.can_login:
            raise ConfigError(f'Instance {self.name!r} has no credentials configured (username + password_file).')
        try:
            username = self.username if self.username is not None else _read(self.username_file)
            return username, _read(self.password_file)
        except OSError as error:
            raise ConfigError(f'Cannot read credentials of instance {self.name!r}: {error.strerror}') from error

    def public(self) -> dict[str, object]:
        return {'name': self.name, 'url': self.url, 'can_login': self.can_login}


def _read(path: Path | None) -> str:
    assert path is not None
    return path.read_text().strip()


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.hostname or ''
    if parts.scheme == 'https' or (parts.scheme == 'http' and host in LOOPBACK_HOSTS):
        if parts.query or parts.fragment or parts.username or parts.password:
            raise ConfigError(f'Hydra URL must not contain credentials, a query or a fragment: {url!r}')
        return f'{parts.scheme}://{parts.netloc}{parts.path.rstrip("/")}'
    raise ConfigError(f'Hydra URL must use HTTPS (plain HTTP only on loopback): {url!r}')


def _path(value: object, field: str, name: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f'{field} of instance {name!r} must be a non-empty string')
    return Path(value).expanduser()


@dataclass(frozen=True)
class Config:
    instances: dict[str, Instance]
    default: str

    def get(self, name: str | None) -> Instance:
        key = name or self.default
        try:
            return self.instances[key]
        except KeyError:
            raise ConfigError(f'Unknown Hydra instance {key!r}; configured: {", ".join(sorted(self.instances))}') from None


def config_path(env: dict[str, str] | os._Environ[str] = os.environ) -> Path:
    if env.get('HYDRA_MCP_CONFIG'):
        return Path(env['HYDRA_MCP_CONFIG']).expanduser()
    base = Path(env.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return base / 'hydra-mcp' / 'config.toml'


def load_config(path: Path | None = None, env: dict[str, str] | os._Environ[str] = os.environ) -> Config:
    """Read the TOML config; without one, use HYDRA_URL/HYDRA_USERNAME/HYDRA_PASSWORD_FILE."""
    path = path or config_path(env)
    if path.exists():
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f'Invalid config {path}: {error}') from error
        return _from_toml(data, path)
    username = env.get('HYDRA_USERNAME') or None
    instance = Instance(name='default', url=normalize_url(env.get('HYDRA_URL') or DEFAULT_URL),
                        username=username, password_file=_path(env.get('HYDRA_PASSWORD_FILE') or None,
                                                                'HYDRA_PASSWORD_FILE', 'default'))
    return Config({'default': instance}, 'default')


def _from_toml(data: dict, path: Path) -> Config:
    raw = data.get('instances')
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f'{path} needs at least one [instances.<name>] table')
    instances: dict[str, Instance] = {}
    for name, table in raw.items():
        if not isinstance(table, dict) or not isinstance(table.get('url'), str):
            raise ConfigError(f'Instance {name!r} in {path} needs a url')
        if 'password' in table:
            raise ConfigError(f'Instance {name!r}: put the password in a file and set password_file')
        unknown = set(table) - {'url', 'username', 'username_file', 'password_file'}
        if unknown:
            raise ConfigError(f'Instance {name!r} has unknown keys: {", ".join(sorted(unknown))}')
        username = table.get('username')
        if username is not None and not isinstance(username, str):
            raise ConfigError(f'username of instance {name!r} must be a string')
        instances[name] = Instance(name=name, url=normalize_url(table['url']), username=username,
                                   username_file=_path(table.get('username_file'), 'username_file', name),
                                   password_file=_path(table.get('password_file'), 'password_file', name))
    default = data.get('default', next(iter(instances)))
    if default not in instances:
        raise ConfigError(f'default instance {default!r} is not configured in {path}')
    return Config(instances, default)
