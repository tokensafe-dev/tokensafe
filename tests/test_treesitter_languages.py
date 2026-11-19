"""
Unit tests for Tree-sitter multi-language support (Java, PHP, Kotlin) in TokenSafe.
"""

import unittest
import os
import shutil
import subprocess
import sys
import tempfile
from src.treesitter_backend import TreeSitterBackend
from src.graph_engine import CodeGraph


class TestTreeSitterLanguages(unittest.TestCase):

    def test_java_parsing_and_skeleton(self):
        java_code = """package com.example;

import java.util.List;

public class PaymentService {
    private final Gateway gateway;

    public PaymentService(Gateway gateway) {
        this.gateway = gateway;
    }

    public boolean processPayment(String orderId, double amount) {
        System.out.println("Processing " + orderId);
        return gateway.charge(orderId, amount);
    }
}
"""
        data = TreeSitterBackend.extract_graph_data(java_code, ".java", "PaymentService.java")
        self.assertIsNotNone(data)
        names = [s["name"] for s in data["symbols"]]
        self.assertIn("PaymentService", names)
        self.assertIn("processPayment", names)
        calls = [c[1] for c in data["calls"]]
        self.assertTrue(any("charge" in c for c in calls))

        skeleton = TreeSitterBackend.skeletonize_braces(java_code, ".java")
        self.assertIn("public class PaymentService {", skeleton)
        self.assertIn("/* ... */", skeleton)

    def test_php_parsing_and_skeleton(self):
        php_code = """<?php
namespace App\\Http\\Controllers;

use App\\Models\\User;

class UserController {
    public function show(int $id): User {
        $user = User::findOrFail($id);
        return $user;
    }
}
"""
        data = TreeSitterBackend.extract_graph_data(php_code, ".php", "UserController.php")
        self.assertIsNotNone(data)
        names = [s["name"] for s in data["symbols"]]
        self.assertIn("UserController", names)
        self.assertIn("show", names)
        calls = [c[1] for c in data["calls"]]
        self.assertTrue(any("findOrFail" in c for c in calls))

        skeleton = TreeSitterBackend.skeletonize_braces(php_code, ".php")
        self.assertIn("class UserController {", skeleton)
        self.assertIn("/* ... */", skeleton)

    def test_kotlin_parsing_and_skeleton(self):
        kt_code = """package com.example.store

class CartManager(val storage: Storage) {
    fun checkout(cartId: String): Boolean {
        validateCart(cartId)
        return storage.save(cartId)
    }
}
"""
        data = TreeSitterBackend.extract_graph_data(kt_code, ".kt", "CartManager.kt")
        self.assertIsNotNone(data)
        names = [s["name"] for s in data["symbols"]]
        self.assertIn("CartManager", names)
        self.assertIn("checkout", names)
        calls = [c[1] for c in data["calls"]]
        self.assertTrue(any("save" in c or "validateCart" in c for c in calls))

        skeleton = TreeSitterBackend.skeletonize_braces(kt_code, ".kt")
        self.assertIn("class CartManager", skeleton)
        self.assertIn("/* ... */", skeleton)

    def test_csharp_parsing_and_skeleton(self):
        cs_code = """namespace Company.App
{
    public class OrderProcessor
    {
        public bool ExecuteOrder(string id)
        {
            Validate(id);
            return Save(id);
        }
    }
}
"""
        data = TreeSitterBackend.extract_graph_data(cs_code, ".cs", "OrderProcessor.cs")
        self.assertIsNotNone(data)
        names = [s["name"] for s in data["symbols"]]
        self.assertIn("OrderProcessor", names)
        self.assertIn("ExecuteOrder", names)
        calls = [c[1] for c in data["calls"]]
        self.assertTrue(any("Save" in c or "Validate" in c for c in calls))

        skeleton = TreeSitterBackend.skeletonize_braces(cs_code, ".cs")
        self.assertIn("public class OrderProcessor", skeleton)
        self.assertIn("/* ... */", skeleton)

    def test_graph_engine_with_polyglot_repo(self):
        tmp_dir = tempfile.mkdtemp(prefix="tokensafe_polyglot_")
        try:
            java_path = os.path.join(tmp_dir, "App.java")
            with open(java_path, "w") as f:
                f.write("public class App { public static void main(String[] args) { System.out.println(1); } }\n")

            php_path = os.path.join(tmp_dir, "index.php")
            with open(php_path, "w") as f:
                f.write("<?php class Router { public function dispatch() { run(); } }\n")

            kt_path = os.path.join(tmp_dir, "Main.kt")
            with open(kt_path, "w") as f:
                f.write("class Runner { fun start() { execute(); } }\n")

            cs_path = os.path.join(tmp_dir, "Service.cs")
            with open(cs_path, "w") as f:
                f.write("namespace Test { public class Service { public void DoWork() { Work(); } } }\n")

            graph = CodeGraph(tmp_dir)
            count = graph.scan()

            self.assertEqual(count, 4)
            self.assertIn("App.java", graph.file_symbols)
            self.assertIn("index.php", graph.file_symbols)
            self.assertIn("Main.kt", graph.file_symbols)
            self.assertIn("Service.cs", graph.file_symbols)

            java_syms = [s.split("::")[-1] for s in graph.file_symbols["App.java"]]
            php_syms = [s.split("::")[-1] for s in graph.file_symbols["index.php"]]
            kt_syms = [s.split("::")[-1] for s in graph.file_symbols["Main.kt"]]
            cs_syms = [s.split("::")[-1] for s in graph.file_symbols["Service.cs"]]

            self.assertIn("App", java_syms)
            self.assertIn("Router", php_syms)
            self.assertIn("Runner", kt_syms)
            self.assertIn("Service", cs_syms)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_c_parsing_and_skeleton(self):
        c_code = """#include <stdio.h>

int compute_sum(int a, int b) {
    printf("sum: %d\\n", a + b);
    return a + b;
}
"""
        data = TreeSitterBackend.extract_graph_data(c_code, ".c", "math.c")
        self.assertIsNotNone(data)
        names = [s["name"] for s in data["symbols"]]
        self.assertIn("compute_sum", names)
        calls = [c[1] for c in data["calls"]]
        self.assertTrue(any("printf" in c for c in calls))

        skeleton = TreeSitterBackend.skeletonize_braces(c_code, ".c")
        self.assertIn("int compute_sum", skeleton)
        self.assertIn("/* ... */", skeleton)

    def test_cpp_parsing_and_skeleton(self):
        cpp_code = """#include <iostream>

class Engine {
public:
    void start() {
        std::cout << "Engine started" << std::endl;
    }
};
"""
        data = TreeSitterBackend.extract_graph_data(cpp_code, ".cpp", "engine.cpp")
        self.assertIsNotNone(data)
        names = [s["name"] for s in data["symbols"]]
        self.assertIn("Engine", names)
        self.assertIn("start", names)

        skeleton = TreeSitterBackend.skeletonize_braces(cpp_code, ".cpp")
        self.assertIn("class Engine {", skeleton)
        self.assertIn("void start() { /* ... */ }", skeleton)

    def test_ruby_parsing_and_skeleton(self):
        rb_code = """class AccountService
  def transfer(from_id, to_id, amount)
    log_event("transfer", amount)
    return true
  end
end
"""
        data = TreeSitterBackend.extract_graph_data(rb_code, ".rb", "account.rb")
        self.assertIsNotNone(data)
        names = [s["name"] for s in data["symbols"]]
        self.assertIn("AccountService", names)
        self.assertIn("transfer", names)
        calls = [c[1] for c in data["calls"]]
        self.assertTrue(any("log_event" in c for c in calls))

        skeleton = TreeSitterBackend.skeletonize_braces(rb_code, ".rb")
        self.assertIn("class AccountService", skeleton)

    def test_swift_parsing_and_skeleton(self):
        swift_code = """import Foundation

class NetworkClient {
    func executeRequest(url: String) -> Bool {
        print(url)
        return true
    }
}
"""
        data = TreeSitterBackend.extract_graph_data(swift_code, ".swift", "client.swift")
        self.assertIsNotNone(data)
        names = [s["name"] for s in data["symbols"]]
        self.assertIn("NetworkClient", names)
        self.assertIn("executeRequest", names)

        skeleton = TreeSitterBackend.skeletonize_braces(swift_code, ".swift")
        self.assertIn("class NetworkClient {", skeleton)
        self.assertIn("/* ... */", skeleton)

    def test_check_all_languages(self):
        status = TreeSitterBackend.check_all_languages()
        self.assertEqual(len(status), 13)
        for lang, ok in status.items():
            self.assertTrue(ok, f"Expected grammar for {lang} to be available")
        self.assertEqual(TreeSitterBackend.get_missing_languages(), [])

    def test_isolated_mode_recovers_user_site_grammars(self):
        """`python -I` hides user site-packages where grammar wheels live; the
        backend must probe and recover them instead of silently degrading."""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        snippet = (
            "import sys; sys.path.insert(0, {root!r}); sys.path.insert(0, {root!r} + '/src'); "
            "from treesitter_backend import TreeSitterBackend; "
            "print(sum(1 for v in TreeSitterBackend.check_all_languages().values() if v))"
        ).format(root=repo_root)
        expected = subprocess.run(
            [sys.executable, "-c", snippet],
            cwd=repo_root, capture_output=True, text=True, timeout=120,
        )
        isolated = subprocess.run(
            [sys.executable, "-I", "-c", snippet],
            cwd=repo_root, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(expected.returncode, 0, expected.stderr)
        self.assertEqual(isolated.returncode, 0, isolated.stderr)
        self.assertEqual(
            expected.stdout.strip(),
            isolated.stdout.strip(),
            "grammar visibility must be identical in normal and isolated modes",
        )
        if expected.stdout.strip() != "0":
            self.assertGreater(int(isolated.stdout.strip() or 0), 0)


if __name__ == "__main__":
    unittest.main()
