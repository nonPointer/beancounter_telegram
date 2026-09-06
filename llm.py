"""Llm responsibilities of Bot; shared entry points are preserved."""

from prompts import BEANCOUNT_SYSTEM_PROMPT
from prompts import EXPENSE_SCREENSHOT_SYSTEM_PROMPT
from prompts import INVEST_ORDER_SYSTEM_PROMPT
from prompts import JOURNAL_REVIEW_SYSTEM_PROMPT
from prompts import QUERY_ROUTER_SYSTEM_PROMPT
import base64
from prompts import build_expense_screenshot_prompt
from prompts import build_invest_order_prompt
from prompts import build_query_router_prompt
from prompts import build_user_prompt
import json
import re
import time
from bot_utils import (
    HTTP, MAX_BEANCOUNT_RETRIES, _C_BLUE, _C_RESET, extract_json_object, format_query_result,
    log,
)
from bot_utils import LedgerValidationError
from prompts import LEDGER_ERROR_EXPLANATION_SYSTEM_PROMPT, build_ledger_error_explanation_prompt

class LLMMixin:
    def _draft_ledger_context(self, loaded=None):
        if loaded is not None:
            return loaded
        try:
            return self.load_ledger()
        except Exception as exc:
            log(f"Draft context unavailable ({type(exc).__name__}: {exc}); commit validation remains mandatory.")
            return None

    def explain_ledger_error(self, entry_text: str, error: str) -> str:
        header = "这条分录没有通过账本校验，尚未保存。"
        try:
            advice = self._call_llm_backends({"temperature": 0.2, "messages": [
                {"role": "system", "content": LEDGER_ERROR_EXPLANATION_SYSTEM_PROMPT},
                {"role": "user", "content": build_ledger_error_explanation_prompt(entry_text, error)},
            ]}, " ledger-error")
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
        """Classify the input as entry-vs-query and, for a query, produce a BQL.

        Returns {"intent": "entry"} or {"intent": "query", "bql": "..."}. Falls back to
        entry on any failure: a misrouted entry costs the user one tap on ❌, while a
        misrouted query would just be a confusing draft — both recoverable, unlike
        blocking the bot's primary purpose because the router hiccuped.
        """
        payload = {
            "temperature": 0,
            "messages": [
                {"role": "system", "content": QUERY_ROUTER_SYSTEM_PROMPT},
                {"role": "user", "content": build_query_router_prompt(
                    user_input, self._accounts_for_prompt(), today)},
            ],
        }
        try:
            raw = self._call_llm_backends(payload, " router")
        except Exception as e:
            log(f"Intent router failed ({e}); treating input as an entry.")
            return {"intent": "entry"}

        parsed = extract_json_object(raw)
        if not parsed or parsed.get("intent") not in ("entry", "query"):
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
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": QUERY_ROUTER_SYSTEM_PROMPT},
                    {"role": "user", "content": build_query_router_prompt(
                        user_input, accounts, today, previous_bql=bql, bql_error=error)},
                ],
            }
            raw = self._call_llm_backends(payload, " bql-retry")
            parsed = extract_json_object(raw)
            if not parsed or not parsed.get("bql"):
                break
            bql = parsed["bql"]

        raise ValueError(f"Could not build a working query. Last error:\n{error}")

    def _call_llm_backends(self, payload: dict, log_prefix: str = "", vision: bool = False) -> str:
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
        last_error: Exception | None = None
        for backend in self.settings.LLM_BACKENDS:
            model = backend.get("vision_model", backend["model"]) if vision else backend["model"]
            try:
                url = f"{backend['base_url']}/chat/completions"
                headers = {
                    "Authorization": f"Bearer {backend['api_key']}",
                    "Content-Type": "application/json",
                }
                started = time.monotonic()
                response = HTTP.post(url, headers=headers, json={**payload, "model": model}, timeout=60)
                response.raise_for_status()
                data = response.json()
                try:
                    content = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as e:
                    raise ValueError(f"Malformed LLM response: {data}") from e
                if content is None:
                    raise ValueError(f"LLM returned null content: {data}")
                elapsed = time.monotonic() - started
                log(f"LLM{log_prefix} answered by {_C_BLUE}[{model}]{_C_RESET} @ {backend['base_url']} ({elapsed:.1f}s)\nResponse content:\n{content}")
                return content.strip()
            except Exception as e:
                log(f"LLM backend '{model}'{log_prefix} failed: {e}, trying next...")
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
        loaded=None,
    ) -> str:
        if not self.llm_enabled:
            raise ValueError(self.llm_unavailable_message())

        accounts_for_prompt = self._accounts_for_prompt()
        loaded = self._draft_ledger_context(loaded)
        validation_error = None
        entry = None
        ledger_error = None

        for attempt in range(1 + MAX_BEANCOUNT_RETRIES):
            if attempt == 0:
                prompt_draft = previous_draft
                prompt_reason = decline_reason
            else:
                prompt_draft = entry
                prompt_reason = f"Previous draft validation error: {validation_error}"

            user_prompt = build_user_prompt(txn_date, accounts_for_prompt, user_input, prompt_draft, prompt_reason, current_time, examples, payees)
            payload = {
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": BEANCOUNT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            }

            raw_text = self._call_llm_backends(payload)

            if raw_text.upper().startswith("NEED_ACCOUNT:"):
                guidance = raw_text.split(":", 1)[1].strip() if ":" in raw_text else ""
                raise ValueError(guidance or "请在输入中提供至少一个账户名（或账户后缀），我才能生成分录。")

            try:
                entry = self.normalize_and_validate_llm_entry(raw_text, accounts)
            except Exception as e:
                raise ValueError(f"{e}\nInvalid LLM output:\n{raw_text}") from e

            validation_error = self.validate_beancount_syntax(entry) or self.validate_accounts_exist(entry, accounts)
            ledger_error = self.validate_entry_against_ledger(entry, loaded) if validation_error is None and loaded is not None else None
            validation_error = validation_error or ledger_error
            if validation_error is None:
                return entry

            log(f"Draft validation failed (attempt {attempt + 1}/{1 + MAX_BEANCOUNT_RETRIES}): {validation_error}")

        if ledger_error:
            raise LedgerValidationError(self.explain_ledger_error(entry, ledger_error))
        raise ValueError(f"Draft validation failed after {MAX_BEANCOUNT_RETRIES} retries: {validation_error}")

    def _call_vision_with_retry(
        self, image_bytes: bytes, accounts: list[str],
        system_prompt: str, base_prompt: str, temperature: float, log_label: str,
    ) -> str:
        if not self.llm_enabled:
            raise ValueError(self.llm_unavailable_message())

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        loaded = self._draft_ledger_context()
        validation_error = None
        entry = None
        ledger_error = None

        for attempt in range(1 + MAX_BEANCOUNT_RETRIES):
            if attempt == 0:
                prompt_text = base_prompt
            else:
                prompt_text = (
                    f"{base_prompt}\n\n"
                    f"Previous draft had validation errors:\n{entry}\n\n"
                    f"Error: {validation_error}\n\n"
                    "Fix the errors and regenerate."
                )

            if attempt == 0:
                user_content = [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": prompt_text},
                ]
            else:
                # Retries are driven only by textual validation errors
                # (syntax / account-exists), which the pixels rarely help fix.
                # Drop the image on retry to avoid re-billing up to 3x image
                # tokens; the full prompt text (incl. account list) is kept so
                # account-exists corrections still work.
                log(f"Retry attempt {attempt}: dropping screenshot, sending text-only correction")
                user_content = [
                    {"type": "text", "text": prompt_text},
                ]

            payload = {
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            }

            raw_text = self._call_llm_backends(payload, f" {log_label}", vision=True)

            try:
                entry = self.normalize_and_validate_llm_entry(raw_text, accounts)
            except Exception as e:
                raise ValueError(f"{e}\nInvalid LLM output:\n{raw_text}") from e

            validation_error = self.validate_beancount_syntax(entry) or self.validate_accounts_exist(entry, accounts)
            ledger_error = self.validate_entry_against_ledger(entry, loaded) if validation_error is None and loaded is not None else None
            validation_error = validation_error or ledger_error
            if validation_error is None:
                return entry

            log(f"Draft validation failed (attempt {attempt + 1}/{1 + MAX_BEANCOUNT_RETRIES}): {validation_error}")

        if ledger_error:
            raise LedgerValidationError(self.explain_ledger_error(entry, ledger_error))
        raise ValueError(f"Draft validation failed after {MAX_BEANCOUNT_RETRIES} retries: {validation_error}")

    def call_openai_vision_invest(self, image_bytes: bytes, accounts: list[str], txn_date: str, caption: str = "", current_datetime: str = "") -> str:
        return self._call_vision_with_retry(
            image_bytes, accounts, INVEST_ORDER_SYSTEM_PROMPT,
            build_invest_order_prompt(txn_date, self._accounts_for_prompt(), caption, current_datetime),
            temperature=0.1, log_label="vision",
        )

    def call_openai_vision_expense(self, image_bytes: bytes, accounts: list[str], txn_date: str, caption: str = "", current_datetime: str = "") -> str:
        return self._call_vision_with_retry(
            image_bytes, accounts, EXPENSE_SCREENSHOT_SYSTEM_PROMPT,
            build_expense_screenshot_prompt(txn_date, self._accounts_for_prompt(), caption, current_datetime),
            temperature=0.2, log_label="vision-expense",
        )

    def review_journal(self, pending: dict, appendix: str):
        if not self.llm_enabled:
            raise ValueError(self.llm_unavailable_message())
        context = {
            "original_input": pending["user_input"],
            "feedback": pending.get("feedback", []),
            "resolved_date": pending["date_str"],
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
            "temperature": 0,
            "messages": [
                {"role": "system", "content": JOURNAL_REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        }, " journal-review", vision=bool(photo))
        # A strict positive verdict is required; malformed or uncertain output blocks writes.
        verdict = json.loads(raw)
        if not isinstance(verdict, dict) or verdict.get("approved") is not True:
            reason = verdict.get("reason", "审核未明确通过") if isinstance(verdict, dict) else "审核格式无效"
            raise ValueError(f"分录与输入一致性审核未通过：{reason}")
        if not isinstance(verdict.get("reason"), str) or not verdict["reason"].strip():
            raise ValueError("审核缺少理由，未提交。")
