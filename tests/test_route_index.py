"""Route → handler resolution for `smart_read_file(endpoint=...)`.

The measured matrix spent 6.3 discovery calls per edit because a route target
could not be resolved to a file: the model searched, read the registration file,
then read the handler's file. These tests pin the one-call contract: route in,
path + line range + whole-file version + handler body out.
"""

import os
import tempfile
import unittest
import uuid

from src import route_index as ri


GO_APP = """package demo

import "net/http"

func NewApp(mux *http.ServeMux, store *Store) *http.ServeMux {
\tmux.HandleFunc("/api/products", func(w http.ResponseWriter, r *http.Request) {
\t\tstore.ListProducts(w)
\t})
\tmux.HandleFunc("/api/products/{id}", func(w http.ResponseWriter, r *http.Request) {
\t\tstore.GetProduct(w, r)
\t})
\tmux.HandleFunc("/api/orders/{id}/cancel", cancelOrder(store))
\treturn mux
}
"""

GO_HANDLERS = """package demo

import "net/http"

func cancelOrder(store *Store) http.HandlerFunc {
\treturn func(w http.ResponseWriter, r *http.Request) {
\t\tstore.CancelOrder(w, r)
\t}
}
"""

EXPRESS_APP = """const express = require('express');
const app = express();
app.post('/api/products', async (req, res) => {
  const created = await store.createProduct(req.body);
  res.status(201).json(created);
});
module.exports = app;
"""

FASTAPI_APP = '''from fastapi import FastAPI

app = FastAPI()


@app.get("/api/products")
def list_products():
    return db.list_products()


@app.post("/api/products", status_code=201)
def create_product(payload: dict):
    return db.create_product(payload)
'''

FLASK_APP = '''from flask import Flask

app = Flask(__name__)


@app.route("/api/products", methods=["POST"])
def create_product():
    return {"ok": True}
'''


class TestParseEndpoint(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(ri.parse_endpoint("POST /api/x"), ("POST", "/api/x"))
        self.assertEqual(ri.parse_endpoint("post /api/x"), ("POST", "/api/x"))
        self.assertEqual(ri.parse_endpoint("get:/api/x"), ("GET", "/api/x"))
        self.assertEqual(ri.parse_endpoint("/api/x"), ("", "/api/x"))
        self.assertIsNone(ri.parse_endpoint('endpoint="POST /api/x"'))

    def test_non_endpoints(self):
        for value in ("", "CreateProduct", "Type.Method", "127.0.0.1", "users.find"):
            self.assertIsNone(ri.parse_endpoint(value), value)


class TestRouteMatches(unittest.TestCase):
    def test_templates(self):
        self.assertEqual(ri.route_matches("/api/orders/{id}/cancel", "/api/orders/7/cancel"), (True, False))
        self.assertEqual(ri.route_matches("/api/orders/:id/cancel", "/api/orders/7/cancel"), (True, False))
        self.assertEqual(ri.route_matches("/api/products", "/api/products"), (True, True))
        self.assertFalse(ri.route_matches("/api/products", "/api/orders")[0])
        self.assertFalse(ri.route_matches("/api/products/{id}", "/api/products")[0])


class TestFindRegistrations(unittest.TestCase):
    def _tree(self, files):
        tmp = tempfile.mkdtemp(prefix="ts-route-")
        for name, text in files.items():
            path = os.path.join(tmp, name)
            os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        return tmp

    def test_go_inline_handler(self):
        root = self._tree({"app.go": GO_APP})
        hits, scanned = ri.find_registrations(root, "GET", "/api/products")
        self.assertEqual(scanned, 1)
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual((hit.file, hit.handler_kind), ("app.go", "inline"))
        self.assertTrue(hit.exact)
        self.assertIn("ListProducts", hit.callees())

    def test_template_target_resolves_concrete_id(self):
        root = self._tree({"app.go": GO_APP})
        hits, _ = ri.find_registrations(root, "GET", "/api/products/42")
        self.assertEqual(len(hits), 1)
        self.assertFalse(hits[0].exact)
        self.assertIn("{id}", hits[0].route)

    def test_named_handler_and_verb_only_paths(self):
        root = self._tree({"app.go": GO_APP, "handlers.go": GO_HANDLERS})
        hits, _ = ri.find_registrations(root, "POST", "/api/orders/9/cancel")
        self.assertEqual(len(hits), 1)
        # `cancelOrder(store)` is a factory: the function name is what resolves
        self.assertEqual(hits[0].handler_kind, "named")
        self.assertEqual(hits[0].handler, "cancelOrder")
        # the registration statement has no handler body to scan for callees
        self.assertEqual(hits[0].callees(), [])

    def test_express(self):
        root = self._tree({"app.js": EXPRESS_APP})
        hits, _ = ri.find_registrations(root, "POST", "/api/products")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].via, "app.post")
        self.assertEqual(hits[0].handler_kind, "inline")
        self.assertIn("createProduct", hits[0].callees())

    def test_fastapi_decorators(self):
        root = self._tree({"main.py": FASTAPI_APP})
        hits, _ = ri.find_registrations(root, "POST", "/api/products")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].handler_kind, "named")
        self.assertEqual(hits[0].handler, "create_product")
        self.assertGreater(hits[0].end_line, hits[0].line)

    def test_flask_methods_keyword(self):
        root = self._tree({"app.py": FLASK_APP})
        hits, _ = ri.find_registrations(root, "POST", "/api/products")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].handler, "create_product")

    def test_wrong_verb_is_not_a_hit(self):
        root = self._tree({"app.js": EXPRESS_APP})
        hits, _ = ri.find_registrations(root, "GET", "/api/products")
        self.assertEqual(hits, [])

    def test_same_file_under_two_spellings_is_reported_once(self):
        root = self._tree({"app.go": GO_APP})
        files = ["app.go", os.path.join(".", "app.go"), "app.go"]
        hits, _ = ri.find_registrations(root, "GET", "/api/products", files=files)
        self.assertEqual(len(hits), 1)


class TestSmartReadFileEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ts-endpoint-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self._old_env = {k: os.environ.get(k) for k in ("TOKENSAFE_DATA_DIR", "TOKENSAFE_DB_PATH")}
        os.environ["TOKENSAFE_DATA_DIR"] = self.tmp
        os.environ["TOKENSAFE_DB_PATH"] = os.path.join(self.tmp, "metrics.db")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write(self, name, text):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_endpoint_without_path_resolves_across_files(self):
        from src.mcp_server import handle_tool_call
        from src.session_context import session_scope
        self._write("app.go", GO_APP)
        self._write("handlers.go", GO_HANDLERS)
        self._write("store.go", "package demo\n\nfunc (s *Store) ListProducts(w http.ResponseWriter) {\n\tw.WriteHeader(200)\n}\n")
        with session_scope(root=self.tmp, session_id=f"test:ep:{uuid.uuid4().hex[:8]}"):
            res = handle_tool_call("smart_read_file", {"endpoint": "GET /api/products"})
        txt = res["content"][0]["text"]
        self.assertNotIn("isError", res)
        self.assertIn("TS endpoint", txt)
        self.assertIn("app.go", txt)
        self.assertRegex(txt, r"ver=[0-9a-f]{12}")
        self.assertIn("ListProducts", txt)
        # the cross-file callee is named so the follow-up read is a direct hit
        self.assertIn("calls:", txt)
        self.assertIn("store.go", txt)

    def test_unknown_route_names_the_verbs_that_exist(self):
        from src.mcp_server import handle_tool_call
        from src.session_context import session_scope
        self._write("app.js", EXPRESS_APP)
        with session_scope(root=self.tmp, session_id=f"test:ep:{uuid.uuid4().hex[:8]}"):
            res = handle_tool_call("smart_read_file", {"endpoint": "DELETE /api/products"})
        txt = res["content"][0]["text"]
        self.assertIn("no registration", txt)
        self.assertIn("POST /api/products", txt)

    def test_symbol_declared_elsewhere_is_inlined(self):
        from src.mcp_server import handle_tool_call
        from src.session_context import session_scope
        app = self._write("app.go", GO_APP)
        self._write("store.go", "package demo\n\nfunc (s *Store) ListProducts(w http.ResponseWriter) {\n\tw.WriteHeader(200)\n}\n")
        with session_scope(root=self.tmp, session_id=f"test:ep:{uuid.uuid4().hex[:8]}"):
            res = handle_tool_call("smart_read_file", {"path": app, "symbol": "ListProducts"})
        txt = res["content"][0]["text"]
        self.assertIn("store.go", txt)
        self.assertIn("w.WriteHeader(200)", txt)
        self.assertNotIn("call smart_read_file(", txt)


if __name__ == "__main__":
    unittest.main()
