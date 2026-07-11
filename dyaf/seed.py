"""Demo data generator.

Seeds the platform tables (wallets, transaction types, internal wallets)
and generates a realistic transaction history that includes deliberately
suspicious patterns so detection rules have something to catch:

* W-1001 (Ahmad): structuring — many transfers just below 10,000 in 24h.
* W-1002 (Layla): a single very large transfer.
* W-1003 (Omar, PEP, high risk): frequent transfers to a high-risk country.
* W-1004 (Sara): high share of card spend at gambling merchants.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from .core.database import Database
from .core.models import (InternalWalletConfig, KycStatus, RiskRating,
                          Transaction, TransactionType, Wallet, WalletType)

HIGH_RISK_COUNTRIES = ["IR", "KP", "SY", "MM"]

TRANSACTION_TYPES = [
    TransactionType(1, "P2P Transfer", "تحويل بين محفظتين"),
    TransactionType(2, "Cash In", "إيداع نقدي"),
    TransactionType(3, "Cash Out", "سحب نقدي"),
    TransactionType(4, "Card Payment", "دفع بالبطاقة"),
    TransactionType(5, "International Remittance", "حوالة دولية"),
    TransactionType(6, "Bill Payment", "دفع فواتير"),
]

WALLETS = [
    Wallet("W-1001", "Ahmad Khalil", "JO", "JO", date(1988, 4, 12), RiskRating.MEDIUM.value, KycStatus.VERIFIED.value, False, WalletType.CUSTOMER.value),
    Wallet("W-1002", "Layla Hassan", "JO", "JO", date(1992, 9, 3), RiskRating.LOW.value, KycStatus.VERIFIED.value, False, WalletType.CUSTOMER.value),
    Wallet("W-1003", "Omar Nasser", "SY", "SY", date(1979, 1, 25), RiskRating.HIGH.value, KycStatus.PENDING.value, True, WalletType.CUSTOMER.value),
    Wallet("W-1004", "Sara Aziz", "JO", "AE", date(1995, 6, 30), RiskRating.LOW.value, KycStatus.VERIFIED.value, False, WalletType.CUSTOMER.value),
    Wallet("W-1005", "Khaled Odeh", "JO", "JO", date(1985, 11, 8), RiskRating.LOW.value, KycStatus.VERIFIED.value, False, WalletType.CUSTOMER.value),
    Wallet("W-2001", "Agent - Downtown Branch", "JO", "JO", None, RiskRating.LOW.value, KycStatus.VERIFIED.value, False, WalletType.AGENT.value),
    Wallet("W-2002", "Agent - Airport Kiosk", "JO", "JO", None, RiskRating.LOW.value, KycStatus.VERIFIED.value, False, WalletType.AGENT.value),
    Wallet("W-9001", "Card Settlement Wallet", None, None, None, RiskRating.LOW.value, KycStatus.VERIFIED.value, False, WalletType.INTERNAL.value),
    Wallet("W-9002", "Remittance Settlement Wallet", None, None, None, RiskRating.LOW.value, KycStatus.VERIFIED.value, False, WalletType.INTERNAL.value),
]

INTERNAL_WALLETS = [
    InternalWalletConfig("W-9001", "Card Settlement", "Settlement wallet for external card scheme transactions"),
    InternalWalletConfig("W-9002", "Remittance Settlement", "Settlement wallet for international remittance partners"),
]


def seed(db: Database, now: datetime | None = None, rng_seed: int = 42) -> dict:
    """Populate the database; returns counts for reporting."""
    rng = random.Random(rng_seed)
    now = now or datetime.now(timezone.utc)
    seq = 0

    for w in WALLETS:
        db.upsert_wallet(w)
    for t in TRANSACTION_TYPES:
        db.upsert_transaction_type(t)
    for iw in INTERNAL_WALLETS:
        db.upsert_internal_wallet(iw)

    def tx(sender, receiver, amount, hours_ago, type_id, **kw):
        nonlocal seq
        seq += 1
        return Transaction(
            transaction_id=f"TX-{seq:06d}",
            sender_wallet_id=sender, receiver_wallet_id=receiver,
            amount=round(amount, 2),
            executed_at=now - timedelta(hours=hours_ago),
            transaction_type_id=type_id,
            reference_number=f"REF-{seq:08d}",
            fee=round(amount * 0.005, 2),
            currency=kw.pop("currency", "USD"),
            **kw,
        )

    txs: list[Transaction] = []

    # --- Normal background traffic over the last 7 days -----------------
    customers = ["W-1001", "W-1002", "W-1004", "W-1005"]
    for _ in range(120):
        sender = rng.choice(customers)
        receiver = rng.choice([w for w in customers + ["W-2001", "W-2002"] if w != sender])
        txs.append(tx(sender, receiver, rng.uniform(10, 800),
                      rng.uniform(1, 168), rng.choice([1, 2, 3, 6])))

    # --- Pattern 1: structuring by W-1001 (12 x ~9,500 in the last 24h) --
    for i in range(12):
        txs.append(tx("W-1001", "W-2001", rng.uniform(9000, 9900), rng.uniform(0.5, 23), 3))

    # --- Pattern 2: single very large transfer by W-1002 -----------------
    txs.append(tx("W-1002", "W-1005", 75000, 5, 1))

    # --- Pattern 3: W-1003 remittances to a high-risk country ------------
    for i in range(8):
        txs.append(tx("W-1003", "W-9002", rng.uniform(1500, 4000), rng.uniform(1, 40), 5,
                      merchant_country=rng.choice(HIGH_RISK_COUNTRIES)))
    # a couple of benign remittances too
    for i in range(2):
        txs.append(tx("W-1003", "W-9002", rng.uniform(100, 300), rng.uniform(1, 40), 5,
                      merchant_country="JO"))

    # --- Pattern 4: W-1004 card spend dominated by gambling merchants ----
    for i in range(9):
        txs.append(tx("W-1004", "W-9001", rng.uniform(200, 900), rng.uniform(1, 30), 4,
                      merchant_id=f"M-{7000 + i}", merchant_name=f"Lucky Star Casino {i}",
                      merchant_category="Gambling", merchant_country="MT"))
    for i in range(3):
        txs.append(tx("W-1004", "W-9001", rng.uniform(20, 90), rng.uniform(1, 30), 4,
                      merchant_id=f"M-{8000 + i}", merchant_name="City Supermarket",
                      merchant_category="Grocery", merchant_country="AE"))

    for t in txs:
        db.insert_transaction(t)

    return {
        "wallets": len(WALLETS),
        "transaction_types": len(TRANSACTION_TYPES),
        "internal_wallets": len(INTERNAL_WALLETS),
        "transactions": len(txs),
    }
