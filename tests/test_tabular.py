import os
import json
import shutil
import tempfile
import unittest

from src.format_parsers.tabular import TabularReducer
from src.stash import retrieve_block


class TestTabularReducer(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tokensafe_tabular_test_")
        self.old_data_dir = os.environ.get("TOKENSAFE_DATA_DIR")
        os.environ["TOKENSAFE_DATA_DIR"] = self.test_dir

    def tearDown(self):
        if self.old_data_dir is not None:
            os.environ["TOKENSAFE_DATA_DIR"] = self.old_data_dir
        else:
            os.environ.pop("TOKENSAFE_DATA_DIR", None)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_reduce_csv(self):
        csv_lines = ["id,name,email,active"]
        for i in range(100):
            csv_lines.append(f"{i},User {i},user{i}@example.com,true")
        raw_csv = "\n".join(csv_lines)

        reduced, was_reduced = TabularReducer.reduce_csv(raw_csv)
        self.assertTrue(was_reduced)
        self.assertIn("[TokenSafe CSV Summary: 100 rows × 4 columns]", reduced)
        self.assertIn("User 0", reduced)
        self.assertIn("User 9", reduced)
        self.assertNotIn("User 50", reduced)
        self.assertIn("[TokenSafe folded: #", reduced)

        restored = retrieve_block(reduced)
        self.assertEqual(restored, raw_csv)

    def test_reduce_sql_dump(self):
        sql_lines = [
            "CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(100));",
            "CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, amount DECIMAL);",
        ]
        for i in range(50):
            sql_lines.append(f"INSERT INTO users VALUES ({i}, 'Alice {i}');")
        for i in range(50):
            sql_lines.append(f"INSERT INTO orders VALUES ({i}, {i}, 99.9);")
        raw_sql = "\n".join(sql_lines)

        reduced, was_reduced = TabularReducer.reduce_sql_dump(raw_sql)
        self.assertTrue(was_reduced)
        self.assertIn("CREATE TABLE users", reduced)
        self.assertIn("CREATE TABLE orders", reduced)
        self.assertIn("`users`: 50 rows folded", reduced)
        self.assertIn("`orders`: 50 rows folded", reduced)
        self.assertIn("[TokenSafe folded: #", reduced)

    def test_reduce_har(self):
        har_data = {
            "log": {
                "entries": [
                    {
                        "request": {"method": "GET", "url": f"https://api.example.com/v1/users/{i}"},
                        "response": {"status": 200, "bodySize": 1024},
                        "time": 45
                    }
                    for i in range(20)
                ]
            }
        }
        raw_har = json.dumps(har_data)
        reduced, was_reduced = TabularReducer.reduce_har(raw_har)
        self.assertTrue(was_reduced)
        self.assertIn("[TokenSafe HAR Archive Summary: 20 HTTP transactions]", reduced)
        self.assertIn("`[GET]` 200", reduced)
        self.assertIn("[TokenSafe folded: #", reduced)


if __name__ == "__main__":
    unittest.main()
