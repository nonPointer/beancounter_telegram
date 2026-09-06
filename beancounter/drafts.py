"""Drafts responsibilities of Bot; shared entry points are preserved."""

from .ledger_validation import check_ledger
from datetime import datetime
import requests
import time
import uuid
from .bot_utils import (
    GITHUB_CONFLICT_RETRIES, _capped_code_block, _code_block, _is_account_error, log, timed,
)
from .bot_utils import LedgerValidationError

class DraftMixin:
    def _save_pending_locked(self):
        with timed("draft checkpoint"):
            self.state.set("drafts", {"pending": self.pending_llm_entries, "inflight": self._inflight,
                                     "counter": self.pending_llm_id, "feedback": self.pending_decline_reasons,
                                     "timezone": str(self.timezone)})

    def _save_pending(self):
        with self._pending_lock:
            self._save_pending_locked()

    def _claim_pending_locked(self, pending_id):
        pending = self.pending_llm_entries.pop(pending_id, None)
        if pending is not None:
            self._inflight[pending_id] = pending
            self._save_pending_locked()
        return pending

    def _finish_pending(self, pending_id):
        with self._pending_lock:
            self._inflight.pop(pending_id, None)
            self._remove_decline_reason_bindings_locked(pending_id)
            self._save_pending_locked()

    def _make_pending_entry(self, chat_id: int, appendix: str, commit_message: str, user_input: str, date_str: str) -> dict:
        return {
            "kind": "llm",
            "operation_id": (f"update-{self._handler_context.update_id}"
                             if getattr(self._handler_context, "update_id", None) is not None else uuid.uuid4().hex),
            "auto_confirm": False,  # Armed only after Telegram delivers the review UI.
            "chat_id": chat_id,
            "appendix": appendix,
            "commit_message": commit_message,
            "created_at": time.time(),
            "user_input": user_input,
            "date_str": date_str,
        }

    def commit_llm_entry(self, pending: dict) -> str:
        """Revalidate each immutable snapshot; a stable marker reconciles uncertain PUTs."""
        operation_id = pending.setdefault("operation_id", uuid.uuid4().hex)
        marker = f'; telegram-operation: {operation_id}'
        appendix = pending.setdefault("commit_appendix", self.ensure_datetime_metadata(
            pending["appendix"], datetime.now(self.timezone).isoformat(timespec="seconds")))
        self._save_pending()  # Persist the stable payload before a potentially ambiguous PUT.
        reviewed = False
        for _ in range(GITHUB_CONFLICT_RETRIES):
            tree_sha, files = self._download_ledger_snapshot()
            current = files[self.settings.FILE_PATH]
            if marker in current["content"].splitlines():
                return appendix
            texts = {path: f["content"] for path, f in files.items()}
            texts[self.settings.FILE_PATH] = current["content"] + "\n" + marker + "\n" + appendix + "\n"
            check_ledger(texts, self._ledger_root(texts), self.settings.FILE_PATH)
            if not reviewed:
                if not self.parse_accounts():
                    raise ValueError("无法取得账户上下文，未提交。")
                self.review_journal(pending, appendix)
                reviewed = True
            # A slow audit may have overlapped another commit. Check again and rebuild.
            latest = self._list_bean_files()
            if latest is None:
                raise ValueError("无法确认账本版本，未提交。")
            if latest[0] != tree_sha:
                reviewed = False
                continue
            ok, status = self._github_put_file(
                texts[self.settings.FILE_PATH], current["sha"], pending["commit_message"].strip())
            if ok:
                return appendix
            if status not in (409, 422):
                raise ValueError(f"GitHub 提交失败（HTTP {status}）。")
        raise ValueError("账本正在被其他操作修改，请重试。")

    def approve_pending(self, pending_id: str, pending: dict, automatic: bool = False):
        """The caller owns the claimed draft; all failures retain it for explicit retry."""
        chat_id = pending["chat_id"]
        try:
            appendix = self.commit_llm_entry(pending)
        except Exception as exc:
            log(f"Draft {pending_id} save failed ({type(exc).__name__}: {exc}); draft retained.")
            pending["auto_confirm"] = False
            self._restore_pending(pending_id, pending)
            self.send_message(chat_id, f"未能确认保存：{exc}\n草稿已保留，自动确认已暂停。可点击 ✅ 重试或 🔧 修改。",
                              reply_markup=self.build_review_buttons(pending_id))
            return
        # Notifications must never roll back a successful write into a retryable draft.
        self._finish_pending(pending_id)
        if pending.get("message_id"):
            self.edit_message_reply_markup(chat_id, pending["message_id"])
        label = "超时自动确认，已保存" if automatic else "已保存"
        block, _ = _capped_code_block(appendix, 3800)
        self.send_message(chat_id, f"{label}（本地检查及输入审核通过）：\n{block}", parse_mode="HTML")

    def next_pending_id(self) -> str:
        with self._pending_lock:
            self.pending_llm_id += 1
            self._save_pending_locked()
            return str(self.pending_llm_id)

    def _pop_pending(self, pending_id: str) -> dict | None:
        """Atomically claim (pop) a pending entry. Returns None if already claimed."""
        with self._pending_lock:
            pending = self.pending_llm_entries.pop(pending_id, None)
            self._save_pending_locked()
            return pending

    def _restore_pending(self, pending_id: str, pending: dict) -> dict:
        """Put a claimed entry back after a failed commit, so the user's reviewed draft
        is not lost to a transient GitHub error. The TTL clock restarts: they only just
        interacted with it, and expiring it immediately would defeat the retry."""
        pending = dict(pending, created_at=time.time())
        with self._pending_lock:
            self._inflight.pop(pending_id, None)
            self.pending_llm_entries[pending_id] = pending
            self._save_pending_locked()
        return pending

    def _remove_decline_reason_bindings_locked(self, pending_id):
        for k, v in list(self.pending_decline_reasons.items()):
            if v == pending_id:
                self.pending_decline_reasons.pop(k, None)

    def remove_decline_reason_bindings(self, pending_id: str):
        with self._pending_lock:
            self._remove_decline_reason_bindings_locked(pending_id)
            self._save_pending_locked()

    def add_non_pnl_accounts_to_commit_message(self, commit_message: str, entry_text: str) -> str:
        for account in self.extract_accounts_from_entry(entry_text):
            if not account.startswith(("Expenses", "Income")):
                commit_message += f"{account}\n"
        return commit_message

    def is_pending_expired(self, pending: dict) -> bool:
        created_at = pending.get("created_at", 0)
        return (time.time() - created_at) > self.settings.DRAFT_TTL_SECONDS

    def cleanup_expired_drafts(self):
        with self._pending_lock:
            expired = [(pid, pending["chat_id"]) for pid, pending in self.pending_llm_entries.items()
                       if self.is_pending_expired(pending)
                       and (pending.get("kind") != "llm" or pending.get("auto_confirm", False))]
        for pid, chat_id in expired:
            self._spawn_handler(self._handle_expired_draft, {"_draft_id": pid}, chat_id)

    def _handle_expired_draft(self, update):
        pid = update["_draft_id"]
        with self._pending_lock:
            pending = self.pending_llm_entries.get(pid)
            if not pending or not self.is_pending_expired(pending):
                return
            if pending.get("kind") == "llm" and not pending.get("auto_confirm", False):
                return
            pending = self._claim_pending_locked(pid)
        if pending.get("kind") == "llm":
            self.approve_pending(pid, pending, automatic=True)
        else:
            self._finish_pending(pid)
            self.send_message(pending["chat_id"], f"Draft expired after {self.settings.DRAFT_TTL_SECONDS} seconds and was discarded.")

    def build_review_buttons(self, pending_id: str):
        return {
            "inline_keyboard": [[
                {"text": "✅", "callback_data": f"approve:{pending_id}"},
                {"text": "🔧", "callback_data": f"decline_reason:{pending_id}"},
                {"text": "❌", "callback_data": f"discard:{pending_id}"},
            ]]
        }

    def send_draft_for_review(self, chat_id, header, appendix, pending_id):
        with self._pending_lock:
            pending = self.pending_llm_entries.get(pending_id)
            if pending is not None:
                pending["auto_confirm"] = False
            self._save_pending_locked()
        block, _ = _capped_code_block(appendix, 3500)
        result = self.send_message(chat_id, f"{header}\n{block}\n✅ 保存 · 🔧 修改 · ❌ 放弃\n{self.settings.DRAFT_TTL_SECONDS} 秒内无操作将自动确认；仅在本地 bean-check 和输入一致性审核均通过后保存。", reply_markup=self.build_review_buttons(pending_id), parse_mode="HTML")
        with self._pending_lock:
            pending = self.pending_llm_entries.get(pending_id)
            if pending is not None:
                if isinstance(result, dict) and result.get("ok"):
                    pending["message_id"] = result.get("result", {}).get("message_id")
                    pending["auto_confirm"] = True
                    pending["created_at"] = time.time()
                else:
                    pending["auto_confirm"] = False
            self._save_pending_locked()

    def build_undo_buttons(self, pending_id: str):
        return {
            "inline_keyboard": [[
                {"text": "✅ 确认撤回", "callback_data": f"undo_confirm:{pending_id}"},
                {"text": "❌ 取消",     "callback_data": f"undo_cancel:{pending_id}"},
            ]]
        }

    def run_recheck(self, chat_id: int, pending_id: str, decline_reason: str | None = None):
        with self._pending_lock:
            pending = self.pending_llm_entries.get(pending_id)
            if pending:
                pending["auto_confirm"] = False
                pending["rechecking"] = True
                if decline_reason:
                    pending["feedback"] = pending.get("feedback", []) + [decline_reason]
            self._save_pending_locked()
        if not pending:
            self.send_message(chat_id, "This request is expired or already handled")
            return

        if not self.llm_enabled:
            pending["rechecking"] = False
            self.send_message(chat_id, self.llm_unavailable_message())
            return

        if decline_reason:
            log(f"Running LLM recheck with reason: {decline_reason}")

        try:
            accounts = self.parse_accounts()
            if not accounts:
                raise ValueError("No accounts available. Please check GitHub account parsing first.")
            new_appendix = self.call_openai_compatible(
                pending["user_input"],
                accounts,
                pending["date_str"],
                previous_draft=pending["appendix"],
                decline_reason="\n".join(pending.get("feedback", [])) or None,
                current_time=datetime.now(self.timezone).strftime('%H:%M'),
            )
            new_appendix = self.insert_prompt_metadata(new_appendix, pending["user_input"])
            new_commit_message = self.add_non_pnl_accounts_to_commit_message(
                'Add entry by Telegram Bot\n\n', new_appendix
            )

            new_pending_id = self.next_pending_id()
            with self._pending_lock:
                if self.pending_llm_entries.get(pending_id) is not pending:
                    return  # User discarded the original while generation was running.
                self.pending_llm_entries[new_pending_id] = self._make_pending_entry(
                    chat_id, new_appendix, new_commit_message, pending["user_input"], pending["date_str"]
                )
                replacement = self.pending_llm_entries[new_pending_id]
                replacement["feedback"] = pending.get("feedback", [])
                if pending.get("photo_file_id"):
                    replacement["photo_file_id"] = pending["photo_file_id"]
                self.pending_llm_entries.pop(pending_id, None)

            log("LLM rechecked draft:\n" + new_appendix)
            self.send_draft_for_review(chat_id, "LLM rechecked draft:", new_appendix, new_pending_id)
        except Exception as e:
            pending["rechecking"] = False
            self._save_pending()
            log(f"LLM recheck failed: {e}")
            error_text = str(e)
            if isinstance(e, LedgerValidationError) or _is_account_error(error_text):
                self.send_message(chat_id, error_text)
            else:
                self.send_message(chat_id, f"LLM recheck failed: {e}")

    def handle_callback_query(self, update):
        callback = update["callback_query"]
        callback_id = callback["id"]
        data = callback.get("data", "")
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")

        if not self.is_authorized(chat_id):
            log(f"Ignoring callback from unauthorized chat_id {chat_id}.")
            return

        try:
            action, pending_id = data.split(":", 1)
        except ValueError:
            self.answer_callback_query(callback_id, "Unknown action")
            return

        # Atomically validate and (for destructive actions) claim the pending entry
        # so concurrent callbacks for the same pending_id cannot double-process it.
        with self._pending_lock:
            pending = self.pending_llm_entries.get(pending_id)
            if not pending:
                self.answer_callback_query(callback_id, "This request is expired or already handled")
                return

            if chat_id != pending["chat_id"]:
                self.answer_callback_query(callback_id, "Not allowed")
                return

            if self.is_pending_expired(pending) and pending.get("kind") != "llm":
                self.pending_llm_entries.pop(pending_id, None)
                self._remove_decline_reason_bindings_locked(pending_id)
                self._save_pending_locked()
                self.answer_callback_query(callback_id, "Expired")
                self.send_message(chat_id, f"Draft expired after {self.settings.DRAFT_TTL_SECONDS} seconds and was discarded.")
                return

            allowed = ("undo_confirm", "undo_cancel") if pending.get("kind") == "undo" else ("approve", "discard", "decline_reason")
            if action not in allowed:
                self.answer_callback_query(callback_id, "Unknown action")
                return

            if pending.get("rechecking") and action != "discard":
                self.answer_callback_query(callback_id, "正在修改，请稍候")
                return

            if action in ("approve", "discard", "undo_confirm", "undo_cancel"):
                # Claim the entry now; prevents any concurrent thread from also processing it.
                pending = (self._claim_pending_locked(pending_id) if action in ("approve", "undo_confirm")
                           else self.pending_llm_entries.pop(pending_id, None))
                if not pending:
                    self.answer_callback_query(callback_id, "This request is expired or already handled")
                    return
            elif action == "decline_reason":
                self.pending_decline_reasons[chat_id] = pending_id
                pending["auto_confirm"] = False
            if action in ("discard", "undo_cancel"):
                self._remove_decline_reason_bindings_locked(pending_id)
            self._save_pending_locked()

        if action == "approve":
            pending["message_id"] = message_id
            # Ack is best effort: a Telegram error must not lose the claimed draft.
            try:
                self.answer_callback_query(callback_id, "正在检查账本和输入…")
            except Exception:
                log("Could not acknowledge approval; continuing validation.")
            self.approve_pending(pending_id, pending)
            return

        if action in ("discard", "undo_cancel"):
            self.edit_message_reply_markup(chat_id, message_id)

        if action == "decline_reason":
            log(f"User requested recheck for pending {pending_id}")
            if not self.llm_enabled:
                self.answer_callback_query(callback_id, "LLM unavailable")
                self.send_message(chat_id, self.llm_unavailable_message())
                return

            self.answer_callback_query(callback_id, "Please send reason")
            self.send_message(chat_id, "Please send your decline reason as plain text. I will send it to LLM for recheck.")
            return

        if action == "discard":
            log(f"User discarded pending {pending_id}")
            self.answer_callback_query(callback_id, "Discarded")
            self.send_message(chat_id, "Discarded. Entry was not saved.")
            return

        if action == "undo_cancel":
            log(f"User cancelled undo for pending {pending_id}")
            self.answer_callback_query(callback_id, "已取消")
            self.send_message(chat_id, "已取消，未作任何更改。")
            return

        if action == "undo_confirm":
            log(f"User confirmed undo for pending {pending_id}")
            # An undo rewrites the whole file, so unlike an append it cannot be retried
            # against a moved sha — a conflict means the precomputed content is stale.
            try:
                ok, status = self._github_put_file(
                    pending["new_content"], pending["file_sha"], pending["commit_message"])
            except requests.RequestException:
                self._restore_pending(pending_id, pending)
                self.send_message(chat_id, "撤回结果未知。请先用 /last 核对账本，再重试。")
                return
            if ok:
                self._finish_pending(pending_id)
                self.answer_callback_query(callback_id, "已撤回")
                self.send_message(
                    chat_id,
                    f"已撤回以下指令：\n{_code_block(pending['transaction_text'])}",
                    parse_mode="HTML",
                )
                log("Undo committed. Removed:\n" + pending["transaction_text"])
            elif status in (409, 422):
                self._finish_pending(pending_id)
                self.answer_callback_query(callback_id, "已过期")
                self.send_message(chat_id, "账本在此期间已变更，撤回已作废。请重新执行 /undo。")
            else:
                self._restore_pending(pending_id, pending)
                self.answer_callback_query(callback_id, "失败")
                self.send_message(chat_id, "Failed to upload to GitHub. 草稿仍在，可再次点击确认重试。")
            return
