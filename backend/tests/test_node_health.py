import unittest
from unittest.mock import patch

import httpx

from app import main


class FailingClient:
    async def get(self, _url):
        raise httpx.ConnectTimeout("temporary timeout")


class SuccessfulResponse:
    def raise_for_status(self):
        return None


class SuccessfulClient:
    async def get(self, _url):
        return SuccessfulResponse()


class NodeHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main.node_health_cache = {
            1: {
                "status": "online",
                "checked_at": "earlier",
                "latency_ms": 10,
                "failures": 0,
            }
        }
        main.active_capture_cancellations.clear()
        self.node = {"id": 1, "name": "Bedroom", "base_url": "192.168.1.81", "enabled": 1}

    async def test_requires_three_consecutive_failures_before_offline(self):
        with patch("app.main.db.find_all", return_value=[self.node]):
            await main.refresh_node_health(FailingClient())
            self.assertEqual(main.node_health_cache[1]["status"], "online")
            await main.refresh_node_health(FailingClient())
            self.assertEqual(main.node_health_cache[1]["status"], "online")
            await main.refresh_node_health(FailingClient())

        self.assertEqual(main.node_health_cache[1]["status"], "offline")
        self.assertEqual(main.node_health_cache[1]["failures"], 3)

    async def test_success_resets_failure_count(self):
        main.node_health_cache[1]["failures"] = 2
        with patch("app.main.db.find_all", return_value=[self.node]):
            await main.refresh_node_health(SuccessfulClient())

        self.assertEqual(main.node_health_cache[1]["status"], "online")
        self.assertEqual(main.node_health_cache[1]["failures"], 0)

    async def test_capture_keeps_previous_health_without_probing(self):
        main.active_capture_cancellations[1] = object()
        client = FailingClient()
        with patch("app.main.db.find_all", return_value=[self.node]):
            await main.refresh_node_health(client)

        self.assertEqual(main.node_health_cache[1]["status"], "online")
        self.assertEqual(main.node_health_cache[1]["busy"], "capture")


if __name__ == "__main__":
    unittest.main()
