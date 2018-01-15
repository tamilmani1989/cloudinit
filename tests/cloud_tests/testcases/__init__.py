# This file is part of cloud-init. See LICENSE file for license information.

"""Main init."""

import importlib
import inspect
import unittest
from unittest.util import strclass

from tests.cloud_tests import config
from tests.cloud_tests.testcases.base import CloudTestCase as base_test


def discover_tests(test_name):
    """Discover tests in test file for 'testname'.

    @return_value: list of test classes
    """
    testmod_name = 'tests.cloud_tests.testcases.{}'.format(
        config.name_sanitize(test_name))
    try:
        testmod = importlib.import_module(testmod_name)
    except NameError:
        raise ValueError('no test verifier found at: {}'.format(testmod_name))

    return [mod for name, mod in inspect.getmembers(testmod)
            if inspect.isclass(mod) and base_test in inspect.getmro(mod) and
            getattr(mod, '__test__', True)]


def get_suite(test_name, data, conf):
    """Get test suite with all tests for 'testname'.

    @return_value: a test suite
    """
    suite = unittest.TestSuite()
    for test_class in discover_tests(test_name):

        class tmp(test_class):

            _realclass = test_class

            def __str__(self):
                return "%s (%s)" % (self._testMethodName,
                                    strclass(self._realclass))

            @classmethod
            def setUpClass(cls):
                cls.data = data
                cls.conf = conf

        suite.addTest(unittest.defaultTestLoader.loadTestsFromTestCase(tmp))

    return suite

# vi: ts=4 expandtab
