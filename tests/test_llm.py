"""Interactive generation using the production LLM path, without ledger writes."""

import sys
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import Bot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="Read real config, ledger and user.md; send input/context to the configured LLM.")
    if not parser.parse_args().live:
        parser.error("Interactive preview requires --live; use only synthetic input and a test ledger.")
    bot = Bot(state_path=":memory:")
    try:
        accounts = bot.parse_accounts()
        if not accounts:
            raise ValueError("No accounts available.")
        print("Natural-language journal preview. 'accounts' lists accounts; 'quit' exits.")
        while True:
            user_input = input("📝 Your input: ").strip()
            if user_input.lower() in {"quit", "exit", "q"}:
                break
            if not user_input:
                continue
            if user_input.lower() == "accounts":
                print("\n".join(accounts))
                continue
            try:
                now = datetime.now(bot.timezone)
                print(bot.call_openai_compatible(user_input, accounts, now.date().isoformat(),
                                                current_time=now.strftime("%H:%M")))
            except Exception as exc:
                print(f"Generation failed: {exc}")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        bot.close()


if __name__ == "__main__":
    main()
