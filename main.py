import base64
import json
import re
import sys
import threading
import time
from datetime import datetime
from pprint import pprint

import pytz
import requests
from jinja2 import Environment, FileSystemLoader

with open("config.json", "r") as f:
    config = json.load(f)

GITHUB_URL_BASE = "https://api.github.com"
GITHUB_TOKEN = config["GITHUB_TOKEN"]
REPO_OWNER = config["REPO_OWNER"]
REPO_NAME = config["REPO_NAME"]
BRANCH_NAME = config["BRANCH_NAME"]
FILE_PATH = config["FILE_PATH"]
CHAT_ID = config.get("CHAT_ID", None)

jinja2 = Environment(loader=FileSystemLoader(searchpath="./templates"))


class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


print_lock = threading.Lock()


def log(message):
    with print_lock:
        print(f"\r \r{bcolors.OKGREEN}[{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}]{bcolors.ENDC}", end="")
        if isinstance(message, str):
            print(" " + message)
        else:
            print()
            pprint(message)


class rotating_loading:
    def __init__(self, stop_event: threading.Event):
        self.stop_event = stop_event

    def start(self):
        symbols = ['/', '-', '\\', '|']
        duration = 0.2
        i = 0
        while not self.stop_event.wait(duration):
            with print_lock:
                print('\r' + symbols[i % len(symbols)], end='', flush=True)
            i += 1
        with print_lock:
            print('\r \r', end='', flush=True)


_accounts_cache = {"accounts": None, "ts": 0}
ACCOUNTS_CACHE_TTL = 300  # seconds

GITHUB_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.object",
    "X-GitHub-Api-Version": "2022-11-28"
}


def parse_accounts():
    now = time.time()
    if _accounts_cache["accounts"] is not None and now - _accounts_cache["ts"] < ACCOUNTS_CACHE_TTL:
        return _accounts_cache["accounts"]

    list_headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    url = f"{GITHUB_URL_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/accounts?ref={BRANCH_NAME}"
    r = requests.get(url, headers=list_headers)
    if r.status_code != 200:
        log(f"Error fetching accounts: {r.status_code}")
        log(r.text)
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
    
    _accounts_cache["accounts"] = accounts
    _accounts_cache["ts"] = now
    return accounts


def match_account(account_suffix: str) -> str | None:
    accounts = parse_accounts()
    suffix_lower = account_suffix.lower()
    matches = [a for a in accounts if a.lower().endswith(suffix_lower)]
    if not matches:
        log(f"No matching account for suffix: {account_suffix}")
        log(f"Available accounts: {accounts}")
    return matches[0] if matches else None


