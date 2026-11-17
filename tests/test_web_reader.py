import unittest
import urllib.error
from src.web_reader import WebReader, _SafeRedirectHandler


class TestWebReader(unittest.TestCase):
    def test_html_to_markdown_basic(self):
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Test Page</title>
            <style>body { color: red; }</style>
            <script>alert('spam');</script>
        </head>
        <body>
            <nav><a href="/home">Home</a><a href="/about">About</a></nav>
            <header>Site Header</header>
            <article>
                <h1>Article Title</h1>
                <p>This is the <strong>main content</strong> of the article.</p>
                <p>Read the <a href="https://example.com/docs">documentation</a> for details.</p>
                <pre><code>def hello():\n    return "world"</code></pre>
                <ul>
                    <li>Feature 1</li>
                    <li>Feature 2</li>
                </ul>
            </article>
            <footer>Copyright 2026</footer>
        </body>
        </html>
        """
        md, _ = WebReader.html_to_markdown(html)
        self.assertNotIn("alert('spam')", md)
        self.assertNotIn("Site Header", md)
        self.assertNotIn("Copyright 2026", md)
        self.assertIn("# Article Title", md)
        self.assertIn("main content", md)
        self.assertIn("[documentation](https://example.com/docs)", md)
        self.assertIn("def hello():", md)
        self.assertIn("- Feature 1", md)

    def test_ssrf_safety(self):
        self.assertFalse(WebReader.is_safe_url("http://localhost:8080/admin"))
        self.assertFalse(WebReader.is_safe_url("http://127.0.0.1/secret"))
        self.assertFalse(WebReader.is_safe_url("http://169.254.169.254/latest/meta-data"))
        self.assertFalse(WebReader.is_safe_url("ftp://example.com"))
        self.assertTrue(WebReader.is_safe_url("https://docs.python.org/3/library/ast.html"))

    def test_ssrf_private_ranges_exact(self):
        """Exact IP-range checks: the old string-prefix rule missed 172.17-172.31 and IPv6."""
        self.assertFalse(WebReader.is_safe_url("http://172.16.5.1/x"))   # docker bridge
        self.assertFalse(WebReader.is_safe_url("http://172.17.0.1/x"))   # was MISSED before
        self.assertFalse(WebReader.is_safe_url("http://172.31.255.9/x"))
        self.assertFalse(WebReader.is_safe_url("http://10.1.2.3/x"))
        self.assertFalse(WebReader.is_safe_url("http://192.168.1.1/x"))
        self.assertFalse(WebReader.is_safe_url("http://[fe80::1]/x"))    # link-local IPv6
        self.assertFalse(WebReader.is_safe_url("http://[fc00::1]/x"))    # IPv6 ULA
        self.assertFalse(WebReader.is_safe_url("http://[::1]/x"))
        self.assertTrue(WebReader.is_safe_url("http://8.8.8.8/x"))       # public literal stays fetchable
        self.assertTrue(WebReader.is_safe_url("http://172.32.0.1/x"))    # public 172.32/4

    def test_redirect_to_private_host_is_blocked(self):
        handler = _SafeRedirectHandler()
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1:9222/json")
        # Public redirect target passes through (super() handles it; just verify no raise here
        # by checking is_safe_url agrees).
        self.assertTrue(WebReader.is_safe_url("https://example.com/docs"))


if __name__ == "__main__":
    unittest.main()
