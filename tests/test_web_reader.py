import unittest
from src.web_reader import WebReader


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


if __name__ == "__main__":
    unittest.main()
