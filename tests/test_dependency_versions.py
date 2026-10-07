import importlib
import sys
from types import ModuleType

from pluginrsi.evaluation import dependency_versions


def test_installed_metadata_prevents_versioneer_execution(tmp_path, monkeypatch):
    package=tmp_path/'fixture_versioned';package.mkdir()
    (package/'__init__.py').write_text('from . import _version\n__version__ = _version.get_versions()["version"]\n')
    (package/'_version.py').write_text('raise AssertionError("Git discovery must not execute")\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(dependency_versions,'VERSIONEER_PACKAGES',('fixture_versioned',))
    monkeypatch.setattr(dependency_versions.importlib.metadata,'version',lambda name:'1.2.3')
    try:
        assert dependency_versions.prepare_dependency_versions()=={'fixture_versioned':'1.2.3'}
        loaded=importlib.import_module('fixture_versioned')
        assert loaded.__version__=='1.2.3'
        assert loaded._version.get_versions()['full-revisionid'] is None
    finally:
        sys.modules.pop('fixture_versioned',None);sys.modules.pop('fixture_versioned._version',None)


def test_existing_loaded_modules_are_not_replaced(monkeypatch):
    module=ModuleType('fixture_loaded');module.__version__='already-loaded'
    monkeypatch.setitem(sys.modules,'fixture_loaded',module)
    monkeypatch.setattr(dependency_versions,'VERSIONEER_PACKAGES',('fixture_loaded',))
    monkeypatch.setattr(dependency_versions.importlib.metadata,'version',lambda name:'1.2.3')
    dependency_versions.prepare_dependency_versions()
    assert sys.modules['fixture_loaded'] is module and module.__version__=='already-loaded'
