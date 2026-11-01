"""Small async client for one Hydra instance's JSON API and pages."""
from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlsplit
import asyncio
import json

import httpx

from . import __version__
from .config import Instance

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_LOG_BYTES = 512 * 1024
MAX_REDIRECTS = 5


class HydraError(Exception):
    pass


class HydraClient:
    def __init__(self, instance: Instance, http: httpx.AsyncClient | None = None, timeout: float = 60):
        self.instance = instance
        self._own_http = http is None
        self.http = http or httpx.AsyncClient(timeout=timeout, headers={'User-Agent': f'hydra-mcp/{__version__}'})
        self.logged_in = False
        self._login_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._own_http:
            await self.http.aclose()

    @property
    def origin(self) -> str:
        parts = urlsplit(self.instance.url)
        return f'{parts.scheme}://{parts.netloc}'

    def url(self, path: str) -> str:
        return self.instance.url + path

    def _same_site(self, url: str) -> bool:
        return url == self.instance.url or url.startswith(self.instance.url + '/')

    async def _send(self, method: str, url: str, *, params: dict | None = None, json_body: Any = None,
                    accept: str = 'application/json', follow: bool = True, limit: int = MAX_JSON_BYTES) -> tuple[httpx.Response, bytes]:
        headers = {'Accept': accept}
        if method != 'GET':
            # Hydra rejects state-changing requests whose Referer/Origin is not its base URL.
            headers |= {'Referer': self.instance.url + '/', 'Origin': self.origin}
        for _ in range(MAX_REDIRECTS + 1):
            if not self._same_site(url):
                raise HydraError(f'Refusing to follow a redirect off {self.instance.url}: {url}')
            # The session cookie lives in the client's jar (one client per instance).
            request = self.http.build_request(method, url, params=params, json=json_body, headers=headers)
            try:
                response = await self.http.send(request, stream=True)
            except httpx.HTTPError as error:
                raise HydraError(f'{self.instance.name}: request to {url} failed: {error}') from error
            try:
                if follow and response.is_redirect and 'location' in response.headers:
                    url = urljoin(url, response.headers['location'])
                    method, params, json_body = 'GET', None, None
                    headers = {'Accept': accept}
                    continue
                body = await _read_limited(response, limit)
                return response, body
            finally:
                await response.aclose()
        raise HydraError(f'Too many redirects from {url}')

    async def login(self) -> None:
        async with self._login_lock:
            if self.logged_in:
                return
            username, password = self.instance.credentials()
            response, body = await self._send('POST', self.url('/login'), json_body={'username': username, 'password': password})
            if response.status_code != 200:
                raise HydraError(f'Login to {self.instance.name} as {username!r} failed: {_error_text(response, body)}')
            self.logged_in = True

    async def request(self, method: str, path: str, *, params: dict | None = None, json_body: Any = None,
                      auth: bool = False, accept: str = 'application/json', follow: bool = True,
                      limit: int = MAX_JSON_BYTES) -> tuple[httpx.Response, bytes]:
        """Send a request; log in first for `auth`, and again once after a 401/403 (expired session)."""
        fresh = False
        if auth and not self.logged_in:
            await self.login()
            fresh = True
        response, body = await self._send(method, self.url(path), params=params, json_body=json_body,
                                          accept=accept, follow=follow, limit=limit)
        if response.status_code in (401, 403) and self.instance.can_login and not fresh:
            self.logged_in = False
            await self.login()
            response, body = await self._send(method, self.url(path), params=params, json_body=json_body,
                                              accept=accept, follow=follow, limit=limit)
        return response, body

    async def json(self, method: str, path: str, **kwargs: Any) -> Any:
        response, body = await self.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise HydraError(f'{self.instance.name}: {method} {path}: {_error_text(response, body)}')
        try:
            return json.loads(body) if body.strip() else None
        except ValueError as error:
            raise HydraError(f'{self.instance.name}: {path} did not return JSON (HTTP {response.status_code})') from error

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self.json('GET', path, params=params)

    async def page(self, path: str) -> str:
        response, body = await self.request('GET', path, accept='text/html', limit=MAX_PAGE_BYTES)
        if response.status_code >= 400:
            raise HydraError(f'{self.instance.name}: GET {path}: {_error_text(response, body)}')
        return body.decode('utf-8', 'replace')

    async def text(self, path: str, params: dict | None = None, limit: int = MAX_LOG_BYTES) -> str:
        response, body = await self.request('GET', path, params=params, accept='text/plain', limit=limit)
        if response.status_code >= 400:
            raise HydraError(f'{self.instance.name}: GET {path}: {_error_text(response, body)}')
        return body.decode('utf-8', 'replace')

    async def location(self, path: str) -> str | None:
        """The redirect target of `path`, without following it."""
        response, body = await self.request('GET', path, accept='text/plain', follow=False, limit=64 * 1024)
        if response.is_redirect:
            return urljoin(self.url(path), response.headers.get('location', ''))
        if response.status_code >= 400:
            raise HydraError(f'{self.instance.name}: GET {path}: {_error_text(response, body)}')
        return None

    async def action(self, path: str, method: str = 'POST', params: dict | None = None) -> str:
        """Run a privileged action that answers with a redirect back to the object page.

        POST, with the same-origin Referer every Hydra accepts: newer ones refuse
        state changes by GET, which a link from another site could trigger."""
        response, body = await self.request(method, path, params=params, auth=True, follow=False, limit=256 * 1024)
        if response.is_redirect or response.status_code == 200:
            return urljoin(self.url(path), response.headers.get('location', path))
        raise HydraError(f'{self.instance.name}: {method} {path}: {_error_text(response, body)}')


async def _read_limited(response: httpx.Response, limit: int) -> bytes:
    chunks, size = [], 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise HydraError(f'Response from {response.request.url} exceeds {limit // 1024} KiB')
        chunks.append(chunk)
    return b''.join(chunks)


def _error_text(response: httpx.Response, body: bytes) -> str:
    message = ''
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            message = str(data.get('error') or data.get('message') or '')
    except ValueError:
        pass
    if not message and body and 'html' not in response.headers.get('content-type', ''):
        message = body[:300].decode('utf-8', 'replace').strip()
    return f'HTTP {response.status_code}' + (f': {message}' if message else '')
