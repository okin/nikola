import os
import sys

import docutils
import pytest

from nikola.utils import LocaleBorg
from ..base import FakeSite


@pytest.fixture(scope="session")
def other_locale():
    return os.environ.get('NIKOLA_LOCALE_OTHER', 'pl')


@pytest.fixture(scope="module")
def output_dir(target_dir):
    return os.path.join(target_dir, "output")


@pytest.fixture(scope="module")
def target_dir(tmpdir_factory):
    tdir = tmpdir_factory.mktemp('integration').join('target')
    yield str(tdir)


@pytest.fixture(scope="module", autouse=True)
def remove_conf_module():
    """
    Remove the module `conf` from `sys.modules` after loading the config.

    Fixes issue #438
    """
    try:
        yield
    finally:
        try:
            del sys.modules['conf']
        except KeyError:
            pass


@pytest.fixture(scope="module", autouse=True)
def localeborg_setup(default_locale):
    """
    Reset the LocaleBorg before and after every test.
    """
    LocaleBorg.reset()
    LocaleBorg.initialize({}, default_locale)
    try:
        yield
    finally:
        LocaleBorg.reset()


@pytest.fixture(autouse=True, scope="module")
def fix_leaked_state():
    """Fix leaked state from integration tests"""
    try:
        yield
    finally:
        try:
            f = docutils.parsers.rst.roles.role('doc', None, None, None)[0]
            f.site = FakeSite()
        except AttributeError:
            pass