"""ollama adapter tests: payload shape, JSON parse, error mapping."""

from __future__ import annotations

import base64

import pytest

from minion_core.adapters.llm import LlmError
from minion_core.adapters.ollama import OllamaBackend


class _Resp:
    """A minimal requests.Response double."""

    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._body


def test_text_payload_and_parse(monkeypatch):
    """A text call posts JSON-forced chat with no image, returns content."""
    import requests

    seen = {}

    def fake_post(url, json, timeout):
        seen['url'] = url
        seen['json'] = json
        return _Resp({'message': {'content': '{"props": ["Wand"]}'}})

    monkeypatch.setattr(requests, 'post', fake_post)
    out = OllamaBackend('http://ollama:11434/', 'qwen2.5vl:7b').text('hi')
    assert out == '{"props": ["Wand"]}'
    assert seen['url'] == 'http://ollama:11434/api/chat'
    assert seen['json']['model'] == 'qwen2.5vl:7b'
    assert seen['json']['format'] == 'json'
    assert seen['json']['stream'] is False
    assert 'images' not in seen['json']['messages'][0]


def test_vision_includes_base64_image(monkeypatch, tmp_path):
    """A vision call carries the image, base64-encoded."""
    import requests

    img = tmp_path / 'a.png'
    img.write_bytes(b'\x89PNG\r\n')
    seen = {}

    def fake_post(url, json, timeout):
        seen['json'] = json
        return _Resp({'message': {'content': '{}'}})

    monkeypatch.setattr(requests, 'post', fake_post)
    OllamaBackend('http://x', 'm').vision_json('classify', img)
    msg = seen['json']['messages'][0]
    assert len(msg['images']) == 1
    assert base64.b64decode(msg['images'][0]) == b'\x89PNG\r\n'


def test_network_error_is_llm_error(monkeypatch):
    """An unreachable model FAILS clean (the belt then punts)."""
    import requests

    def boom(url, json, timeout):
        raise requests.ConnectionError('connection refused')

    monkeypatch.setattr(requests, 'post', boom)
    with pytest.raises(LlmError, match='ollama_unreachable'):
        OllamaBackend('http://x', 'm').text('hi')


def test_missing_model_is_model_not_pulled(monkeypatch):
    """A 404 (server up, model absent) names the exact pull command."""
    import requests

    monkeypatch.setattr(
        requests,
        'post',
        lambda url, json, timeout: _Resp({'error': 'not found'}, status=404),
    )
    with pytest.raises(LlmError, match='model_not_pulled') as caught:
        OllamaBackend('http://x', 'qwen2.5vl:7b').text('hi')
    assert 'qwen2.5vl:7b' in str(caught.value)


def test_timeout_is_distinct_from_unreachable(monkeypatch):
    """A read timeout says 'too slow' + names the fix, not 'unreachable'."""
    import requests

    def slow(url, json, timeout):
        raise requests.ReadTimeout('read timed out')

    monkeypatch.setattr(requests, 'post', slow)
    with pytest.raises(LlmError, match='ollama_timeout') as caught:
        OllamaBackend('http://x', 'm').text('hi')
    assert 'Gemini' in str(caught.value)


def test_other_http_error_is_ollama_error(monkeypatch):
    """A 5xx is distinct from both unreachable and model-not-pulled."""
    import requests

    monkeypatch.setattr(
        requests,
        'post',
        lambda url, json, timeout: _Resp({}, status=500),
    )
    with pytest.raises(LlmError, match='ollama_error'):
        OllamaBackend('http://x', 'm').text('hi')


def test_empty_content_is_llm_error(monkeypatch):
    """A blank reply is a failure, not an empty classification."""
    import requests

    monkeypatch.setattr(
        requests,
        'post',
        lambda url, json, timeout: _Resp({'message': {'content': ' '}}),
    )
    with pytest.raises(LlmError):
        OllamaBackend('http://x', 'm').text('hi')
