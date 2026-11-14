"""Check that your CoinDCX API key works. Prints only masked account details."""

import config as cfg
from coindcx_client import CoinDCXClient, CoinDCXError


def mask(value: str, keep: int = 3) -> str:
    value = str(value or "")
    return value[:keep] + "*" * max(len(value) - keep, 0)


def main() -> None:
    if not cfg.API_KEY or not cfg.API_SECRET:
        print("Missing COINDCX_API_KEY / COINDCX_API_SECRET")
        return
    client = CoinDCXClient(cfg.API_KEY, cfg.API_SECRET, cfg.BASE_URL, cfg.PUBLIC_BASE_URL)
    try:
        info = client.user_info()
        bals = client.balances()
    except CoinDCXError as e:
        print(f"Auth FAILED: {e}\nCheck key/secret, IP binding and your system clock.")
        return
    print("Auth OK")
    print(f"  user: {mask(info.get('first_name', ''))} | email: {mask(info.get('email', ''))}")
    inr = bals.get("INR", {})
    print(f"  INR free ₹{inr.get('balance', 0):,.2f} | locked ₹{inr.get('locked', 0):,.2f}")
    print(f"  coins with a balance: {sum(1 for c, b in bals.items() if c != 'INR' and b['total'] > 0)}")


if __name__ == "__main__":
    main()
