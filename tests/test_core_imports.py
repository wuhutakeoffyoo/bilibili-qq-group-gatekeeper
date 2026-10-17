"""在全新进程中证明核心不依赖插件或框架，也没有导入时文件副作用。"""

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class CoreImportTests(unittest.TestCase):
    def test_core_imports_with_adapters_blocked_and_creates_no_files(self):
        repository = Path(__file__).resolve().parents[1]
        script = textwrap.dedent("""
            import importlib
            import importlib.abc
            import pkgutil
            import sys
            from pathlib import Path

            class BlockAdapters(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if any(fullname == name or fullname.startswith(name + ".")
                           for name in ("nonebot", "src.plugins")):
                        raise AssertionError("核心导入了外部适配器: " + fullname)
                    return None

            sys.meta_path.insert(0, BlockAdapters())
            sys.path.insert(0, sys.argv[1])
            core = importlib.import_module("src.gatekeeper")
            expected = {
                "src.gatekeeper.models", "src.gatekeeper.config",
                "src.gatekeeper.conditions", "src.gatekeeper.pipeline",
                "src.gatekeeper.parsing", "src.gatekeeper.formatters",
                "src.gatekeeper.decisions", "src.gatekeeper.decision_runtime",
            }
            imported = set()
            for module in pkgutil.walk_packages(core.__path__, core.__name__ + "."):
                importlib.import_module(module.name)
                imported.add(module.name)
            assert expected <= imported, "未导入全部核心模块: " + repr(expected - imported)
            assert not any(name == "nonebot" or name.startswith("nonebot.")
                           or name == "src.plugins" or name.startswith("src.plugins.")
                           for name in sys.modules), "已加载框架或插件模块"
            assert not list(Path.cwd().iterdir()), "核心导入在工作目录创建了文件"
            print("core imports are independent")
        """)

        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", script, str(repository)],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(list(Path(directory).iterdir()), [])

        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        self.assertIn("core imports are independent", result.stdout)


if __name__ == "__main__":
    unittest.main()
