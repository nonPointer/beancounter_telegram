"""Llm responsibilities of Bot; shared entry points are preserved."""

import base64
import json
import re
import time
from .settings import llm_headers
from .bot_utils import (
    HTTP, MAX_BEANCOUNT_RETRIES, _C_BLUE, _C_RESET, extract_json_object, format_query_result,
    log, NOT_LOADED, TELEGRAM_MESSAGE_LIMIT, _capped_code_block, _is_account_error,
    JournalReviewRejected, LedgerValidationError,
)
from .prompts import (
    BEANCOUNT_SYSTEM_PROMPT, EXPENSE_SCREENSHOT_SYSTEM_PROMPT, INVEST_ORDER_SYSTEM_PROMPT,
    JOURNAL_REVIEW_SYSTEM_PROMPT, LEDGER_ERROR_EXPLANATION_SYSTEM_PROMPT, QUERY_ROUTER_SYSTEM_PROMPT,
    build_expense_screenshot_prompt, build_invest_order_prompt, build_ledger_error_explanation_prompt,
    build_query_router_prompt, build_user_prompt,
)

class LLMMixin:
    def handle_natural_language(self, chat_id, text, user_input, date_str, current_time=""):
        """Route a prepared text input, keeping the original text for final review."""
        def reply(message):
            self.send_message(chat_id, message)

        commit_message = 'Add entry by Telegram Bot\n\n'
        log("Single-line natural language detected, forwarding to LLM")
        if not self.llm_enabled:
            reply(self.llm_unavailable_message())
            return

        accounts = self.parse_accounts()
        if not accounts:
            reply("No accounts available. Please check GitHub account parsing first.")
            return

        route = self.route_intent(text, date_str)
        if route["intent"] == "analysis":
            try:
                for message in self.analyze_ledger(text, date_str):
                    self.send_message(chat_id, message, parse_mode="HTML")
            except Exception as e:
                log(f"Analysis failed: {type(e).__name__}")
                reply("分析未完成，账本未修改。请稍后重试。")
            return
        if route["intent"] == "query":
            try:
                bql, rendered = self.answer_query(text, route["bql"], date_str)
            except Exception as e:
                log(f"Query failed: {e}")
                reply(f"查询未完成：{e}")
                return
            # Send only the formatted result. The BQL stays in the console log
            # (Query BQL from LLM / BQL ok) — it's noise to the person asking.
            # format_query_result caps by code points; go through the UTF-16-aware
            # capper so an emoji-heavy table can't still overflow Telegram's cap on
            # this HTML path (which bypasses send_message's plain-text guard).
            block, _ = _capped_code_block(rendered, TELEGRAM_MESSAGE_LIMIT)
            self.send_message(chat_id, block, parse_mode="HTML")
            return

        # Between the two LLM calls that a text entry already makes (router above,
        # generator below), do free local lookups to enrich the draft prompt. Load the
        # ledger once here and hand it to both helpers so they don't each pay a separate
        # GitHub round trip for the same tree. Best-effort — never blocks.
        try:
            loaded_ledger = self.load_ledger()
        except Exception as e:
            log(f"Ledger load for payee context failed ({e}); generating without it.")
            loaded_ledger = None

        # If the router named a payee, feed the user's own past entries for that
        # merchant to the generator so it matches their account/narration conventions.
        examples = None
        payee_raw = route.get("payee")
        payee_hint = payee_raw.strip() if isinstance(payee_raw, str) else ""
        if payee_hint:
            try:
                examples = self.examples_for_payee(payee_hint, loaded=loaded_ledger)
                if examples:
                    log(f"Injecting past entries for payee {payee_hint!r} into the draft prompt.")
            except Exception as e:
                log(f"Payee-history lookup failed ({e}); generating without examples.")

        # Also hand the LLM the user's most-used merchant names so it snaps a fuzzy
        # input onto an existing payee instead of coining a near-duplicate.
        payees = None
        try:
            payees = self.frequent_payees(loaded=loaded_ledger)
        except Exception as e:
            log(f"Frequent-payee lookup failed ({e}); generating without payee list.")

        try:
            pending = self._prepare_llm_pending(chat_id, commit_message, user_input, date_str, prompt=text)
            appendix = self.call_openai_compatible(text, accounts, date_str, current_time=current_time,
                                                  examples=examples, payees=payees, loaded=loaded_ledger, pending=pending)
            self.publish_llm_draft(
                chat_id, appendix, commit_message, user_input, date_str,
                header="草稿：", prompt=text, pending=pending,
            )
            return
        except Exception as e:
            log(f"LLM generation failed: {e}")
            error_text = str(e)
            if isinstance(e, LedgerValidationError) or _is_account_error(error_text):
                reply(error_text)
            else:
                reply(f"LLM generation failed: {e}")
            return


    def _draft_ledger_context(self, loaded=NOT_LOADED):
        if loaded is not NOT_LOADED:
            return loaded
        try:
            return self.load_ledger()
        except Exception as exc:
            log(f"Draft context unavailable ({type(exc).__name__}: {exc}); commit validation remains mandatory.")
            return None

    def explain_ledger_error(self, entry_text: str, error: str) -> str:
        header = "这条分录没有通过账本校验，尚未保存。"
        try:
            advice = self._call_llm_backends({"messages": [
                {"role": "system", "content": LEDGER_ERROR_EXPLANATION_SYSTEM_PROMPT},
                {"role": "user", "content": build_ledger_error_explanation_prompt(entry_text, error)},
            ]}, "账本校验错误解释")
            if advice.strip():
                return f"{header}\n{advice.strip()}"
        except Exception:
            pass
        return f"{header}\n错误信息：{error}"

    def llm_unavailable_message(self) -> str:
        if self.llm_enabled:
            return ""
        missing = ", ".join(["LLM_BACKENDS"])
        return (
            "LLM is not fully configured, unable to process natural language. "
            f"Missing: {missing}."
        )

    def route_intent(self, user_input: str, today: str) -> dict:
        """Classify entry, simple query or multi-step analysis.

        Returns entry (optional payee), query (with BQL), or analysis. Retains the existing entry fallback on router failure; any resulting draft still requires the normal checks before saving.
        """
        payload = {
            "messages": [
                {"role": "system", "content": QUERY_ROUTER_SYSTEM_PROMPT},
                {"role": "user", "content": build_query_router_prompt(
                    user_input, self._accounts_for_prompt(), today)},
            ],
        }
        try:
            raw = self._call_llm_backends(payload, "意图识别、payee 提取及查询语句生成")
        except Exception as e:
            log(f"Intent router failed ({e}); treating input as an entry.")
            return {"intent": "entry"}

        parsed = extract_json_object(raw)
        if not parsed or parsed.get("intent") not in ("entry", "query", "analysis"):
            log(f"Intent router returned unusable output {raw!r}; treating input as an entry.")
            return {"intent": "entry"}
        if parsed["intent"] == "query" and not parsed.get("bql"):
            log("Intent router said query but gave no BQL; treating input as an entry.")
            return {"intent": "entry"}
        if parsed["intent"] == "query":
            log(f"Query BQL from LLM: {parsed['bql']}")
        return parsed

    def answer_query(self, user_input: str, bql: str, today: str) -> tuple[str, str]:
        """Run the BQL, re-asking the LLM to fix it if beancount rejects it.

        Mirrors the beancount-syntax retry loop used for entries. Returns (bql, rendered).
        """
        accounts = self._accounts_for_prompt()
        # The ledger can't change across retries, so load it once here rather than paying a
        # fetch+parse (or, on the no-tree-sha fallback path, a full re-download) each attempt.
        loaded = self.load_ledger()
        if loaded is None:
            raise ValueError("Failed to download the ledger from GitHub.")
        error = None
        for attempt in range(1 + MAX_BEANCOUNT_RETRIES):
            try:
                rtypes, rrows = self.run_bql(bql, loaded=loaded)
                log(f"BQL ok ({len(rrows)} rows): {bql}")
                return bql, format_query_result(rtypes, rrows)
            except ValueError:
                raise  # ledger download failure — not something the LLM can fix
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                log(f"BQL failed (attempt {attempt + 1}/{1 + MAX_BEANCOUNT_RETRIES}): {error}")

            if attempt == MAX_BEANCOUNT_RETRIES:
                break

            payload = {
                "messages": [
                    {"role": "system", "content": QUERY_ROUTER_SYSTEM_PROMPT},
                    {"role": "user", "content": build_query_router_prompt(
                        user_input, accounts, today, previous_bql=bql, bql_error=error)},
                ],
            }
            raw = self._call_llm_backends(payload, f"查询语句纠错（重试 {attempt + 1}）")
            parsed = extract_json_object(raw)
            if not parsed or not parsed.get("bql"):
                break
            bql = parsed["bql"]

        raise ValueError(f"Could not build a working query. Last error:\n{error}")

    def _with_user_preferences(self, payload: dict) -> dict:
        # Read on every logical request so editing user.md needs no restart.
        path = self.settings.USER_PROMPT_PATH
        custom_prompt = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        custom_prompt = re.sub(r"<!--.*?-->", "", custom_prompt, flags=re.DOTALL).strip()
        if custom_prompt:
            payload = {**payload, "messages": [
                {"role": "system", "content": (
                    "以下是用户自定义偏好，应用于当前任务；不改变当前任务的输出格式、"
                    "审核职责或通过条件：\n" + custom_prompt)},
                *payload.get("messages", []),
            ]}
        return payload

    def _request_llm_message(self, backend, payload, purpose, *, vision=False, timeout=60):
        """Transport shared by text responses and Chat Completions tool messages."""
        model = backend.get("vision_model", backend["model"]) if vision else backend["model"]
        label = f" [用途：{purpose.strip() or '分录生成'}]"
        started = time.monotonic()
        log(f"LLM{label} requesting [{model}] @ {backend['base_url']}")
        response = HTTP.post(
            f"{backend['base_url']}/chat/completions",
            headers=llm_headers(backend),
            json={**payload, "model": model}, timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        try:
            message = data["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError("message must be an object")
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError("Malformed LLM response") from e
        content = message.get("content")
        shown = content if content is not None else json.dumps(message.get("tool_calls"), ensure_ascii=False)
        log(f"LLM{label} answered by {_C_BLUE}[{model}]{_C_RESET} @ {backend['base_url']} ({time.monotonic() - started:.1f}s)\nResponse content:\n{shown}")
        return message

    def _call_llm_backends(self, payload: dict, log_prefix: str = "", vision: bool = False) -> str:
        payload = self._with_user_preferences(payload)
        last_error: Exception | None = None
        for backend in self.settings.LLM_BACKENDS:
            model = backend.get("vision_model", backend["model"]) if vision else backend["model"]
            try:
                message = self._request_llm_message(backend, payload, log_prefix, vision=vision)
                content = message.get("content")
                if not isinstance(content, str):
                    raise ValueError("LLM returned non-text content")
                return content.strip()
            except Exception as e:
                log(f"LLM backend '{model}' [用途：{log_prefix.strip() or '分录生成'}] failed: {e}, trying next...")
                last_error = e
        raise ValueError(f"All LLM backends failed. Last error: {last_error}")

    def call_openai_compatible(
        self,
        user_input: str,
        accounts: list[str],
        txn_date: str,
        previous_draft: str | None = None,
        decline_reason: str | None = None,
        current_time: str = "",
        examples: str | None = None,
        payees: list[str] | None = None,
        loaded=NOT_LOADED,
        pending=None,
    ) -> str:
        accounts_for_prompt = self._accounts_for_prompt()

        def build_payload(attempt, entry, error):
            prompt_draft = entry if attempt else previous_draft
            prompt_reason = f"Previous draft validation error: {error}" if attempt else decline_reason
            return {"messages": [
                {"role": "system", "content": BEANCOUNT_SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(
                    txn_date, accounts_for_prompt, user_input, prompt_draft, prompt_reason,
                    current_time, examples, payees)},
            ]}

        purpose = "根据用户反馈修改分录" if previous_draft is not None else "文本分录生成"
        return self._generate_validated_entry(build_payload, accounts, purpose, loaded=loaded, pending=pending)

    def _call_vision_with_retry(
        self, image_bytes: bytes, accounts: list[str],
        system_prompt: str, base_prompt: str, log_label: str,
        *, pending=None,
    ) -> str:
        b64 = base64.b64encode(image_bytes).decode("utf-8")

        def build_payload(attempt, entry, error):
            prompt_text = base_prompt
            if attempt:
                prompt_text += (
                    f"\n\nPrevious draft had validation errors:\n{entry}\n\n"
                    f"Error: {error}\n\nFix the errors and regenerate."
                )
                log(f"Retry attempt {attempt}: dropping screenshot, sending text-only correction")
            # Only the initial request needs pixels; correction retries use the
            # draft and validation errors, avoiding repeated image-token costs.
            content = [] if attempt else [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]
            content.append({"type": "text", "text": prompt_text})
            return {"messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ]}

        return self._generate_validated_entry(build_payload, accounts, log_label, vision=True, pending=pending)

    def _generate_validated_entry(self, build_payload, accounts, purpose, *, loaded=NOT_LOADED, vision=False, pending=None):
        """One retry/validation pipeline for text, feedback and screenshot drafts."""
        if not self.llm_enabled:
            raise ValueError(self.llm_unavailable_message())
        files = None
        if pending is not None:
            _, files = self._download_ledger_snapshot()
        else:
            loaded = self._draft_ledger_context(loaded)
        entry = validation_error = ledger_error = None
        for attempt in range(1 + MAX_BEANCOUNT_RETRIES):
            payload = build_payload(attempt, entry, validation_error)
            retry_label = f" / 校验纠错（{'纯文本重试' if vision else '重试'} {attempt}）" if attempt else ""
            raw_text = self._call_llm_backends(payload, purpose + retry_label, vision=vision)
            if not vision and raw_text.upper().startswith("NEED_ACCOUNT:"):
                guidance = raw_text.split(":", 1)[1].strip()
                raise ValueError(guidance or "请在输入中提供至少一个账户名（或账户后缀），我才能生成分录。")
            try:
                entry = self.normalize_and_validate_llm_entry(raw_text, accounts)
            except Exception as exc:
                raise ValueError(f"{exc}\nInvalid LLM output:\n{raw_text}") from exc
            validation_error = self.validate_beancount_syntax(entry) or self.validate_accounts_exist(entry, accounts)
            ledger_error = None
            if validation_error is None:
                if pending is not None:
                    self._finalize_pending_entry(pending, entry)
                    try:
                        self._validate_pending_draft(pending, files)
                    except ValueError as exc:
                        ledger_error = str(exc)
                elif loaded is not None:
                    ledger_error = self.validate_entry_against_ledger(entry, loaded)
            validation_error = validation_error or ledger_error
            if validation_error is None:
                return entry
            log(f"Draft validation failed (attempt {attempt + 1}/{1 + MAX_BEANCOUNT_RETRIES}): {validation_error}")
        if ledger_error:
            raise LedgerValidationError(self.explain_ledger_error(entry, ledger_error))
        raise ValueError(f"Draft validation failed after {MAX_BEANCOUNT_RETRIES} retries: {validation_error}")

    def call_openai_vision_invest(self, image_bytes: bytes, accounts: list[str], txn_date: str, caption: str = "", current_datetime: str = "", *, pending=None) -> str:
        return self._call_vision_with_retry(
            image_bytes, accounts, INVEST_ORDER_SYSTEM_PROMPT,
            build_invest_order_prompt(txn_date, self._accounts_for_prompt(), caption, current_datetime),
            log_label="投资截图识别与分录生成",
            pending=pending,
        )

    def call_openai_vision_expense(self, image_bytes: bytes, accounts: list[str], txn_date: str, caption: str = "", current_datetime: str = "", *, pending=None) -> str:
        return self._call_vision_with_retry(
            image_bytes, accounts, EXPENSE_SCREENSHOT_SYSTEM_PROMPT,
            build_expense_screenshot_prompt(txn_date, self._accounts_for_prompt(), caption, current_datetime),
            log_label="消费截图识别与分录生成",
            pending=pending,
        )

    def review_journal(self, pending: dict, appendix: str):
        if not self.llm_enabled:
            raise ValueError(self.llm_unavailable_message())
        # Derive provenance from the persisted draft, including drafts created before
        # this field existed and retries with an already persisted commit_appendix.
        datetime_pattern = r'^\s*datetime\s*:\s*".*"\s*$'
        draft_datetimes = {
            line.strip() for line in pending["appendix"].splitlines()
            if re.match(datetime_pattern, line)
        }
        context = {
            "original_input": pending["user_input"],
            "feedback": pending.get("feedback", []),
            "resolved_date": pending["date_str"],
            "system_generated_datetime_metadata": [
                line.strip() for line in appendix.splitlines()
                if re.match(datetime_pattern, line) and line.strip() not in draft_datetimes
            ],
            "accounts": self._accounts_for_prompt(),
            "journal": appendix,
        }
        content = [{"type": "text", "text": json.dumps(context, ensure_ascii=False)}]
        photo = pending.get("photo_file_id")
        if photo:
            image_bytes = self.get_telegram_file_bytes(photo)
            if not image_bytes:
                raise ValueError("无法取得原始截图，审核未通过。")
            b64 = base64.b64encode(image_bytes).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        raw = self._call_llm_backends({
            "messages": [
                {"role": "system", "content": JOURNAL_REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        }, "保存前分录与原始输入一致性审核", vision=bool(photo))
        # Accept one JSON object, optionally enclosed in a Markdown fence. Do
        # not extract a positive fragment from prose or contradictory verdicts.
        fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", raw.strip(), re.DOTALL | re.IGNORECASE)

        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate review field")
                result[key] = value
            return result

        try:
            verdict = json.loads(fenced[1] if fenced else raw, object_pairs_hook=unique_fields)
        except ValueError:
            raise ValueError("保存前审核的回复格式无效，无法确认是否通过，尚未提交。请重试审核。") from None
        if (not isinstance(verdict, dict) or not isinstance(verdict.get("approved"), bool)
                or not isinstance(verdict.get("reason"), str) or not verdict["reason"].strip()):
            raise ValueError("保存前审核缺少明确结论或理由，尚未提交。请重试审核。")
        if not verdict["approved"]:
            raise JournalReviewRejected(verdict["reason"].strip())
