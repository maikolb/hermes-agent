from __future__ import annotations
import importlib.util, sys, unittest
from pathlib import Path
PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"

def load_plugin():
    name="clean_root_guard_test_subject"; spec=importlib.util.spec_from_file_location(name,PLUGIN); module=importlib.util.module_from_spec(spec); sys.modules[name]=module; assert spec.loader; spec.loader.exec_module(module); return module
class FakeContext:
    def __init__(self): self.hooks={}
    def register_hook(self,name,callback): self.hooks[name]=callback
class CleanRootGuardTests(unittest.TestCase):
    def setUp(self): self.guard=load_plugin()
    def test_registers_hooks(self):
        ctx=FakeContext(); self.guard.register(ctx); self.assertEqual(set(ctx.hooks),{"pre_llm_call","pre_tool_call"})
    def test_rejected_root_mutation_is_blocked(self):
        r=self.guard._pre_tool_call("write_file",{"path":"C:/Users/maiko/Projetos/Hermes Project Ops/app.py","content":"x"}); self.assertEqual(r["action"],"block")
    def test_rejected_runtime_marker_is_blocked(self):
        r=self.guard._pre_tool_call("execute_code",{"code":"serve(port=4312); mode='PROJECT_OPS_TEST_MODE'"}); self.assertEqual(r["action"],"block")
    def test_documentation_can_name_historical_marker(self):
        r=self.guard._pre_tool_call("write_file",{"path":"C:/docs/history.md","content":"PROJECT_OPS_TEST_MODE and localhost:4312 were rejected"}); self.assertIsNone(r)
    def test_new_clean_root_is_allowed(self):
        r=self.guard._pre_tool_call("write_file",{"path":"C:/Users/maiko/Projetos/NewHermes/app.py","content":"print('clean')"}); self.assertIsNone(r)
if __name__=="__main__": unittest.main(verbosity=2)
