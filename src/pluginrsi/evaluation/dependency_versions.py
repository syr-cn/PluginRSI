"""Use installed dependency versions instead of discovering unrelated ancestor Git trees."""

import importlib.metadata
import importlib.util
import sys
from types import ModuleType

VERSIONEER_PACKAGES = ('scantree', 'dirhash')


def prepare_dependency_versions():
    versions = {}
    for package in VERSIONEER_PACKAGES:
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            continue
        versions[package] = version
        if package in sys.modules:
            continue
        name = f'{package}._version'
        module = ModuleType(name)
        module.__spec__ = importlib.util.spec_from_loader(name, loader=None)
        def get_versions(verbose=False, version=version):
            return {'version': version, 'full-revisionid': None, 'dirty': None, 'error': None, 'date': None}
        module.get_versions = get_versions
        sys.modules[name] = module
    return versions