class Bot:
    def __init__(self, debug: bool = False):
        self.update_id = 0
        self.debug = debug
        self.stop = threading.Event()
        self.timezone = pytz.timezone(config["TIMEZONE"])
        self.api_base = "https://api.telegram.org/bot{}".format(config["TELEGRAM_BOT_TOKEN"])

    def send_message(self, chat_id, text):
        data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        response = requests.post(self.api_base + "/sendMessage", data=data)
        if response.status_code != 200:
            log(f"Error sending message: {response.status_code}")
            log(response.text)
        return response.json()

    def github_download_file(self) -> dict | None:
        url = f"{GITHUB_URL_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{FILE_PATH}?ref={BRANCH_NAME}"
        r = requests.get(url=url, headers=GITHUB_HEADERS)
        if r.status_code == 200:
            return {
                "content": base64.b64decode(r.json()["content"]).decode("utf-8"),
                "sha": r.json()["sha"]
            }
        elif r.status_code == 404:
            log("File not found.")
            return {"content": "", "sha": ""}
        else:
            log(f"Error: {r.status_code}")
            return None

    def github_upload_file(self, content: str, sha: str, commit_message: str) -> bool:
        url = f"{GITHUB_URL_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{FILE_PATH}"
        data = {
            "message": commit_message,
            "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
            "branch": BRANCH_NAME,
            "sha": sha
        }
        r = requests.put(url=url, headers=GITHUB_HEADERS, json=data)
        if r.status_code in [200, 201]:
            return True
        else:
            log(f"Error uploading file: {r.status_code}")
            log(r.text)
            return False

    def handle_message(self, message):
        text = message["message"]["text"]
        chat_id = message["message"]["chat"]["id"]

        def reply(text: str):
            self.send_message(chat_id, text)

        if CHAT_ID and str(chat_id) != CHAT_ID:
            log(f"Ignoring message from chat_id {chat_id}, only responding to {CHAT_ID}.")
            reply("How dare you?")
            return

        dt = datetime.now(self.timezone)
        date_str = dt.strftime('%Y-%m-%d')
        datetime_str = dt.strftime('%Y-%m-%d %H:%M:%S')

        if re.match(r'^\d{4}-\d{2}-\d{2}', text.strip()):
            log("Custom date detected")
            date_str = text.strip().splitlines()[0].strip()
            text = '\n'.join(text.strip().splitlines()[1:]).strip()

        commit_message = 'Add entry by Telegram Bot\n\n'
        appendix = ""

        if text.startswith('/'):
            text = text[1:]
            command = text.split(' ', 1)[0]
            payload = text[len(command):].strip()
            log(f"Command: {command}, Payload: {payload}")
            if command == "tz":
                if payload == 'London':
                    self.timezone = pytz.timezone("Europe/London")
                elif payload == 'Beijing':
                    self.timezone = pytz.timezone("Asia/Shanghai")
                else:
                    try:
                        self.timezone = pytz.timezone(payload)
                    except pytz.UnknownTimeZoneError:
                        reply(f"Unknown timezone: {payload}")
                        return
                reply(f"Timezone set to {self.timezone}")
                reply(f"Current time: {datetime.now(self.timezone).strftime('%Y-%m-%d %H:%M:%S')}")
            else:
                reply(f"Unknown command: {command}")
            return

        elif text.lower().startswith("open"):
            log("/open command detected")
            matches = re.findall(r'.*?\s+([^\s]+)\s+([^\s]+)', text, re.IGNORECASE)
            if not matches or len(matches[0]) < 2:
                reply("Invalid open command format.")
                return
            account = matches[0][0]
            currency = matches[0][1]
            appendix = jinja2.get_template("open.bean.j2").render(
                date=date_str, account=account, currency=currency, datetime=datetime_str
            )

        elif text.lower().startswith("balance"):
            log("/balance command detected")
            matches = re.findall(r'.*?\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)', text, re.IGNORECASE)
            if not matches or len(matches[0]) < 3:
                reply("Invalid balance command format.")
                return
            account = match_account(matches[0][0])
            if not account:
                reply(f"No matching account found for suffix: {matches[0][0]}")
                return
            amount = matches[0][1]
            currency = matches[0][2]
            appendix = jinja2.get_template("balance.bean.j2").render(
                date=date_str, account=account, amount=amount, currency=currency, datetime=datetime_str
            )

        elif text.lower().startswith("pad"):
            log("/pad command detected")
            matches = re.findall(r'.*?\s+([^\s]+)\s+([^\s]+)', text, re.IGNORECASE)
            if not matches or len(matches[0]) < 2:
                reply("Invalid pad command format.")
                return
            account = match_account(matches[0][0])
            if not account:
                reply(f"No matching account found for suffix: {matches[0][0]}")
                return
            pad_account = match_account(matches[0][1])
            if not pad_account:
                reply(f"No matching account found for suffix: {matches[0][1]}")
                return
            appendix = jinja2.get_template("pad.bean.j2").render(
                date=date_str, account=account, pad_account=pad_account, datetime=datetime_str
            )

        else:
            log("Transaction detected")
            lines = text.splitlines()

            if len(lines) < 4:
                reply("Invalid transaction format. Please provide payee, narration and two postings.")
                return

            payee = lines.pop(0).strip()
            narration = lines.pop(0).strip()

            tag = None
            link = None
            while lines and lines[0].strip():
                if lines[0].startswith('#'):
                    tag = lines.pop(0)[1:].strip()
                elif lines[0].startswith('^'):
                    link = lines.pop(0)[1:].strip()
                else:
                    break

            if len(lines) < 2:
                reply("A transaction must have at least two postings.")
                return

            postings = []
            r_posting = r'([^\s]+)\s*(-?\d+\.?\d*)\s*([^\s]+)\s*(.*?)\s*$'
            for posting_line in lines:
                posting_line = posting_line.strip()
                if not posting_line:
                    continue

                posting_str, comment = posting_line.split(';', 1) if ';' in posting_line else (posting_line, "")
                pmatches = re.match(r_posting, posting_str)
                if not pmatches:
                    reply(f"Invalid posting format: {posting_str}")
                    return

                account = match_account(pmatches.group(1))
                if not account:
                    reply(f"No matching account found for suffix: {pmatches.group(1)}")
                    return
                if not account.startswith("Expenses") and not account.startswith("Income"):
                    commit_message += f"{account}\n"

                amount = pmatches.group(2)
                currency = pmatches.group(3)
                rest = pmatches.group(4) or ""

                if not re.match(r'^[A-Z0-9][A-Z0-9\'._-]*$', currency) or not re.search(r'[A-Z]', currency):
                    reply(f"货币符号 '{currency}' 无效：必须全部大写，可包含数字，例如 USD、CNY、3NVD。")
                    return

                postings.append({
                    "account": account,
                    "amount": amount,
                    "currency": currency,
                    "rest": rest,
                    "comment": comment.strip()
                })

            if len(postings) == 2:
                a0, a1 = float(postings[0]["amount"]), float(postings[1]["amount"])
                c0, c1 = postings[0]["currency"], postings[1]["currency"]
                r0, r1 = postings[0]["rest"], postings[1]["rest"]

                if a0 * a1 >= 0:
                    reply("两条 posting 必须一正一负。")
                    return

                if c0 == c1:
                    if a0 + a1 != 0:
                        reply(f"同币种 {c0} 的两条 posting 金额不平衡：{a0} + {a1} != 0")
                        return
                else:
                    has_cost_or_price = any(('@' in r or '{' in r) for r in [r0, r1])
                    if not has_cost_or_price:
                        reply(f"不同币种 ({c0}/{c1}) 的交易需要标记成本 {{}} 或价格 @。")
                        return

            appendix = jinja2.get_template("transaction.bean.j2").render(
                date=date_str, payee=payee, narration=narration,
                postings=postings, tag=tag, link=link, datetime=datetime_str,
            )

        f = self.github_download_file()
        if not f:
            reply("Failed to download from GitHub.")
            return
        if self.github_upload_file(f["content"] + '\n' + appendix + '\n', f["sha"], commit_message.strip()):
            reply("Created entry" + (f":\n```beancount\n{appendix}```" if appendix else ""))
            log("Logged entry:\n" + appendix)
        else:
            reply("Failed to upload to GitHub.")

    def get_updates(self):
        params = {"offset": self.update_id + 1, "timeout": 30}
        try:
            loading_stop_event = threading.Event()
            loading = threading.Thread(target=rotating_loading(loading_stop_event).start)
            loading.start()
            response = requests.get(self.api_base + "/getUpdates", data=params, timeout=params["timeout"] + 1)
            loading_stop_event.set()

            if response.status_code != 200:
                log(f"Error: {response.status_code}")
                return {"result": []}
        except KeyboardInterrupt:
            log("Got KeyboardInterrupt in Bot thread.")
            loading_stop_event.set()
            self.stop.set()
            exit(0)
        except Exception as e:
            log(e)
            log("Timeout or Connection Error")
            return {"result": []}
        return response.json()

    def process_updates(self):
        updates = self.get_updates()
        edited_messages = [x for x in updates["result"] if "edited_message" in x]
        messages = [x for x in updates["result"] if "message" in x]

        if self.debug:
            pprint(updates)

        for message in edited_messages:
            self.update_id = message["update_id"]
            log(message)

        for message in messages:
            self.update_id = message["update_id"]
            chat = message["message"]["chat"]
            chat_id = chat["id"]
            first_name = chat.get("first_name", "")
            last_name = chat.get("last_name", "")
            username = chat.get("username", "")
            text = message["message"].get("text")

            fmt = f"{bcolors.OKBLUE}[{chat_id}]{bcolors.ENDC} {first_name} {last_name} (@{username}):"
            if text:
                hd = threading.Thread(target=self.handle_message, args=(message,))
                hd.start()
                if len(text.splitlines()) > 1:
                    log(f"{fmt} \n{text}")
                else:
                    log(f"{fmt} {text}")
            else:
                obj = {k: v for k, v in message['message'].items() if k not in ['chat', 'date', 'from', 'message_id']}
                log(f"{fmt} {obj}")

    def start(self):
        while not self.stop.is_set():
            self.process_updates()


if __name__ == "__main__":
    debug = len(sys.argv) > 1 and sys.argv[1] == "debug"
    bot = Bot(debug)
    if debug:
        log("Debug mode")
    try:
        bot.start()
    except KeyboardInterrupt:
        log("Exiting...")
