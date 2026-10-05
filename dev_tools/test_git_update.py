"""Exercise real Git updates: python -m unittest dev_tools.test_git_update."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from deploy.Windows.git import GitManager


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class GitUpdateTest(unittest.TestCase):
    def test_update_preserves_ignored_permissions_and_local_content(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory(prefix='git-update-', dir=previous / 'log') as directory:
            root = Path(directory)
            upstream, work = root / 'upstream', root / 'work'
            upstream.mkdir()

            def git(folder, *args):
                return subprocess.check_output(['git', '-C', str(folder), *args], text=True).strip()

            git(upstream, 'init', '-q', '-b', 'master')
            git(upstream, 'config', 'user.name', 'Update Test')
            git(upstream, 'config', 'user.email', 'update-test@example.invalid')
            (upstream / 'local.txt').write_text('original\n')
            (upstream / 'remote.txt').write_text('before\n')
            git(upstream, 'add', '.')
            git(upstream, 'commit', '-qm', 'Initial')
            git(root, 'clone', '-q', str(upstream), str(work))
            git(work, 'config', 'core.fileMode', 'false')
            (work / 'local.txt').write_text('user change\n')
            (work / 'remote.txt').chmod(0o755)
            (upstream / 'remote.txt').write_text('after\n')
            git(upstream, 'add', '.')
            git(upstream, 'commit', '-qm', 'Remote update')
            manager = GitManager.__new__(GitManager)
            manager.git = 'git'
            try:
                os.chdir(work)
                manager.git_repository_init(str(upstream), keep_changes=True)
            finally:
                os.chdir(previous)
            self.assertEqual(git(work, 'config', '--get', 'core.fileMode'), 'false')
            self.assertEqual(git(work, 'rev-parse', 'HEAD'), git(upstream, 'rev-parse', 'HEAD'))
            self.assertEqual((work / 'remote.txt').read_text(), 'after\n')
            self.assertEqual((work / 'local.txt').read_text(), 'user change\n')


if __name__ == '__main__':
    unittest.main()
