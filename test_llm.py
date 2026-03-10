#!/usr/bin/env python3
"""
Interactive LLM Beancount Generator Test Tool
用于测试 LLM 生成 beancount 记录的交互式工具
"""

import importlib
import json
import re
from datetime import datetime

import pytz
import requests

import prompts


def load_config():
    """从 config.json 加载配置"""
    with open("config.json", "r") as f:
        return json.load(f)


def parse_accounts(config):
    """从 GitHub repo 解析账户列表"""
    import base64
    
    GITHUB_URL_BASE = "https://api.github.com"
    GITHUB_TOKEN = config["GITHUB_TOKEN"]
    REPO_OWNER = config["REPO_OWNER"]
    REPO_NAME = config["REPO_NAME"]
    BRANCH_NAME = config["BRANCH_NAME"]
    
    list_headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    url = f"{GITHUB_URL_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/accounts?ref={BRANCH_NAME}"
    r = requests.get(url, headers=list_headers)
    if r.status_code != 200:
        print(f"Error fetching accounts: {r.status_code}")
        print(r.text)
        return []
    
    accounts = []
    for item in r.json():
        if not item["name"].endswith(".bean"):
            continue
        file_r = requests.get(item["url"], headers=list_headers)
        if file_r.status_code != 200:
            continue
        content = base64.b64decode(file_r.json()["content"]).decode("utf-8")
        for m in re.findall(r'\d{4}-\d{2}-\d{2} open (.*)', content):
            accounts.append(m.strip().split(" ", 1)[0])
        for m in re.findall(r'\d{4}-\d{2}-\d{2} close (.*)', content):
            closed = m.strip().split(" ", 1)[0]
            if closed in accounts:
                accounts.remove(closed)
    
    accounts.sort()
    return accounts


def prefer_current_account(account: str, accounts: list[str]) -> str:
    """优先选择 Current 账户"""
    accounts_by_lower = {a.lower(): a for a in accounts}
    
    if account.lower() in accounts_by_lower:
        return accounts_by_lower[account.lower()]
    
    # Don't add :Current suffix for Liabilities accounts (credit cards)
    if not account.lower().startswith("liabilities:"):
        if ":current" not in account.lower():
            current_candidate = f"{account}:Current"
            if current_candidate.lower() in accounts_by_lower:
                return accounts_by_lower[current_candidate.lower()]
    
    return account


def strip_code_fence(text: str) -> str:
    """移除代码围栏标记"""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def normalize_and_validate_llm_entry(entry_text: str, accounts: list[str]) -> str:
    """规范化并验证 LLM 输出的记账条目"""
    text = strip_code_fence(entry_text)
    raw_lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(raw_lines) < 3:
        raise ValueError("LLM output is too short. Expected a transaction header and at least two postings.")
    
    header = raw_lines[0].strip()
    metadata_lines = []
    postings = []
    
    posting_re = re.compile(r'^\s*(\S+)\s+(-?\d+(?:\.\d+)?)\s+(\S+)(?:\s+(.*))?$')
    
    for line in raw_lines[1:]:
        pm = posting_re.match(line)
        if pm:
            account = prefer_current_account(pm.group(1), accounts)
            amount = pm.group(2)
            currency = pm.group(3)
            rest = (pm.group(4) or "").strip()
            postings.append({
                "account": account,
                "amount": amount,
                "currency": currency,
                "rest": rest,
            })
            continue
        
        # Keep metadata/comment-like lines and normalize indentation.
        metadata_lines.append(f"  {line.strip()}")
    
    if len(postings) < 2:
        raise ValueError("LLM output must contain at least two postings.")
    
    currencies = set(p["currency"] for p in postings)
    if len(currencies) == 1:
        total = sum(float(p["amount"]) for p in postings)
        if abs(total) > 0.0001:
            raise ValueError(f"LLM output invalid: postings do not balance (sum = {total:.4f}).")
    
    if len(postings) == 2:
        a0 = float(postings[0]["amount"])
        a1 = float(postings[1]["amount"])
        c0 = postings[0]["currency"]
        c1 = postings[1]["currency"]
        r0 = postings[0]["rest"]
        r1 = postings[1]["rest"]
        
        if a0 * a1 >= 0:
            raise ValueError("LLM output invalid: two postings must be one positive and one negative.")
        
        if c0 == c1 and abs(a0 + a1) > 0.0001:
            raise ValueError(f"LLM output invalid: same-currency postings are unbalanced ({a0} + {a1} != 0).")
        
        if c0 != c1:
            has_cost_or_price = any(('@' in r or '{' in r) for r in [r0, r1])
            if not has_cost_or_price:
                # Auto-insert FX price annotation when LLM misses @/@@ on cross-currency postings.
                abs0, abs1 = abs(a0), abs(a1)
                if abs0 == 0 and abs1 == 0:
                    raise ValueError("LLM output invalid: zero amounts in cross-currency postings.")
                
                # Put cost mark on the side that yields the larger numerical FX rate.
                if abs0 <= abs1 and abs0 != 0:
                    rate = abs1 / abs0
                    rate_str = f"{rate:.8f}".rstrip('0').rstrip('.')
                    postings[0]["rest"] = (postings[0]["rest"] + f" @ {rate_str} {c1}").strip()
                elif abs1 != 0:
                    rate = abs0 / abs1
                    rate_str = f"{rate:.8f}".rstrip('0').rstrip('.')
                    postings[1]["rest"] = (postings[1]["rest"] + f" @ {rate_str} {c0}").strip()
                else:
                    # abs0 > 0, abs1 == 0: degenerate cross-currency posting; cannot infer FX rate.
                    raise ValueError(
                        "LLM output invalid: one cross-currency posting has zero amount; cannot infer FX rate."
                    )
    
    account_width = max(len(p["account"]) for p in postings) + 2
    amount_width = max(len(str(p["amount"])) for p in postings) + 2
    currency_width = max(len(p["currency"]) for p in postings) + 2
    
    out = [header]
    out.extend(metadata_lines)
    for p in postings:
        line = (
            "  "
            + p["account"].ljust(account_width)
            + " "
            + p["amount"].rjust(amount_width)
            + " "
            + p["currency"].ljust(currency_width)
        )
        if p["rest"]:
            line += f" {p['rest']}"
        out.append(line.rstrip())
    
    return "\n".join(out)


