from __future__ import annotations

import unittest

import requests

from block_detector.http import HttpResponseError, JsonHttpClient


def response(status: int, url: str, body: bytes = b"{}") -> requests.Response:
    value = requests.Response()
    value.status_code = status
    value.url = url
    value._content = body
    value.encoding = "utf-8"
    return value


class HttpTests(unittest.TestCase):
    def test_http_errors_strip_query_secrets_and_userinfo(self) -> None:
        value = response(
            401,
            "https://rpc-user:rpc-password@example.test/path"
            "?apiKey=news-secret&key=blockchair-secret",
        )
        with self.assertRaises(HttpResponseError) as raised:
            JsonHttpClient._decode(value)
        message = str(raised.exception)
        self.assertEqual(message, "HTTP 401 from https://example.test/path")
        self.assertNotIn("secret", message)
        self.assertNotIn("rpc-user", message)

    def test_invalid_json_errors_strip_query_parameters(self) -> None:
        value = response(
            200,
            "https://example.test/data?token=secret",
            b"not-json",
        )
        with self.assertRaises(HttpResponseError) as raised:
            JsonHttpClient._decode(value)
        self.assertEqual(
            str(raised.exception),
            "Invalid JSON response from https://example.test/data",
        )

    def test_transport_errors_do_not_repeat_prepared_secret_urls(self) -> None:
        class FailingSession:
            def get(self, url, **kwargs):
                raise requests.ConnectionError(
                    "failed https://example.test/data?apiKey=transport-secret"
                )

        client = JsonHttpClient(session=FailingSession())
        with self.assertRaises(HttpResponseError) as raised:
            client.get(
                "https://rpc-user:rpc-password@example.test/data"
                "?apiKey=url-secret",
                params={"key": "params-secret"},
            )
        message = str(raised.exception)
        self.assertEqual(
            message,
            "ConnectionError while requesting https://example.test/data",
        )
        self.assertNotIn("secret", message)
        self.assertNotIn("rpc-user", message)

    def test_url_redaction_handles_invalid_ports_without_userinfo_leak(self) -> None:
        class FailingSession:
            def get(self, url, **kwargs):
                raise requests.exceptions.InvalidURL("invalid port")

        client = JsonHttpClient(session=FailingSession())
        with self.assertRaises(HttpResponseError) as raised:
            client.get(
                "https://rpc-user:rpc-password@example.test:notaport/data"
                "?apiKey=secret"
            )
        self.assertEqual(
            str(raised.exception),
            "InvalidURL while requesting "
            "https://example.test:notaport/data",
        )


if __name__ == "__main__":
    unittest.main()
