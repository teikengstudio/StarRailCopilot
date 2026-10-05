"""Queue regressions: python -m unittest dev_tools.test_cloud_queue."""

import threading
import unittest
from unittest.mock import patch

from module.device.cloud.core.dispatcher import DispatchConfig, Dispatcher


class CloudQueueTest(unittest.TestCase):
    def setUp(self):
        self.dispatcher = Dispatcher(DispatchConfig())
        self.addCleanup(self.dispatcher.close)

    def test_queue_continues_beyond_old_poll_limits(self):
        polls = 0
        finish = {"sdk_param": "test-session", "queue_type": "", "cost_method": 0}
        acknowledgements = []

        def respond(path, payload, headers, **kwargs):
            nonlocal polls
            if path.endswith("ackDispatchTicket"):
                acknowledgements.append(payload)
                return {"retcode": 0}
            polls += 1
            data = {"ticket_status": "QUEUEING", "queue_info": {"query_interval": 10}}
            if polls == 3101:
                data = {"ticket_status": "SUCCESS", "finish_result": finish}
            return {"retcode": 0, "data": data}

        with patch.object(self.dispatcher, "_dispatch_post", side_effect=respond), \
                patch.object(self.dispatcher, "_sleep"), \
                patch.object(self.dispatcher, "_line"):
            result = self.dispatcher._poll_dispatch_ticket({"ticket": "test-ticket"}, 10, {})
        self.assertEqual(result["sdk_param"], "test-session")
        self.assertEqual(polls, 3101)
        self.assertEqual(acknowledgements, [{"ticket": "test-ticket"}])

    def test_cancel_interrupts_wait(self):
        stop = threading.Event()
        stop.set()
        with patch.object(self.dispatcher, "_dispatch_post") as post, \
                patch.object(self.dispatcher, "_line"):
            with self.assertRaisesRegex(RuntimeError, "dispatch stopped"):
                self.dispatcher._poll_dispatch_ticket({"ticket": "test-ticket"}, 3600, {}, stop_event=stop)
        post.assert_not_called()

    def test_server_failure_exits_queue(self):
        responses = {
            "statusCheck": {}, "listPingServer": {},
            "getNodesInfo": {"nodes": [{"node_id": "test-node", "net_state": "NORMAL"}]},
            "preDispatchVerify": {},
            "paasDispatch": {"result_code": "QUEUED", "queue_info": {"ticket": "test-ticket"}},
            "getDispatchTicketInfo": {"ticket_status": "FAILED"},
            "leaveDispatchQueue": {},
        }
        leaves = []

        def respond(path, payload, headers, **kwargs):
            name = path.rsplit("/", 1)[-1]
            if name == "leaveDispatchQueue":
                leaves.append(payload)
            return {"retcode": 0, "data": responses[name]}

        with patch.object(self.dispatcher, "_require_initialized", return_value={}), \
                patch.object(self.dispatcher, "_dispatch_post", side_effect=respond), \
                patch.object(self.dispatcher, "_sleep"), \
                patch.object(self.dispatcher, "_line"):
            with self.assertRaisesRegex(RuntimeError, "ticket failed: FAILED"):
                self.dispatcher.run()
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0]["ticket"], "test-ticket")
        self.assertEqual(leaves[0]["leave_type"], 1)


if __name__ == "__main__":
    unittest.main()
