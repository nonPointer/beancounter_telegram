import requests
import time
import threading
import time
import os
import sys
import re
import base64
import pytz
from datetime import datetime
from jinja2 import Environment, FileSystemLoader

jinja2 = Environment(loader=FileSystemLoader(searchpath="./templates"))
from pprint import pprint


with open("config.json", "r") as f:
    import json
    config = json.load(f)

GITHUB_URL_BASE = "https://api.github.com"
BOT_API_BASE_URL = "https://api.telegram.org/bot{}".format(
    config["TELEGRAM_BOT_TOKEN"])
GITHUB_TOKEN = config["GITHUB_TOKEN"]
REPO_OWNER = config["REPO_OWNER"]
REPO_NAME = config["REPO_NAME"]
BRANCH_NAME = config["BRANCH_NAME"]
FILE_PATH = config["FILE_PATH"]
CHAT_ID = config.get("CHAT_ID", None)

TIMEZONE = pytz.timezone(config["TIMEZONE"])

def parse_accounts():
    files = list(filter(lambda x: x.endswith(".bean"), os.listdir("./accounts")))
    accounts = []
    for file in files:
        with open(f"./accounts/{file}", "r") as f:
            content = f.read()
            r_account = r'\d{4}-\d{2}-\d{2} open (.*)'
            matches = re.findall(r_account, content)
            accounts += [x.strip().split(" ", 1)[0] for x in matches]

    return sorted(accounts)

def match_account(account_suffix: str) -> list:
    accounts = parse_accounts()
    suffix_lower = account_suffix.lower()
    matches = [a for a in accounts if a.lower().endswith(suffix_lower)]

    if not matches:
        bot.log(f"No matching account for suffix: {account_suffix}")
        bot.log(f"Available accounts: {accounts}")
    
    return matches[0] if matches else account_suffix

class bcolors():
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


class rotating_loading():
    def __init__(self, stop_event: threading.Event):
        self.stop_event = stop_event

    def start(self):
        symbols = ['/', '-', '\\', '|']
        duration = 0.2
        while not self.stop_event.is_set():
            for symbol in symbols:
                print('\r' + symbol, end='', flush=True)
                time.sleep(duration)

        print("\r", end='')


def handle_edited_message(message):
    pass


