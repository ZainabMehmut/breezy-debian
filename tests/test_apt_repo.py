#    test_apt_repo.py -- Testsuite for apt_repo
#    Copyright (C) 2022 Jelmer Vernooij <jelmer@debian.org>
#
#    This file is part of bzr-builddeb.
#
#    bzr-builddeb is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    bzr-builddeb is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with bzr-builddeb; if not, write to the Free Software
#    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
#

import os
import shutil

from breezy.tests import TestCase

from debian.deb822 import Dsc


from ..apt_repo import LocalApt, NoAptSources, RemoteApt


class MockSources:
    def __init__(self, versions, files):
        self.restart_called_times = 0
        self.step_called_times = 0
        self.lookup_called_times = 0
        self.lookup_package = None
        self.versions = versions
        self.version = None
        self.filess = files
        self.files = None

    @property
    def record(self):
        if self.version is None:
            return None
        dsc = Dsc({"Package": "apackage", "Version": self.version})
        dsc["Files"] = [
            {"md5sum": checksum, "size": size, "name": name}
            for (checksum, size, name, kind) in self.files
        ]
        return str(dsc)

    def restart(self):
        self.restart_called_times += 1

    def step(self):
        self.step_called_times += 1
        if self.step_called_times <= len(self.versions):
            self.version = self.versions[self.step_called_times - 1]
            self.files = self.filess[self.step_called_times - 1]
            return True
        else:
            self.version = None
            self.files = None
            return False

    def lookup(self, package):
        self.lookup_called_times += 1
        assert not self.lookup_package or self.lookup_package == package
        self.lookup_package = package
        if self.lookup_called_times <= len(self.versions):
            self.version = self.versions[self.lookup_called_times - 1]
            self.files = self.filess[self.lookup_called_times - 1]
            return True
        else:
            self.version = None
            self.files = None
            return False


class MockAptConfig:
    def __init__(self):
        self._d = {}

    def set(self, name, value):
        self._d[name] = value


class MockAptPkg:
    def __init__(self, sources):
        self.init_called_times = 0
        self.get_pkg_source_records_called_times = 0
        self.sources = sources
        self.config = MockAptConfig()

    def init(self):
        self.init_called_times += 1

    def SourceRecords(self):
        self.get_pkg_source_records_called_times += 1
        return self.sources


class MockAptCaller:
    def __init__(self, work=False):
        self.work = work
        self.called = 0
        self.package = None
        self.version_str = None
        self.target_dir = None

    def call(self, package, version_str, target_dir):
        self.package = package
        self.version_str = version_str
        self.target_dir = target_dir
        self.called += 1
        return self.work


class MockAptCache:
    def __init__(self, calls, close_error=None, update_error=None):
        self.close_called_times = 0
        self._calls = calls
        self._close_error = close_error
        self._update_error = update_error

    def update(self):
        self._calls.append("update")
        if self._update_error is not None:
            raise self._update_error

    def close(self):
        self.close_called_times += 1
        self._calls.append("close")
        if self._close_error is not None:
            raise self._close_error


class LocalAptTests(TestCase):
    def test_get_apt_command_for_source(self):
        self.assertEqual(
            ["apt", "source", "-d", "-y", "--only-source", "apackage=someversion"],
            LocalApt(rootdir=None)._get_command("apackage", "someversion"),
        )
        self.assertEqual(
            ["apt", "source", "-d", "-y", "--only-source", "apackage"],
            LocalApt()._get_command("apackage"),
        )
        self.assertEqual(
            ["apt", "source", "-d", "--tar-only", "-y", "--only-source", "apackage"],
            LocalApt()._get_command("apackage", tar_only=True),
        )
        self.assertEqual(
            [
                "apt",
                "source",
                "-d",
                "-oDir=/tmp/lala",
                "-y",
                "--only-source",
                "apackage",
            ],
            LocalApt("/tmp/lala")._get_command("apackage"),
        )

    def test_iter_sources_empty(self):
        caller = MockAptCaller()
        sources = MockSources([], [])
        src = LocalApt()
        src._run_apt_source = caller.call
        src.apt_pkg = MockAptPkg(sources)
        self.assertEqual([], list(src.iter_sources()))
        self.assertEqual([], list(src.iter_source_by_name("breezy")))

    def test_no_apt_sources(self):
        src = LocalApt()
        sources = MockSources([], [])

        def raise_systemerror():
            raise SystemError("no apt sources")

        src.apt_pkg = MockAptPkg(sources)
        src.apt_pkg.SourceRecords = raise_systemerror
        self.assertRaises(NoAptSources, list, src.iter_sources())

    def test_iter_sources(self):
        sources = MockSources(
            ["0.1-1"], [[("checksum", 0, "apackage_0.1.orig.tar.gz", "tar")]]
        )
        src = LocalApt()
        src.apt_pkg = MockAptPkg(sources)
        self.assertEqual(
            [
                {
                    "Package": "apackage",
                    "Version": "0.1-1",
                    "Files": [
                        {
                            "md5sum": "checksum",
                            "size": "0",
                            "name": "apackage_0.1.orig.tar.gz",
                        }
                    ],
                }
            ],
            list(src.iter_sources()),
        )
        self.assertEqual(
            [
                {
                    "Package": "apackage",
                    "Version": "0.1-1",
                    "Files": [
                        {
                            "md5sum": "checksum",
                            "size": "0",
                            "name": "apackage_0.1.orig.tar.gz",
                        }
                    ],
                }
            ],
            list(src.iter_source_by_name("apackage")),
        )


