"""Core domain models for the financial platform.

These mirror the platform's primary tables described in the system overview:
Transactions, Wallets, Transaction Types and Internal Wallet definitions.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional


class WalletType(str, enum.Enum):
    CUSTOMER = "Customer Wallet"
    AGENT = "Agent Wallet"
    INTERNAL = "Internal Wallet"


class KycStatus(str, enum.Enum):
    VERIFIED = "Verified"
    PENDING = "Pending"
    REJECTED = "Rejected"
    NOT_STARTED = "Not Started"


class RiskRating(str, enum.Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


@dataclass
class Wallet:
    wallet_id: str
    owner_name: str
    nationality: Optional[str] = None
    residence_country: Optional[str] = None
    date_of_birth: Optional[date] = None
    risk_rating: str = RiskRating.LOW.value
    kyc_status: str = KycStatus.NOT_STARTED.value
    pep_status: bool = False
    wallet_type: str = WalletType.CUSTOMER.value

    def to_dict(self) -> dict:
        d = asdict(self)
        if isinstance(d.get("date_of_birth"), date):
            d["date_of_birth"] = d["date_of_birth"].isoformat()
        return d


@dataclass
class TransactionType:
    type_id: int
    name_en: str
    name_ar: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Transaction:
    transaction_id: str
    sender_wallet_id: str
    receiver_wallet_id: str
    amount: float
    executed_at: datetime
    transaction_type_id: int
    reference_number: str
    fee: float = 0.0
    currency: str = "USD"
    # Merchant-related optional attributes
    merchant_id: Optional[str] = None
    merchant_name: Optional[str] = None
    merchant_category: Optional[str] = None
    merchant_country: Optional[str] = None
    # Free-form extra attributes (kept extensible, no hardcoding)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("extra", None)
        if isinstance(self.executed_at, datetime):
            d["executed_at"] = self.executed_at.isoformat()
        d.update(self.extra or {})
        return d


@dataclass
class InternalWalletConfig:
    """Admin-managed classification of an internal (settlement) wallet.

    Managed from the settings screen: links a wallet id to a purpose
    name/description so its role in transaction flows is identifiable.
    """
    wallet_id: str
    name: str
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