def github_download_file() -> dict | None:
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.object",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    url = f"{GITHUB_URL_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{FILE_PATH}?ref={BRANCH_NAME}"
    r = requests.get(url=url,headers=headers)
    
    if r.status_code == 200:
        results = {
            "content": base64.b64decode(r.json()["content"]).decode("utf-8"),
            "sha": r.json()["sha"]
        }
        return results
    elif r.status_code == 404:
        bot.log("File not found.")
        return {"content": "", "sha": ""}
    else:
        bot.log("Error:", r.status_code)
        return None

def github_upload_file(content: str, sha: str, commit_message: str) -> bool:
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.object",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    url = f"{GITHUB_URL_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{FILE_PATH}"
    data = {
        "message": commit_message,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "branch": BRANCH_NAME,
        "sha": sha
    }
    r = requests.put(url=url, headers=headers, json=data)
    if r.status_code in [200, 201]:
        return True
    else:
        bot.log(f"Error uploading file: {r.status_code}")
        bot.log(r.text)
        return False
        
def handle_message(message):
    text = message["message"]["text"]
    chat_id = message["message"]["chat"]["id"]
    
    def reply(text: str):
        bot.send_message(chat_id, text)
        
    if CHAT_ID and str(chat_id) != CHAT_ID:
        bot.log(f"Ignoring message from chat_id {chat_id}, only responding to {CHAT_ID}.")
        reply("How dare you?")
        return
    
    
    # date and datetime in custom timezone
    global TIMEZONE
    dt = datetime.now(TIMEZONE)
    date_str = dt.strftime('%Y-%m-%d')
    datetime_str = dt.strftime('%Y-%m-%d %H:%M:%S')
    
    appendix = ""
    if text.startswith('/'):
        # command
        text = text[1:]
        command = text.split(' ', 1)[0]
        payload = text[len(command):].strip()
        bot.log(f"Command: {command}, Payload: {payload}")
        if command == "tz":
            TIMEZONE = payload
            if TIMEZONE == 'London':
                TIMEZONE = pytz.timezone("Europe/London")
                reply(f"Timezone set to {TIMEZONE}")
            elif TIMEZONE == 'Beijing':
                TIMEZONE = pytz.timezone("Asia/Shanghai")
                reply(f"Timezone set to {TIMEZONE}")
            else:
                try:
                    TIMEZONE = pytz.timezone(payload)
                    reply(f"Timezone set to {TIMEZONE}")
                except pytz.UnknownTimeZoneError:
                    reply(f"Unknown timezone: {payload}")
                    return
            reply(f"Current time: {datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            reply(f"Unknown command: {command}")
        return
    elif text.lower().startswith("open"):
        bot.log("/open command detected")
        r = r'.*?\s+([^\s]+)\s+([^\s]+)'
        matches = re.findall(r, text, re.IGNORECASE)
        if not matches or len(matches[0]) < 2:
            reply("Invalid open command format.")
            return
        account = matches[0][0]
        currency = matches[0][1]
        
        appendix = jinja2.get_template("open.bean.j2").render(
            date=date_str,
            account=account,
            currency=currency,
            datetime=datetime_str
        )
    elif text.lower().startswith("balance"):
        bot.log("/balance command detected")
        # directive, account, amount, currency
        r = r'.*?\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)'
        matches = re.findall(r, text, re.IGNORECASE)
        if not matches or len(matches[0]) < 2:
            reply("Invalid balance command format.")
            return
        account = match_account(matches[0][0])
        amount = matches[0][1]
        currency = matches[0][2]

        appendix = jinja2.get_template("balance.bean.j2").render(
            date=date_str,
            account=account,
            amount=amount,
            currency=currency,
            datetime=datetime_str
        )
    else:
        # transaction
        bot.log("Transaction detected")
        text = text.splitlines()
        # if len(text) < 3:
        #     reply("Invalid transaction format.")
        #     return        
        # header = text.pop()
        # r = r'"(.*?)"\s+"(.*?)"(?:\s+#([^\s]+))?(?:\s+\^([^\s]+))?'
        # matches = re.findall(r, header)
        # if not matches or len(matches[0]) < 2:
        #     reply("Invalid transaction header format.")
        #     return
        # payee = matches[0][0]
        # narration = matches[0][1]
        # tag = matches[0][2] if len(matches[0]) > 2 else ""
        # link = matches[0][3] if len(matches[0]) > 3 else ""
        
        if len(text) < 4:
            reply("Invalid transaction format. Please provide payee, narration and two postings.")
            return
        payee = text.pop(0).strip()
        narration = text.pop(0).strip()
        
        # extract optional tag and link
        tag = None
        link = None
        while text and text[0].strip():
            if text[0].startswith('#'):
                tag = text.pop(0)[1:].strip()
            elif text[0].startswith('^'):
                link = text.pop(0)[1:].strip()
            else:
                break
        
        postings = []
        # if there are less than 2 postings, invalid
        if len(text) < 2:
            reply("A transaction must have at least two postings.")
            return
        
        # units and currency can be optional in postings
        r_posting = r'([^\s]+)\s*([^\s]+)?\s*([^\s]+)?'
        for posting in text:
            pmatches = re.findall(r_posting, posting)
            if not pmatches or len(pmatches[0]) < 1:
                reply(f"Invalid posting format: {posting}")
                return
            account = match_account(pmatches[0][0])
            amount = pmatches[0][1] if len(pmatches[0]) > 1 else ""
            currency = pmatches[0][2] if len(pmatches[0]) > 2 else ""
            p = {
                "account": account,
                "amount": amount,
                "currency": currency
            }
            postings.append(p)
            # posting_list.append(f"{account}  {amount} {currency}".strip())
            
        appendix = jinja2.get_template("transaction.bean.j2").render(
            date=date_str,
            payee=payee,
            narration=narration,
            postings=postings,
            tag=tag,
            link=link,
            datetime=datetime_str,
        )
    
     
    # upload to github
    f = github_download_file()
    if f:
        if github_upload_file(f["content"] + '\n' + appendix + '\n', f["sha"], "Update via bot"):
            reply("Created entry" + (f":\n```{appendix}```" if appendix else ""))
            bot.log("Logged entry:\n" + appendix)


class Bot():
    def log(self, message):
        print(
            f"{bcolors.OKGREEN}[{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}]{bcolors.ENDC}", end="")
        if isinstance(message, str):
            print(" " + message)
        else:
            print()
            pprint(message)

    def __init__(self, debug: bool = False):
        self.update_id = 0
        self.debug = debug
        self.stop = threading.Event()

    def get_updates(self):
        params = {
            "offset": self.update_id + 1,
            "timeout": 30
        }
        try:
            loading_stop_event = threading.Event()
            loading = threading.Thread(target=rotating_loading(loading_stop_event).start)
            loading.start()
            response = requests.get(BOT_API_BASE_URL + "/getUpdates", data=params, timeout=params["timeout"] + 1)
            loading_stop_event.set()

            if response.status_code != 200:
                self.log(f"Error: {response.status_code}")
                return {"result": []}
        except KeyboardInterrupt:
            self.log("Got KeyboardInterrupt in Bot thread.")
            loading_stop_event.set()
            self.stop.set()
            exit(0)
        except Exception as e:
            self.log(e)
            self.log("Timeout or Connection Error")
            return {"result": []}
        return response.json()

    def send_message(self, chat_id, text):
        data = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }
        response = requests.post(BOT_API_BASE_URL + "/sendMessage", data=data)
        if response.status_code != 200:
            self.log(f"Error sending message: {response.status_code}")
            self.log(response.text)
        return response.json()

    def main(self, ):
        updates = self.get_updates()
        edited_message = list(
            filter(lambda x: "edited_message" in x, updates["result"]))
        messages = list(filter(lambda x: "message" in x, updates["result"]))

        if self.debug:
            pprint(updates)

        for message in edited_message:
            self.update_id = message["update_id"]
            self.log(message)

        for message in messages:
            # self.log(message)

            self.update_id = message["update_id"]

            chat_id = message["message"]["chat"]["id"]
            first_name = message["message"]["chat"]["first_name"] if "first_name" in message["message"]["chat"] else ""
            last_name = message["message"]["chat"]["last_name"] if "last_name" in message["message"]["chat"] else ""
            username = message["message"]["chat"]["username"] if "username" in message["message"]["chat"] else ""

            text = message["message"]["text"] if "text" in message["message"] else None

            fmt = f"{bcolors.OKBLUE}[{chat_id}]{bcolors.ENDC} {first_name} {last_name} (@{username}):"
            if text:
                hd = threading.Thread(target=handle_message, args=(message,))
                hd.start()
                if len(text.splitlines()) > 1:
                    self.log(f"{fmt} \n{text}")
                else:
                    self.log(f"{fmt} {text}")
            else:
                obj = {k: v for k, v in message['message'].items() if k not in [
                    'chat', 'date', 'from', 'message_id']}
                self.log(f"{fmt} {obj}")

    def start(self):
        while not self.stop.is_set():
            self.main()
            

if __name__ == "__main__":
    bot = Bot()

    debug = False
    if len(sys.argv) > 1 and sys.argv[1] == "debug":
        bot.log("Debug mode")
        debug = True

    try:
        th = Bot(debug)
        th.start()
    except KeyboardInterrupt:
        bot.log("Exiting...")
