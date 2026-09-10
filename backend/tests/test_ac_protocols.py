from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from app.main import (
    AcControllerCommandIn,
    CommandError,
    build_ac_controller_payload,
    daikin64_payload,
    panasonic_ac_payload,
    send_ac_controller_command,
)


class PanasonicAcPayloadTests(unittest.TestCase):
    def test_on_26_auto_fan_auto_swing_matches_learned_protocol(self) -> None:
        command = AcControllerCommandIn(
            temperature=26,
            fan="auto",
            swing=True,
        )

        payload = panasonic_ac_payload(command, power=True)

        self.assertEqual(
            payload["state"],
            [
                0x02, 0x20, 0xE0, 0x04, 0x00, 0x00, 0x00, 0x06,
                0x02, 0x20, 0xE0, 0x04, 0x00, 0x3D, 0x34, 0x80,
                0xAF, 0x00, 0x00, 0x0E, 0xE0, 0x00, 0x00, 0x89,
                0x00, 0x00, 0x1D,
            ],
        )
        self.assertEqual(payload["khz"], 37)
        self.assertEqual(payload["repeat"], 1)
        self.assertEqual(len(payload["raw"]), 439)
        self.assertEqual(payload["raw"][131], 10000)

    def test_off_and_control_fields_are_encoded_independently(self) -> None:
        command = AcControllerCommandIn(
            temperature=30,
            fan="2",
            swing=False,
        )

        payload = panasonic_ac_payload(command, power=False)
        state = payload["state"]

        self.assertEqual(state[13], 0x3C)
        self.assertEqual(state[14], 0x3C)
        self.assertEqual(state[16], 0x53)
        self.assertEqual(state[26], (0xF4 + sum(state[:26])) & 0xFF)


class AcProtocolDispatchTests(unittest.TestCase):
    def test_daikin64_output_remains_unchanged(self) -> None:
        command = AcControllerCommandIn(
            temperature=27,
            fan="1",
            swing=False,
            power_toggle=True,
        )
        now = datetime(2026, 9, 10, 4, 35, tzinfo=timezone.utc)

        payload = daikin64_payload(command, now=now)

        self.assertEqual(payload["protocol"], "daikin64")
        self.assertEqual(payload["state"][0], 0x16)
        self.assertEqual(len(payload["state"]), 8)
        self.assertEqual(len(payload["raw"]), 137)

    def test_dispatch_uses_controller_protocol(self) -> None:
        command = AcControllerCommandIn(temperature=26, fan="auto", swing=True)

        payload = build_ac_controller_payload(
            {"protocol": "panasonic_ac"}, command, power=True
        )

        self.assertEqual(payload["protocol"], "panasonic_ac")
        self.assertEqual(payload["state"][13], 0x3D)

    def test_unknown_protocol_is_rejected(self) -> None:
        command = AcControllerCommandIn(temperature=26, fan="auto", swing=True)

        with self.assertRaisesRegex(CommandError, "Unsupported aircond protocol"):
            build_ac_controller_payload({"protocol": "unknown"}, command, power=False)


class AcControllerRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_node_overrides_and_replaces_last_used_node(self) -> None:
        controller = {
            "id": 1,
            "name": "Bedroom AC",
            "brand": "Panasonic",
            "protocol": "panasonic_ac",
            "last_node_id": 1,
            "power": 0,
            "temperature": 26,
            "fan": "auto",
            "swing": 1,
        }
        selected_node = {
            "id": 2,
            "name": "Guest room",
            "base_url": "192.168.1.82",
            "enabled": 1,
        }
        command = AcControllerCommandIn(
            node_id=2,
            temperature=27,
            fan="1",
            swing=False,
            power_toggle=True,
        )
        requests = []

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, **kwargs):
                requests.append((url, kwargs))
                return FakeResponse()

        def find_one(entity, query):
            if entity == "ac_controllers":
                return controller.copy()
            if entity == "nodes" and query == {"id": 2}:
                return selected_node.copy()
            return None

        with (
            patch("app.main.db.find_one", side_effect=find_one),
            patch("app.main.db.update") as update,
            patch("app.main.db.insert"),
            patch("app.main.controller_details", return_value={**controller, "last_node_id": 2}),
            patch("app.main.httpx.AsyncClient", return_value=FakeClient()),
        ):
            result = await send_ac_controller_command(1, command)

        self.assertEqual(requests[0][0], "http://192.168.1.82/send/ir/raw")
        self.assertEqual(update.call_args.args[2]["last_node_id"], 2)
        self.assertEqual(result["controller"]["last_node_id"], 2)


if __name__ == "__main__":
    unittest.main()