def parse_llm_backends(config) -> list[dict]:
    """从 config 解析 LLM 后端列表，支持新数组格式和旧单配置项格式"""
    raw = config.get("LLM_BACKENDS")
    if isinstance(raw, list):
        backends = []
        for b in raw:
            url = b.get("LLM_API_BASE_URL", "").rstrip("/")
            key = b.get("LLM_API_KEY", "")
            model = b.get("LLM_MODEL", "")
            if url and key and model:
                backends.append({"base_url": url, "api_key": key, "model": model})
        return backends
    # Backward compat: single-backend keys
    url = config.get("LLM_API_BASE_URL", "").rstrip("/")
    key = config.get("LLM_API_KEY", "")
    model = config.get("LLM_MODEL", "")
    if url and key and model:
        return [{"base_url": url, "api_key": key, "model": model}]
    return []


def call_llm(config, user_input: str, accounts: list[str], txn_date: str) -> str:
    """调用 LLM API 生成 beancount 记录，按顺序尝试所有后端，失败则 fallback"""
    # 重新加载 prompts 模块以获取最新的 prompt 定义
    importlib.reload(prompts)

    backends = parse_llm_backends(config)
    if not backends:
        raise ValueError("LLM configuration incomplete. Set LLM_BACKENDS array (or LLM_API_BASE_URL/LLM_API_KEY/LLM_MODEL) in config.json")

    system_prompt = prompts.BEANCOUNT_SYSTEM_PROMPT
    user_prompt = prompts.build_user_prompt(txn_date, accounts, user_input)
    payload = {
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    last_error: Exception | None = None
    for backend in backends:
        try:
            url = f"{backend['base_url']}/chat/completions"
            headers = {
                "Authorization": f"Bearer {backend['api_key']}",
                "Content-Type": "application/json",
            }
            response = requests.post(url, headers=headers, json={**payload, "model": backend["model"]}, timeout=60)
            response.raise_for_status()
            data = response.json()
            raw_text = data["choices"][0]["message"]["content"].strip()

            if raw_text.upper().startswith("NEED_ACCOUNT:"):
                guidance = raw_text.split(":", 1)[1].strip() if ":" in raw_text else ""
                raise ValueError(guidance or "请在输入中提供至少一个账户名（或账户后缀）")

            try:
                return normalize_and_validate_llm_entry(raw_text, accounts)
            except Exception as e:
                raise ValueError(f"{e}\nInvalid LLM output:\n{raw_text}") from e

        except ValueError:
            raise
        except Exception as e:
            print(f"  ⚠ Backend '{backend['model']}' failed: {e}, trying next...")
            last_error = e

    raise ValueError(f"All LLM backends failed. Last error: {last_error}")


def main():
    """主函数"""
    print("=" * 60)
    print("LLM Beancount Generator Test Tool")
    print("=" * 60)
    print()
    
    # 加载配置
    try:
        config = load_config()
        print("✓ Config loaded from config.json")
    except Exception as e:
        print(f"✗ Error loading config: {e}")
        return
    
    # 解析账户列表
    print("Fetching accounts from GitHub repo...", end=" ", flush=True)
    try:
        accounts = parse_accounts(config)
        print(f"✓ Found {len(accounts)} accounts")
    except Exception as e:
        print(f"\n✗ Error fetching accounts: {e}")
        return
    
    # 获取时区
    timezone = pytz.timezone(config.get("TIMEZONE", "UTC"))
    
    print()
    print("-" * 60)
    print("Enter your transaction in natural language.")
    print("Type 'quit' or 'exit' to quit.")
    print("Type 'accounts' to see all available accounts.")
    print("-" * 60)
    print()
    
    # 交互循环
    while True:
        try:
            user_input = input("📝 Your input: ").strip()
            
            if not user_input:
                continue
            
            if user_input.lower() in ['quit', 'exit', 'q']:
                print("\nBye! 👋")
                break
            
            if user_input.lower() == 'accounts':
                print("\nAvailable accounts:")
                for acc in accounts:
                    print(f"  - {acc}")
                print()
                continue
            
            # 获取当前日期
            now = datetime.now(timezone)
            txn_date = now.strftime("%Y-%m-%d")
            
            # 调用 LLM
            print("\n🤖 Generating beancount entry...\n")
            result = call_llm(config, user_input, accounts, txn_date)
            
            # 显示结果
            print("✅ Generated entry:")
            print("-" * 60)
            print(result)
            print("-" * 60)
            print()
            
        except KeyboardInterrupt:
            print("\n\nBye! 👋")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}\n")


if __name__ == "__main__":
    main()
