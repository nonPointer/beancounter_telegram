"""Telegram api responsibilities of Bot; shared entry points are preserved."""

import traceback
import uuid
import time
import requests
from .bot_utils import (
    AccountMatchError, HTTP, POLL_BACKOFF_BASE, POLL_BACKOFF_MAX, TELEGRAM_MESSAGE_LIMIT,
    _scrub, _utf16_len, log,
)

class TelegramMixin:
    # Menu descriptions and help share one command list.
    COMMANDS = (
        ("help", "查看命令和记账示例", ""),
        ("today", "查看今天的记录", ""),
        ("last", "查看最近记录（默认 5 条，最多 50 条）", "[数量]"),
        ("undo", "预览并确认撤回最后一条指令", ""),
        ("tz", "查看或设置时区", "[时区，例如 Europe/London]"),
        ("update", "补差并设置次日余额断言", "<账户> <补差账户> <金额> <币种>"),
        ("view", "生成月度桑基图（需配置工作流）", ""),
        ("start", "查看入门说明", ""),
    )

    def command_help(self):
        lines = ["记账助手", "输入 / 可选择命令："]
        lines.extend(f"/{name}{' ' + usage if usage else ''} — {description}" for name, description, usage in self.COMMANDS)
        lines.extend([
            "", "记账示例：", "直接发送：现金买咖啡 5 GBP", "也可以发送账单截图；投资订单截图请在说明中加 invest。",
            "自然语言和截图需要配置 LLM；草稿的保存方式请查看确认消息。",
            "", "手动记账（每项单独一行）：", "咖啡店\n咖啡\nExpenses:Food 5 GBP\nAssets:Cash -5 GBP",
            "", "账户指令不加 /，例如：open Assets:Cash GBP", "还支持 close、balance、pad；/update 会直接写入补差和余额断言。",
        ])
        return "\n".join(lines)

    def register_commands(self):
        """Refresh menus for configured chats; failures leave polling available."""
        calls = []
        if not self._telegram_username:
            calls.append(("getMe", {}))
        commands = [{"command": name, "description": description} for name, description, _ in self.COMMANDS]
        for chat_id in sorted(self.settings.ALLOWED_CHATS):
            calls.append(("setMyCommands", {"commands": commands, "scope": {"type": "chat", "chat_id": chat_id}, "language_code": ""}))
            if chat_id.isdigit() and int(chat_id) > 0:
                calls.append(("setChatMenuButton", {"chat_id": int(chat_id), "menu_button": {"type": "commands"}}))
        succeeded = True
        for method, payload in calls:
            if self.stop.is_set():
                return False
            try:
                response = HTTP.post(self.api_base + "/" + method, json=payload, timeout=10)
                data = response.json()
                if response.status_code != 200 or not isinstance(data, dict) or data.get("ok") is not True:
                    raise ValueError(f"HTTP {response.status_code}")
                if method == "getMe":
                    self._telegram_username = data["result"]["username"].lower()
                elif data.get("result") is not True:
                    raise ValueError("Unexpected result")
            except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as exc:
                # Request exceptions may contain the token in the URL; log only the type.
                log(f"Telegram {method} failed ({type(exc).__name__}); retrying in 5 minutes.")
                succeeded = False
        return succeeded

    def get_telegram_file_bytes(self, file_id: str) -> bytes | None:
        r = HTTP.get(self.api_base + "/getFile", params={"file_id": file_id}, timeout=30)
        if r.status_code != 200:
            log(f"Error getting file info: {r.status_code}")
            return None
        try:
            file_path = r.json()["result"]["file_path"]
        except (ValueError, KeyError, TypeError) as e:
            # A 200 with a malformed/non-JSON body must honour this method's None contract
            # (the caller replies "Failed to download the image."), not raise past it.
            log(f"getFile returned an unexpected body: {e}")
            return None
        token = self.settings.TELEGRAM_BOT_TOKEN
        dl = HTTP.get(f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=60)
        if dl.status_code != 200:
            log(f"Error downloading file: {dl.status_code}")
            return None
        return dl.content

    def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        # Telegram hard-caps a message at 4096 chars and 400s the whole POST if exceeded —
        # since send_message is the last hop of every reply() error path, an over-long body
        # (e.g. an error that embeds raw LLM output) would fail *silently*, leaving the user
        # with nothing. Guard plain text here; HTML callers pre-truncate their own payload
        # so we must not blind-cut mid-tag.
        if parse_mode is None and isinstance(text, str) and _utf16_len(text) > TELEGRAM_MESSAGE_LIMIT:
            # Cut by code points until the UTF-16 length (what Telegram counts) fits, leaving
            # room for the ellipsis. Over-cuts on astral chars, which is fine — it only needs
            # to get under the cap, and this path is a last-resort backstop for outsized text.
            text = text[:TELEGRAM_MESSAGE_LIMIT - 1]
            while _utf16_len(text) > TELEGRAM_MESSAGE_LIMIT - 1:
                text = text[:-1]
            text += "…"
        data = {"chat_id": chat_id, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup is not None:
            data["reply_markup"] = reply_markup
        response = HTTP.post(self.api_base + "/sendMessage", json=data, timeout=30)
        if response.status_code != 200:
            log(f"Error sending message: {response.status_code}")
            log(response.text)
        # No caller uses the return value; guard .json() so a non-JSON error body (e.g. an
        # HTML 502 from a proxy) can't raise here and mask the original failure that this
        # very call was trying to report.
        try:
            return response.json()
        except ValueError:
            return {}

    def answer_callback_query(self, callback_query_id, text=None):
        data = {"callback_query_id": callback_query_id}
        if text:
            data["text"] = text
        HTTP.post(self.api_base + "/answerCallbackQuery", json=data, timeout=30)

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        data = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }
        HTTP.post(self.api_base + "/editMessageReplyMarkup", json=data, timeout=30)

    def _backoff(self, reason: str, retry_after: float | None = None):
        """Wait before polling again. Waits on `stop` so Ctrl-C interrupts the delay."""
        self._poll_failures += 1
        if retry_after is None:
            retry_after = min(POLL_BACKOFF_MAX, POLL_BACKOFF_BASE * 2 ** (self._poll_failures - 1))
        log(f"{reason}; retrying in {retry_after:.1f}s")
        self.stop.wait(retry_after)

    def get_updates(self):
        params = {"offset": self.update_id + 1, "timeout": 30,
                  "limit": min(100, self.settings.WORKERS * self.settings.QUEUE_SIZE)}
        try:
            response = HTTP.get(self.api_base + "/getUpdates", params=params, timeout=params["timeout"] + 1)
        except KeyboardInterrupt:
            log("Got KeyboardInterrupt in Bot thread.")
            self.stop.set()
            exit(0)
        except Exception as e:
            self._backoff(f"getUpdates failed: {e}")
            return {"result": []}

        if response.status_code != 200:
            # A failing call returns at once instead of long-polling for 30s, so without
            # a delay the loop spins. 429 tells us exactly how long to wait.
            retry_after = None
            if response.status_code == 429:
                try:
                    retry_after = float(response.json()["parameters"]["retry_after"])
                except Exception:
                    pass
            self._backoff(f"getUpdates HTTP {response.status_code}", retry_after)
            return {"result": []}

        try:
            data = response.json()
        except ValueError:
            self._backoff("getUpdates returned non-JSON")
            return {"result": []}

        if not isinstance(data.get("result"), list):
            # Telegram error payloads have no "result"; indexing it would kill the loop.
            self._backoff(f"getUpdates payload has no 'result' list: {str(data)[:120]}")
            return {"result": []}

        self._poll_failures = 0
        return data

    def _spawn_handler(self, fn, update, chat_id):
        """Submit to a bounded per-chat lane, preserving the existing handler entry points."""
        if not self.is_authorized(chat_id):
            return False
        name = getattr(fn, "__name__", type(fn).__name__)
        uid = update.get("update_id")
        token = ("update", uid) if uid is not None else ("draft", update.get("_draft_id", uuid.uuid4().hex))
        queued_at = time.monotonic()

        def guarded():
            log(f"Timing [queue wait {token}]: {time.monotonic() - queued_at:.3f}s")
            self._handler_context.update_id = uid
            if uid is not None:
                self.state.begin(uid)
            try:
                fn(update)
            except AccountMatchError as exc:
                if self.is_authorized(chat_id):
                    self.send_message(chat_id, str(exc))
            except Exception:
                log(f"{name} crashed for chat {chat_id}:\n{traceback.format_exc()}")
                # Only ever reply to authorized chats — a crash must not become an oracle.
                if self.is_authorized(chat_id):
                    try:
                        self.send_message(chat_id, "Something went wrong handling that. Please try again.")
                    except Exception:
                        log(f"Could not notify chat {chat_id}:\n{traceback.format_exc()}")
            finally:
                self._save_pending()
                if uid is not None:
                    self.state.finish(uid)
                self._handler_context.update_id = None

        return self.dispatcher.submit(chat_id, token, guarded)

    def _schedule_inbox(self):
        blocked_lanes = set()
        for uid, chat_id, update in self.state.queued():
            lane = int(chat_id) % len(self.dispatcher.queues)
            if lane in blocked_lanes:
                continue
            if "callback_query" in update:
                handler = self.handle_callback_query
            else:
                handler = self.handle_photo_message if update["message"].get("photo") else self.handle_message
            if self._spawn_handler(handler, update, chat_id) is False:
                blocked_lanes.add(lane)  # Keep FIFO without blocking unrelated lanes.

    def process_updates(self):
        self._schedule_inbox()
        updates = self.get_updates()
        accepted = []
        offset = self.update_id
        for update in sorted(updates["result"], key=lambda u: u["update_id"]):
            offset = max(offset, update["update_id"])
            message = update.get("message") or update.get("callback_query", {}).get("message", {})
            chat_id = message.get("chat", {}).get("id")
            if self.is_authorized(chat_id) and ("callback_query" in update or message.get("text") or message.get("photo")):
                accepted.append((update["update_id"], chat_id, update))
                log(f"[{chat_id}] {_scrub(message.get('text', '[callback/photo]'))}")
        if self.state.enqueue(accepted, offset, self.settings.WORKERS * self.settings.QUEUE_SIZE):
            self.update_id = offset  # Ack only after the whole accepted batch is on disk.
        else:
            self._backoff("Inbox is full")
        self._schedule_inbox()
        self.cleanup_expired_drafts()

    def start(self):
        next_registration = 0
        while not self.stop.is_set():
            try:
                if time.monotonic() >= next_registration:
                    registered = self.register_commands()
                    next_registration = float("inf") if registered else time.monotonic() + 300
                self.process_updates()
            except KeyboardInterrupt:
                raise
            except Exception:
                # One bad update or a network blip must not terminate the bot.
                log(f"Poll cycle crashed:\n{traceback.format_exc()}")
                self._backoff("Poll cycle crashed")

    def close(self):
        if not self._closed:
            self.stop.set()
            self.dispatcher.close()
            self._downloads.shutdown(wait=True)
            self._save_pending()
            self.state.close()
            self._closed = True
