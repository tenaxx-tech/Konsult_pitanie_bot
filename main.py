import os
from nutrition.channels import main

if __name__ == "__main__":
    if not os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("API_TOKEN"):
        os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["API_TOKEN"]
    main()
