"""Command menus and startup smoke tests with synthetic Telegram responses."""

import unittest
from unittest.mock import MagicMock, patch

from test_bot import make_bot, main


class TestCommands(unittest.TestCase):
    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)
        self.bot.send_message = MagicMock()
        self.bot.append_to_file = MagicMock()
        self.bot.handle_natural_language = MagicMock()

    def message(self, text, chat_id=123):
        self.bot.handle_message({"message": {"text": text, "chat": {"id": chat_id}}})

    def response(self, url, **kwargs):
        result = {"username": "ExampleBot"} if url.endswith('/getMe') else True
        return MagicMock(status_code=200, json=MagicMock(return_value={"ok": True, "result": result}))

    def test_registration_scopes_and_private_menu(self):
        self.bot.settings.ALLOWED_CHATS = {"123", "-456"}
        with patch.object(main.HTTP, 'post', side_effect=self.response) as post:
            self.assertTrue(self.bot.register_commands())
        commands = [c.kwargs['json'] for c in post.call_args_list if c.args[0].endswith('/setMyCommands')]
        self.assertEqual({p['scope']['chat_id'] for p in commands}, {'123', '-456'})
        for payload in commands:
            self.assertEqual(payload['scope']['type'], 'chat')
            self.assertEqual(payload['language_code'], '')
            names = [c['command'] for c in payload['commands']]
            self.assertEqual(set(names), {'start', 'help', 'today', 'last', 'undo', 'tz', 'update', 'view'})
            self.assertEqual(len(names), len(set(names)))
            for command in payload['commands']:
                self.assertRegex(command['command'], r'^[a-z0-9_]{1,32}$')
                self.assertTrue(1 <= len(command['description']) <= 256)
        menus = [c.kwargs['json'] for c in post.call_args_list if c.args[0].endswith('/setChatMenuButton')]
        self.assertEqual(menus, [{'chat_id': 123, 'menu_button': {'type': 'commands'}}])
        self.assertEqual(self.bot._telegram_username, 'examplebot')

    def test_failed_registration_is_nonfatal_and_does_not_log_token(self):
        failures = [main.requests.Timeout(self.bot.api_base), ValueError('invalid JSON')]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), patch.object(main.HTTP, 'post', side_effect=failure), patch('beancounter.telegram_api.log') as log:
                self.assertFalse(self.bot.register_commands())
                self.assertNotIn(self.bot.settings.TELEGRAM_BOT_TOKEN, str(log.call_args_list))

    def test_rejected_or_malformed_registration_response(self):
        for status, payload in [(500, {}), (200, {'ok': False}), (200, []), (200, {'ok': True, 'result': False})]:
            with self.subTest(status=status, payload=payload), patch.object(main.HTTP, 'post', return_value=MagicMock(status_code=status, json=MagicMock(return_value=payload))):
                self.assertFalse(self.bot.register_commands())

    def test_registration_retries_while_polling_and_stops_after_success(self):
        clock = [0]
        poll_times = []

        def poll():
            poll_times.append(clock[0])
            clock[0] += 150
            if len(poll_times) == 5:
                self.bot.stop.set()

        self.bot.process_updates = poll
        with patch.object(self.bot, 'register_commands', side_effect=[False, True]) as register, patch('beancounter.telegram_api.time.monotonic', side_effect=lambda: clock[0]):
            self.bot.start()
        self.assertEqual(register.call_count, 2)
        self.assertEqual(poll_times, [0, 150, 300, 450, 600])

    def test_help_and_start_are_local_and_include_every_command(self):
        for text in ['/help', '/start welcome', ' /HELP\n']:
            self.message(text)
            help_text = self.bot.send_message.call_args.args[1]
            for name, _, _ in self.bot.COMMANDS:
                self.assertIn('/' + name, help_text)
            self.assertLessEqual(main._utf16_len(help_text), 4096)
        self.bot.append_to_file.assert_not_called()
        self.bot.handle_natural_language.assert_not_called()

    def test_help_does_not_consume_pending_feedback(self):
        self.bot.pending_decline_reasons[123] = 'draft'
        self.message('/help')
        self.assertEqual(self.bot.pending_decline_reasons[123], 'draft')
        self.assertIn('记账示例', self.bot.send_message.call_args.args[1])

    def test_unauthorized_help_is_ignored(self):
        self.message('/help', chat_id=999)
        self.bot.send_message.assert_not_called()

    def test_group_suffix_is_checked_and_arguments_survive(self):
        self.bot._telegram_username = 'examplebot'
        self.bot.handle_last = MagicMock()
        self.message('/last@ExampleBot\n12')
        self.bot.handle_last.assert_called_once_with(123, 12)
        self.message('/last@AnotherBot 3')
        self.assertEqual(self.bot.handle_last.call_count, 1)
        self.bot.send_message.assert_not_called()

    def test_unknown_bot_identity_does_not_handle_addressed_commands(self):
        self.message('/help@AnotherBot')
        self.bot.send_message.assert_not_called()

    def test_timezone_menu_click_shows_status_without_mutation(self):
        self.message('/tz')
        self.assertEqual(str(self.bot.timezone), 'UTC')
        self.assertIn('当前时区：UTC', self.bot.send_message.call_args.args[1])
        self.assertIn('/tz Europe/London', self.bot.send_message.call_args.args[1])
        self.bot.append_to_file.assert_not_called()

    def test_timezone_setting_and_invalid_value(self):
        self.message('/tz Europe/London')
        self.assertEqual(str(self.bot.timezone), 'Europe/London')
        self.bot.send_message.assert_called_once()
        self.message('/tz Invalid/Zone')
        self.assertEqual(str(self.bot.timezone), 'Europe/London')
        self.assertIn('未知时区', self.bot.send_message.call_args.args[1])

    def test_unknown_command_points_to_help(self):
        self.message('/missing')
        self.assertIn('/help', self.bot.send_message.call_args.args[1])
        self.bot.handle_natural_language.assert_not_called()

    def test_startup_smoke_registers_then_dispatches_help(self):
        def updates(*args, **kwargs):
            self.bot.stop.set()
            return MagicMock(status_code=200, json=MagicMock(return_value={'ok': True, 'result': [
                {'update_id': 1, 'message': {'chat': {'id': 123}, 'text': '/help@ExampleBot'}}
            ]}))

        with patch.object(main.HTTP, 'post', side_effect=self.response) as post, patch.object(main.HTTP, 'get', side_effect=updates):
            self.bot.start()
            self.bot.close()
        self.assertTrue(any(c.args[0].endswith('/setMyCommands') for c in post.call_args_list))
        self.bot.send_message.assert_called_once()
        self.assertIn('记账示例', self.bot.send_message.call_args.args[1])
        self.bot.append_to_file.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
