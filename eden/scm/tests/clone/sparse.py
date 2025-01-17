# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

# pyre-strict

from eden.testlib.base import BaseTest, hgtest
from eden.testlib.repo import Repo
from eden.testlib.util import new_dir
from eden.testlib.workingcopy import WorkingCopy


class TestSparseClone(BaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.config.enable("sparse")
        self.config.add("remotenames", "selectivepulldefault", "master")
        self.config.add("clone", "force-rust", "True")

    @hgtest
    def test_simple(self, repo: Repo, wc: WorkingCopy) -> None:
        wc.file(path="sparse/base", content="sparse/\ninc/\n")
        wc.file(path="inc/foo")
        wc.file(path="inc/bar")
        wc.file(path="exc/foo")
        commit1 = wc.commit()

        wc.hg.push(rev=commit1.hash, to="master", create=True)

        sparse_wc = WorkingCopy(repo, new_dir())
        repo.hg.clone(repo.url, sparse_wc.root, enable_profile="sparse/base")

        self.assertEqual(
            sorted(sparse_wc.hg.files().stdout.rstrip().split("\n")),
            ["inc/bar", "inc/foo", "sparse/base"],
        )

        self.assertTrue(sparse_wc.status().empty())

        # Make sure Python agrees what files we should have.
        sparse_wc.hg.sparse("refresh")
        self.assertEqual(
            sorted(sparse_wc.hg.files().stdout.rstrip().split("\n")),
            ["inc/bar", "inc/foo", "sparse/base"],
        )

    @hgtest
    def test_config_override(self, repo: Repo, wc: WorkingCopy) -> None:
        wc.file(path="sparse/base", content="sparse/\n")
        wc.file(path="a")
        wc.file(path="b")
        commit1 = wc.commit()

        wc.hg.push(rev=commit1.hash, to="master", create=True)

        # We support sparse rules coming from dynamic config.
        repo.config.add("sparseprofile", "include.blah.sparse/base", "a")

        sparse_wc = WorkingCopy(repo, new_dir())
        repo.hg.clone(repo.url, sparse_wc.root, enable_profile="sparse/base")

        self.assertEqual(
            sorted(sparse_wc.hg.files().stdout.rstrip().split("\n")),
            ["a", "sparse/base"],
        )

        self.assertTrue(sparse_wc.status().empty())

        # Make sure Python agrees what files we should have.
        sparse_wc.hg.sparse("refresh")
        self.assertEqual(
            sorted(sparse_wc.hg.files().stdout.rstrip().split("\n")),
            ["a", "sparse/base"],
        )
