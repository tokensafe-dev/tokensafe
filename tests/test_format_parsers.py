import os
import json
import shutil
import tempfile
import unittest

from src.format_parsers.lockfiles import LockfileReducer
from src.format_parsers.docker_k8s import ContainerAndK8sReducer
from src.format_parsers.json_log import JSONLogReducer
from src.stash import retrieve_block


class TestFormatParsers(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_format_parsers_")
        self.old_data_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = self.test_dir

    def tearDown(self):
        if self.old_data_dir is not None:
            os.environ["TOKENSAFE_DATA_DIR"] = self.old_data_dir
        else:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_npm_lockfile_reducer(self):
        # Construct synthetic package-lock.json with 50 packages
        packages = {
            "": {"dependencies": {"express": "^4.18.2", "react": "^18.2.0"}},
            "node_modules/express": {"version": "4.18.2"},
            "node_modules/react": {"version": "18.2.0"},
        }
        for i in range(50):
            packages[f"node_modules/sub-dep-{i}"] = {"version": f"1.0.{i}"}

        raw_lock = json.dumps({"name": "my-app", "lockfileVersion": 3, "packages": packages}, indent=2)
        self.assertTrue(LockfileReducer.is_lockfile("package-lock.json"))
        reduced, was_reduced = LockfileReducer.reduce(raw_lock, "package-lock.json")
        self.assertTrue(was_reduced)
        self.assertIn("express: 4.18.2", reduced)
        self.assertIn("react: 18.2.0", reduced)
        self.assertIn("[TokenSafe folded: #", reduced)

        restored = retrieve_block(reduced)
        self.assertEqual(restored, raw_lock)

    def test_k8s_yaml_reducer(self):
        raw_yaml = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web-api
  namespace: production
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: api
        image: myorg/web-api:v2.1
---
apiVersion: v1
kind: Service
metadata:
  name: web-service
  namespace: production
spec:
  ports:
  - port: 80
""" + "\n# Extra metadata comments to reach minimum lines\n" * 20

        reduced, was_reduced = ContainerAndK8sReducer.reduce_k8s_yaml(raw_yaml)
        self.assertTrue(was_reduced)
        self.assertIn("**Deployment**: `web-api` (production)", reduced)
        self.assertIn("**Service**: `web-service` (production)", reduced)
        self.assertIn("[TokenSafe folded: #", reduced)

    def test_json_log_reducer(self):
        log_lines = []
        for i in range(30):
            log_lines.append(json.dumps({"level": "info", "msg": f"Request {i} completed", "time": "2026-11-16T00:00:00Z"}))
        log_lines.append(json.dumps({"level": "error", "msg": "Database connection refused", "time": "2026-11-16T00:00:01Z"}))
        log_lines.append(json.dumps({"level": "warn", "msg": "High memory pressure", "time": "2026-11-16T00:00:02Z"}))

        raw_stream = "\n".join(log_lines)
        self.assertTrue(JSONLogReducer.is_ndjson_log(raw_stream))

        reduced, was_reduced = JSONLogReducer.reduce(raw_stream)
        self.assertTrue(was_reduced)
        self.assertIn("Database connection refused", reduced)
        self.assertIn("High memory pressure", reduced)
        self.assertNotIn("Request 15 completed", reduced)
        self.assertIn("[TokenSafe folded: #", reduced)


if __name__ == "__main__":
    unittest.main()
