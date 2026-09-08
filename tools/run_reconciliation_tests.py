"""Run candidate tests with synthetic data, no runtime workers or external I/O.

Linux /app filesystem literals are relocated in memory for portable tests;
source files on disk remain unchanged. No production lifespan is started.
"""
import ast
import importlib.abc
import importlib.util
import os
from pathlib import Path
import socket
import sys
import tempfile
from contextlib import asynccontextmanager

ROOT=Path(__file__).resolve().parents[1]
os.chdir(ROOT)
APP=ROOT/'app'
sys.path.insert(0,str(APP))
with tempfile.TemporaryDirectory(prefix='anyaicam-candidate-') as temp:
    (Path(temp)/'static').mkdir()
    for name in list(os.environ):
        if name.startswith(('ANYAICAM_', 'AWS_', 'CAMERA')): os.environ.pop(name,None)
    os.environ.update(ANYAICAM_PARTNER_DB=str(Path(temp)/'test.db'),
        ANYAICAM_RUNTIME_ROLE='cloud',ANYAICAM_ENV='development',
        ANYAICAM_REQUIRE_JS_TESTS='true',AWS_EC2_METADATA_DISABLED='true')
    class Paths(ast.NodeTransformer):
        def visit_Constant(self,node):
            if isinstance(node.value,str) and (node.value=='/app' or node.value.startswith('/app/')):
                node.value=str(Path(temp)/node.value.removeprefix('/app').lstrip('/'))
            return node
    class Loader(importlib.abc.Loader):
        def __init__(self,path): self.path=path
        def create_module(self,spec): return None
        def exec_module(self,module):
            module.__file__=str(self.path)
            tree=ast.parse(self.path.read_text(encoding='utf-8'),filename=str(self.path))
            exec(compile(ast.fix_missing_locations(Paths().visit(tree)),str(self.path),'exec'),module.__dict__)
    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self,fullname,path=None,target=None):
            if '.' not in fullname:
                file=APP/(fullname+'.py')
                if file.exists(): return importlib.util.spec_from_file_location(fullname,file,loader=Loader(file))
    original_connect=socket.socket.connect
    def blocked(*args,**kwargs):
        if sys._getframe(1).f_code.co_name=='_fallback_socketpair': return original_connect(*args,**kwargs)
        raise RuntimeError('External network access prohibited during candidate tests')
    socket.socket.connect=blocked
    socket.create_connection=blocked
    sys.meta_path.insert(0,Finder())
    import types
    unavailable=types.ModuleType('ultralytics')
    def no_model(*args,**kwargs): raise RuntimeError('Model execution prohibited in reconciliation tests')
    unavailable.YOLO=no_model
    sys.modules['ultralytics']=unavailable
    import main
    @asynccontextmanager
    async def no_workers(app): yield
    main.app.router.lifespan_context=no_workers
    import pytest
    result=pytest.main(sys.argv[1:]+['-p','no:cacheprovider'])
    sys.exit(result)
