"""Verified SnowLuma 2.2.0 wire contracts through real SQLite workflows."""
import asyncio
import unittest
from unittest.mock import AsyncMock

import test_startup_request_recovery as fixtures
from app.database.records import JoinRequestRecordManager as Records
from app.onebot.inspector import OneBotRequestInspector
from app.onebot.recovery import MissedRequestRecovery
from app.onebot.snowluma import SnowLumaRequestInspector, unhandled_events


def row(sequence=1, qq=200, group=10, kind=1, filtered=0, **values):
    return {"request_id": sequence, "requester_uin": qq, "invitor_uin": 300,
            "group_id": group, "message": "UID:456", "checked": False,
            "flag": f"slreq:1:{sequence}:{group}:{kind}:{filtered}", **values}


class SnowLumaTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.RecoveryTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.RecoveryTests.asyncSetUp(self)
        original = self.client.call_action
        self.client.rows = [row()]

        async def call(action, **kwargs):
            if action == "get_version_info":
                self.client.calls.append(action)
                return {"app_name": "SnowLuma", "app_version": "2.2.0-node"}
            if action == "get_group_system_msg":
                self.client.calls.append(action)
                return self.client.rows
            return await original(action, **kwargs)

        self.client.call_action = call
        self.inspector = SnowLumaRequestInspector(lambda bot: self.client if bot == "99" else None)
        self.metadata = {"bot_id": "99", "group_id": "10", "qq": "200", "sub_type": "add",
                         "flag": row()["flag"], "expected_result": "approved"}

    def event(self, **values):
        return unhandled_events([row(**values)], "99")[0]

    async def test_array_rows_use_requester_and_original_flag_and_skip_invitations(self):
        self.client.rows += [row(2, kind=2), row(3, kind=22), row(4, group=20)]
        await self.recovery.run_once()
        self.assertEqual(self.client.actions, [{"flag": "slreq:1:1:10:1:0", "sub_type": "add", "approve": True}])
        self.assertEqual(Records.get_user_report("200", 5)[1].approved_count, 1)
        self.assertIsNone(Records.get_user_report("300", 5)[0])

    async def test_existing_napcat_audit_is_kept_and_prevents_repeat_after_switch(self):
        Records.add_audit(group_id="10", qq="200", result="ignored", reasons=["old"], request_key="99:10:add:1")
        before = Records.get_user_report("200", 5)
        await self.recovery.run_once()
        self.assertEqual(Records.get_user_report("200", 5), before)
        self.assertEqual(self.answers, [])
        self.assertEqual(self.client.actions, [])

    async def test_periodic_scan_preserves_ignored_audit_and_discovers_new_request(self):
        now = [0.0]
        self.recovery = MissedRequestRecovery(self.runtime, clock=lambda: now[0])
        self.state = "inaccessible"
        await self.recovery.run_once()
        messages = list(self.client.messages)
        self.client.rows = [row(filtered=1), row(2, qq=201), row(3, kind=2, qq=202),
                            row(4, kind=22, qq=203), row(5, qq=0)]
        self.state = "passed"
        now[0] = 60
        await self.recovery.run_once()
        self.assertEqual(self.client.messages, messages)
        self.assertEqual(self.client.actions, [{"flag": "slreq:1:2:10:1:0", "sub_type": "add", "approve": True}])
        self.assertEqual(Records.get_user_report("200", 5)[1].ignored_count, 1)
        self.assertEqual(Records.get_user_report("201", 5)[1].approved_count, 1)
        self.assertEqual(self.answers, ["UID:456", "UID:456"])

    async def test_two_inbox_flags_and_restart_share_one_application(self):
        await self.recovery.run_once()
        self.client.rows = [row(filtered=1)]
        await MissedRequestRecovery(self.runtime).run_once()
        self.assertEqual(len(self.client.actions), 1)
        self.assertEqual(len(self.answers), 1)

    async def test_live_inbox_variants_racing_scan_do_not_duplicate(self):
        await asyncio.gather(self.recovery.run_once(),
                            self.service.handle_group_request(self.client, self.event(filtered=1)))
        self.assertEqual(len(self.client.actions), 1)
        self.assertEqual(Records.get_user_report("200", 5)[1].approved_count, 1)

    async def test_migrated_identity_with_different_applicant_is_not_guessed(self):
        Records.add_audit(group_id="10", qq="201", result="ignored", reasons=["old"], request_key="99:10:add:1")
        await self.recovery.run_once()
        self.assertEqual(self.client.actions, [])
        self.assertEqual(self.answers, [])

    async def test_live_join_flag_with_other_group_or_invitation_type_is_not_actioned(self):
        event = self.event()
        for flag in ("slreq:1:1:11:1:0", "slreq:1:1:10:22:0", "slreq:1:1:10:1:2"):
            await self.service.handle_group_request(self.client, {**event, "flag": flag})
        self.assertEqual(self.client.actions, [])
        self.assertEqual(self.answers, [])

    async def test_exact_unhandled_evidence_preserves_all_action_fields(self):
        result = await self.inspector(self.metadata)
        self.assertEqual(result.state, "unhandled")
        self.assertTrue(result.matches(self.metadata))

    async def test_checked_missing_conflicting_or_ambiguous_rows_are_unknown(self):
        for rows in ([row(checked=True)], [], [row(qq=201)], [row(group=11)],
                     [row(filtered=1)], [row(), row(qq=201)], [row(checked=0)]):
            with self.subTest(rows=rows):
                self.client.rows = rows
                self.assertEqual((await self.inspector(self.metadata)).state, "unknown")

    async def test_numeric_napcat_pending_is_not_replayed_through_snowluma(self):
        inspector = OneBotRequestInspector(lambda bot: self.client)
        result = await inspector({**self.metadata, "flag": "1"})
        self.assertEqual(result.state, "unknown")
        self.assertNotIn("get_group_system_msg", self.client.calls)

    async def test_auto_inspector_selects_snowluma_contract(self):
        result = await OneBotRequestInspector(lambda bot: self.client)(self.metadata)
        self.assertEqual(result.state, "unhandled")

    async def test_unavailable_or_wrong_implementation_does_not_confirm_outcome(self):
        self.client.call_action = AsyncMock(side_effect=TimeoutError())
        self.assertEqual((await self.inspector(self.metadata)).state, "unavailable")
        self.client.call_action = AsyncMock(return_value={"app_name": "unknown"})
        self.assertEqual((await self.inspector(self.metadata)).state, "unknown")

    async def test_unknown_ack_remains_pending_across_inbox_change(self):
        self.client.fail_action = True
        await self.recovery.run_once()
        self.client.rows = [row(filtered=1)]
        await MissedRequestRecovery(self.runtime).run_once()
        self.assertEqual(Records.get_pending_decision_count(), 1)
        self.assertEqual(len(self.client.actions), 1)
        self.assertIsNone(Records.get_bili_binding(456))

    def test_parser_never_guesses_flags_or_uses_invitor_as_applicant(self):
        invalid = [row(flag="1"), row(flag="slreq:1:2:10:1:0"), row(flag="slreq:1:1:11:1:0"),
                   row(flag="slreq:1:1:10:1:2"), row(sequence=2**53), row(qq=False), row(message=None)]
        self.assertEqual(unhandled_events(invalid, "99"), [])
        self.assertEqual(unhandled_events([row(), row(filtered=1)], "99"), [])
        with self.assertRaises(ValueError):
            unhandled_events({"join_requests": [row()]}, "99")
