import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
import time
import tempfile
import unittest
from unittest.mock import patch

from src.schema_slicer import SchemaSlicer


class TestSchemaContracts(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.orig_data_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = self.tmp_dir.name
        SchemaSlicer.clear_memory_cache()

    def tearDown(self):
        SchemaSlicer.clear_memory_cache()
        if self.orig_data_dir is not None:
            os.environ["TOKENSAFE_DATA_DIR"] = self.orig_data_dir
        else:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        self.tmp_dir.cleanup()

    def test_single_file_prisma_slice(self):
        prisma_content = """
datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}

model User {
  id        Int       @id @default(autoincrement())
  email     String    @unique
  profile   Profile?
  posts     Post[]
}

model Profile {
  id     Int    @id @default(autoincrement())
  bio    String
  user   User   @relation(fields: [userId], references: [id])
  userId Int    @unique
}

model Post {
  id        Int      @id @default(autoincrement())
  title     String
  author    User     @relation(fields: [authorId], references: [id])
  authorId  Int
}
"""
        prisma_path = os.path.join(self.tmp_dir.name, "schema.prisma")
        with open(prisma_path, "w", encoding="utf-8") as f:
            f.write(prisma_content)

        # Slice model User with 1-hop relations
        sliced, info = SchemaSlicer.slice_file(prisma_path, target="User")
        self.assertEqual(info["kind"], "prisma")
        self.assertIn("model User", sliced)
        self.assertIn("model Profile", sliced)
        self.assertIn("model Post", sliced)
        self.assertNotIn("datasource db", sliced)

    def test_multi_file_prisma_composite_fingerprint(self):
        # Create multi-file prisma structure: prisma/schema/{user.prisma, post.prisma}
        schema_dir = os.path.join(self.tmp_dir.name, "prisma", "schema")
        os.makedirs(schema_dir, exist_ok=True)

        user_file = os.path.join(schema_dir, "user.prisma")
        with open(user_file, "w", encoding="utf-8") as f:
            f.write("model User { id Int @id }")

        post_file = os.path.join(schema_dir, "post.prisma")
        with open(post_file, "w", encoding="utf-8") as f:
            f.write("model Post { id Int @id }")

        fp1 = SchemaSlicer.compute_schema_fingerprint(user_file, "prisma")
        self.assertIsNotNone(fp1)

        # Invalidate cache by modifying post.prisma (simulating dirty edit)
        time.sleep(0.02)
        with open(post_file, "a", encoding="utf-8") as f:
            f.write("\nmodel Comment { id Int @id }")

        fp2 = SchemaSlicer.compute_schema_fingerprint(user_file, "prisma")
        self.assertNotEqual(fp1, fp2, "Composite fingerprint must change when sibling file in prisma/schema/ changes")

    def test_openapi_ref_composite_fingerprint(self):
        # Create root openapi.json and referenced models/user.json
        models_dir = os.path.join(self.tmp_dir.name, "models")
        os.makedirs(models_dir, exist_ok=True)

        user_model = os.path.join(models_dir, "user.json")
        with open(user_model, "w", encoding="utf-8") as f:
            json.dump({"type": "object", "properties": {"id": {"type": "string"}}}, f)

        root_api = os.path.join(self.tmp_dir.name, "openapi.json")
        with open(root_api, "w", encoding="utf-8") as f:
            json.dump({
                "openapi": "3.0.0",
                "paths": {
                    "/users": {
                        "get": {
                            "responses": {
                                "200": {
                                    "schema": {"$ref": "./models/user.json"}
                                }
                            }
                        }
                    }
                }
            }, f)

        fp1 = SchemaSlicer.compute_schema_fingerprint(root_api, "openapi")

        # Invalidate referenced model
        time.sleep(0.02)
        with open(user_model, "w", encoding="utf-8") as f:
            json.dump({"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}}}, f)

        fp2 = SchemaSlicer.compute_schema_fingerprint(root_api, "openapi")
        self.assertNotEqual(fp1, fp2, "OpenAPI fingerprint must change when referenced $ref file changes")

    def test_schema_fingerprint_stable_across_mtime_change(self):
        schema_dir = os.path.join(self.tmp_dir.name, "prisma_mtime", "schema")
        os.makedirs(schema_dir, exist_ok=True)
        user_file = os.path.join(schema_dir, "user.prisma")
        with open(user_file, "w", encoding="utf-8") as f:
            f.write("model User { id Int @id }")
        post_file = os.path.join(schema_dir, "post.prisma")
        with open(post_file, "w", encoding="utf-8") as f:
            f.write("model Post { id Int @id }")

        fp1 = SchemaSlicer.compute_schema_fingerprint(user_file, "prisma")

        # Change mtime artificially (simulate touch or git clone on another machine)
        future_time = time.time() + 3600
        os.utime(user_file, (future_time, future_time))
        os.utime(post_file, (future_time, future_time))

        fp2 = SchemaSlicer.compute_schema_fingerprint(user_file, "prisma")
        self.assertEqual(fp1, fp2, "Fingerprint must be deterministic based on content, not mtime")

    def test_l1_l2_caching_and_l3_team_sync(self):
        graphql_path = os.path.join(self.tmp_dir.name, "schema.graphql")
        with open(graphql_path, "w", encoding="utf-8") as f:
            f.write("type Query { me: User }\ntype User { id: ID! name: String! }")

        # 1. Live Parse
        sliced1, info1 = SchemaSlicer.slice_file(graphql_path, target="User")
        self.assertEqual(info1["source"], "parsed_live")

        # 2. L1 Memory Hit
        sliced2, info2 = SchemaSlicer.slice_file(graphql_path, target="User")
        self.assertEqual(info2["source"], "L1_memory")

        # 3. Clear L1 to test L2 SQLite hit
        SchemaSlicer.clear_memory_cache()
        sliced3, info3 = SchemaSlicer.slice_file(graphql_path, target="User")
        self.assertEqual(info3["source"], "L2_sqlite")

    @patch("src.team_cache.TeamCache.get")
    def test_l3_team_cache_hit(self, mock_team_get):
        mock_team_get.return_value = json.dumps({
            "sliced": "model Account { id String @id }",
            "info": {"kind": "prisma", "target": "Account"}
        })

        prisma_path = os.path.join(self.tmp_dir.name, "schema.prisma")
        with open(prisma_path, "w", encoding="utf-8") as f:
            f.write("model Dummy { id Int @id }")

        sliced, info = SchemaSlicer.slice_file(prisma_path, target="Account")
        self.assertEqual(info["source"], "L3_team_cache")
        self.assertIn("model Account", sliced)


if __name__ == "__main__":
    unittest.main()