class RemoteAptExitTests(TestCase):
    def test_closes_cache_before_rmtree(self):
        # The apt.Cache opened in __enter__ holds descriptors into _rootdir,
        # so __exit__ has to close it before rmtree'ing that directory.
        calls = []
        remote = RemoteApt("http://example.com/debian", "sid", ["main"])
        remote._rootdir = "/nonexistent/rootdir"
        remote.cache = MockAptCache(calls)
        self.overrideAttr(shutil, "rmtree", lambda path: calls.append(("rmtree", path)))

        remote.__exit__(None, None, None)

        self.assertEqual(["close", ("rmtree", "/nonexistent/rootdir")], calls)
        self.assertIsNone(remote.cache)

    def test_exit_without_enter(self):
        # breezyshim's RemoteApt::drop calls __exit__ blind, so __exit__ has to
        # cope with __enter__ never having run.
        calls = []
        remote = RemoteApt("http://example.com/debian", "sid", ["main"])
        remote._rootdir = "/nonexistent/rootdir"
        self.overrideAttr(shutil, "rmtree", lambda path: calls.append(("rmtree", path)))

        remote.__exit__(None, None, None)

        self.assertEqual([("rmtree", "/nonexistent/rootdir")], calls)

    def test_exit_without_rootdir(self):
        calls = []
        remote = RemoteApt("http://example.com/debian", "sid", ["main"])
        self.overrideAttr(shutil, "rmtree", lambda path: calls.append(("rmtree", path)))

        remote.__exit__(None, None, None)

        self.assertEqual([], calls)

    def test_cache_is_none_before_enter(self):
        self.assertIsNone(LocalApt().cache)
        self.assertIsNone(RemoteApt("http://example.com/debian", "sid", ["main"]).cache)

    def test_removes_rootdir_when_close_raises(self):
        calls = []
        remote = RemoteApt("http://example.com/debian", "sid", ["main"])
        remote._rootdir = "/nonexistent/rootdir"
        remote.cache = MockAptCache(calls, close_error=RuntimeError("apt_pkg"))
        self.overrideAttr(shutil, "rmtree", lambda path: calls.append(("rmtree", path)))

        self.assertRaises(RuntimeError, remote.__exit__, None, None, None)

        self.assertEqual(["close", ("rmtree", "/nonexistent/rootdir")], calls)


class RemoteAptEnterTests(TestCase):
    def test_cleans_up_when_update_fails(self):
        import apt
        import apt_pkg

        calls = []
        rootdirs = []

        def make_cache(rootdir=None):
            rootdirs.append(rootdir)
            return MockAptCache(calls, update_error=RuntimeError("mirror down"))

        apt_pkg.init()
        self.addCleanup(apt_pkg.config.set, "Dir", apt_pkg.config.find("Dir"))
        self.overrideAttr(apt, "Cache", make_cache)
        remote = RemoteApt("http://example.com/debian", "sid", ["main"])

        self.assertRaises(RuntimeError, remote.__enter__)

        self.assertEqual(["update", "close"], calls)
        self.assertIsNone(remote.cache)
        self.assertIsNone(remote._rootdir)
        self.assertFalse(os.path.isdir(rootdirs[0]))

    def test_creates_trusted_gpg_d(self):
        import apt
        import apt_pkg

        calls = []
        rootdirs = []

        def make_cache(rootdir=None):
            rootdirs.append(rootdir)
            return MockAptCache(calls)

        apt_pkg.init()
        self.addCleanup(apt_pkg.config.set, "Dir", apt_pkg.config.find("Dir"))
        self.overrideAttr(apt, "Cache", make_cache)
        remote = RemoteApt("http://example.com/debian", "sid", ["main"])

        with remote:
            self.assertTrue(
                os.path.isdir(os.path.join(rootdirs[0], "etc", "apt", "trusted.gpg.d"))
            )
