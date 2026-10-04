"""Consumer-visible cloud task policy and clipboard regression checks."""

import copy
import unittest
from datetime import datetime, timedelta

from module.config.config import AzurLaneConfig
from module.config.config_updater import ConfigUpdater


class CloudPolicyTests(unittest.TestCase):
    def config(self, mode):
        config = AzurLaneConfig.__new__(AzurLaneConfig)
        config.data = {
            'Alas': {'Emulator': {'GameClient': mode}},
            'Rogue': {'Scheduler': {'Command': 'Rogue', 'Enable': True, 'NextRun': datetime.now() - timedelta(days=1)},
                      'RogueWorld': {'UseImmersifier': True, 'UseStamina': True, 'DoubleEvent': True}},
            'Dungeon': {'Scheduler': {'Command': 'Dungeon', 'Enable': True, 'NextRun': datetime.now() - timedelta(days=1)}},
        }
        config.modified = {}
        config.auto_update = False
        return config

    def test_protocol_mode_ignores_stale_unsupported_scheduler_tasks(self):
        config = self.config('cloud_direct')
        config.get_next_task()
        self.assertEqual([task.command for task in config.pending_task], ['Dungeon'])
        for task in ('Rogue', 'Daemon', 'PlannerScan'):
            self.assertFalse(config.is_task_supported(task))
        self.assertFalse(config.task_call('Rogue', force_call=True))
        self.assertNotIn('Rogue.Scheduler.Enable', config.modified)

    def test_protocol_conversion_disables_rogue_without_enabling_dungeon(self):
        config = self.config('cloud_direct')
        config.data['Dungeon']['Scheduler']['Enable'] = False
        result = ConfigUpdater.update_state(copy.deepcopy(config.data))
        self.assertFalse(result['Rogue']['Scheduler']['Enable'])
        self.assertFalse(result['Dungeon']['Scheduler']['Enable'])
        self.assertEqual(result['Rogue']['RogueWorld'], {'UseImmersifier': False, 'UseStamina': False, 'DoubleEvent': False})
        self.assertEqual(result['Alas']['Emulator']['PackageName'], 'CN-Official')

    def test_other_device_modes_keep_existing_task_support(self):
        for mode in ('android', 'cloud_android'):
            with self.subTest(mode=mode):
                config = self.config(mode)
                for task in ('Rogue', 'Daemon', 'PlannerScan'):
                    self.assertTrue(config.is_task_supported(task))
                config.get_next_task()
                self.assertIn('Rogue', [task.command for task in config.pending_task])


if __name__ == '__main__':
    unittest.main()
