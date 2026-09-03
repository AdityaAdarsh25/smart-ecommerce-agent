"""Manual Razorpay Test Mode connectivity check.

Creates one small Razorpay TEST MODE order and prints it, purely to confirm
that RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET in `.env` actually authenticate.
It is a developer smoke check and is not part of any product code path --
nothing in `backend/` imports it, and it creates no quote, transaction or
payment in MandatePay's own database.

    python razorpay_smoke.py

Previously named `test_razorpay.py`, which matched pytest's default
discovery pattern AND performed the provider call at import time: running
`pytest .` (or an IDE's "run all tests") reached the live Razorpay account
and created a real order. The call now happens only under `__main__`, and
the filename no longer looks like a test.
"""

import os

import razorpay
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")

    if not key_id or not key_secret:
        print(
            "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must both be set in .env. "
            "No provider call was made."
        )
        return

    client = razorpay.Client(auth=(key_id, key_secret))
    order = client.order.create({"amount": 100 * 100, "currency": "INR"})

    print("Order created successfully")
    print(order)


if __name__ == "__main__":
    main()
