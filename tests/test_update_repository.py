"""Update repository selection must never pull this fork's upstream CDN pack."""

import tempfile
import unittest
from pathlib import Path

from deploy.Windows.config import ConfigModel, DeployConfig


class UpdateRepositoryTests(unittest.TestCase):
    def test_mainland_alias_cannot_apply_upstream_cdn_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'deploy.yaml'
            path.write_text('Deploy:\n  Git:\n    Repository: cn\n')
            config = DeployConfig(file=str(path))
            self.assertEqual(config.Repository, ConfigModel.Repository)
            self.assertFalse(config.GitOverCdn)
            config.Repository = 'global'
            config.config_redirect()
            self.assertEqual(config.Repository, ConfigModel.Repository)
            self.assertFalse(config.GitOverCdn)

    def test_explicit_repository_is_not_overridden(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'deploy.yaml'
            repository = 'git@github.com:teikengstudio/StarRailCopilot.git'
            path.write_text('Deploy:\n  Git:\n    Repository: ' + repository + '\n')
            config = DeployConfig(file=str(path))
            self.assertEqual(config.Repository, repository)
            self.assertFalse(config.GitOverCdn)
            config.Repository = 'https://example.invalid/custom.git'
            config.config_redirect()
            self.assertEqual(config.Repository, 'https://example.invalid/custom.git')


if __name__ == '__main__':
    unittest.main()
