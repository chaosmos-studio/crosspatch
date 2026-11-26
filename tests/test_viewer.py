from __future__ import annotations

import http.client
import threading
import unittest

from helpers import BridgeCase

from crosspatch import viewer
from crosspatch.project import find_project


class ViewerTest(BridgeCase):
    def setUp(self) -> None:
        super().setUp()
        self.pair()
        (self.root / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        self.cli_ok("send", "art", "--as", "maker", "--kind", "REVIEW", "--attach", "shot.png",
                    "-m", "Door **done**. <script>alert(1)</script> See `shot.png`.")
        self.server, self.url = viewer.serve([find_project(self.root)], token="tok")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, path: str, host: str | None = None) -> tuple[int, bytes, http.client.HTTPResponse]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", path, headers={"Host": host or f"127.0.0.1:{self.port}"})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body, response

    def test_binds_to_loopback_only(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertTrue(self.url.startswith(f"http://127.0.0.1:{self.port}/tok/"))

    def test_lists_threads_and_shows_messages_with_images_inline(self) -> None:
        status, body, response = self.get("/tok/")
        self.assertEqual(status, 200)
        self.assertIn(b"art", body)
        self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))
        status, body, _ = self.get("/tok/p/0/art")
        self.assertEqual(status, 200)
        self.assertIn(b"<strong>done</strong>", body)
        self.assertIn(b"&lt;script&gt;", body)
        self.assertNotIn(b"<script>", body)
        self.assertIn(b"<img class='att' src='/tok/p/0/art/0001/0'", body)
        status, image, response = self.get("/tok/p/0/art/0001/0")
        self.assertEqual((status, response.getheader("Content-Type")), (200, "image/png"))
        self.assertTrue(image.startswith(b"\x89PNG"))

    def test_refuses_other_hosts_and_wrong_tokens(self) -> None:
        self.assertEqual(self.get("/tok/", host="evil.example:80")[0], 403)
        self.assertEqual(self.get("/nope/")[0], 404)
        self.assertEqual(self.get("/")[0], 404)
        self.assertEqual(self.get("/tok/p/0/art/0001/9")[0], 404)
        self.assertEqual(self.get("/tok/p/0/../../etc")[0], 404)


class MarkdownTest(unittest.TestCase):
    def test_escapes_html_and_keeps_only_http_links(self) -> None:
        rendered = viewer.render_markdown("[ok](https://example.com) [bad](javascript:alert(1))\n\n```\n<b>x</b>\n```")
        self.assertIn('<a href="https://example.com"', rendered)
        self.assertNotIn('href="javascript', rendered)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", rendered)


if __name__ == "__main__":
    unittest.main()
