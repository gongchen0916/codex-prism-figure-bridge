import json,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import windows_ppt_finalizer_status as reader
class StatusTests(unittest.TestCase):
    def test_unclaimed_job_is_read_only_and_never_imports_windows_hooks(self):
        with tempfile.TemporaryDirectory()as td:
            job=Path(td);result=reader.status(job);self.assertTrue(result['read_only']);self.assertEqual(result['finish_helper_state'],'not_claimed');self.assertEqual(result['accept_helper_state'],'not_claimed');self.assertEqual(list(job.iterdir()),[])
    def test_exact_live_and_exited_helper_states(self):
        class WindowsError(Exception):pass
        class Handle:
            def Close(self):pass
        with tempfile.TemporaryDirectory()as td:
            claim=Path(td)/'claim';claim.write_text(json.dumps({'pid':123}));api=types.SimpleNamespace(OpenProcess=lambda access,inherit,pid:Handle());process=types.SimpleNamespace(GetExitCodeProcess=lambda handle:259);errors=types.SimpleNamespace(error=WindowsError)
            with patch.dict(sys.modules,{'win32api':api,'win32process':process,'pywintypes':errors}):
                self.assertEqual(reader.helper_state(claim),'alive');process.GetExitCodeProcess=lambda handle:0;self.assertEqual(reader.helper_state(claim),'exited');claim.write_text(json.dumps({'cancelled_before_dispatch':True}));self.assertEqual(reader.helper_state(claim),'cancelled_before_dispatch')
    def test_uncertain_helper_remains_unknown(self):
        class WindowsError(Exception):pass
        def denied(*args):
            error=WindowsError();error.winerror=5;raise error
        with tempfile.TemporaryDirectory()as td:
            claim=Path(td)/'claim';claim.write_text(json.dumps({'pid':123}))
            with patch.dict(sys.modules,{'win32api':types.SimpleNamespace(OpenProcess=denied),'win32process':types.SimpleNamespace(),'pywintypes':types.SimpleNamespace(error=WindowsError)}):self.assertEqual(reader.helper_state(claim),'unknown')
if __name__=='__main__':unittest.main()
