"""Keep extracted decision diagnostics in the Bot's configured Loguru sinks."""

import logging
import unittest

import nonebot

nonebot.init()

from nonebot.log import logger
from src.gatekeeper import decisions
from src.plugins.group_request_manager.decision_adapter import configure_decision_logging


class DecisionLoggingTests(unittest.TestCase):
    def setUp(self):
        self.decision_logger = logging.getLogger(decisions.__name__)
        original = (
            self.decision_logger.handlers,
            self.decision_logger.level,
            self.decision_logger.propagate,
        )

        def restore():
            handlers, level, propagate = original
            self.decision_logger.handlers = handlers
            self.decision_logger.setLevel(level)
            self.decision_logger.propagate = propagate

        self.addCleanup(restore)
        self.decision_logger.handlers = []
        self.messages = []
        sink_id = logger.add(self.messages.append, format="{message}")
        self.addCleanup(logger.remove, sink_id)

    def test_repeated_configuration_delivers_one_formatted_message(self):
        configure_decision_logging()
        configure_decision_logging()
        self.decision_logger.info("审批审计 #%s", 7)

        self.assertEqual([message.record["message"] for message in self.messages], ["审批审计 #7"])
        self.assertFalse(self.decision_logger.propagate)

    def test_action_failure_retains_exception_information_in_loguru_sink(self):
        configure_decision_logging()
        try:
            raise RuntimeError("approval transport offline")
        except RuntimeError:
            self.decision_logger.exception("审批结果无法确认")

        self.assertEqual(len(self.messages), 1)
        record = self.messages[0].record
        self.assertEqual(record["message"], "审批结果无法确认")
        self.assertIs(record["exception"].type, RuntimeError)
