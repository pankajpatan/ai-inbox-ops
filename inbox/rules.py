"""Load and check routing.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator, model_validator

CATEGORIES = ("new_job", "quote_request", "complaint", "invoice_payment", "supplier", "spam_marketing", "other")
URGENCIES = ("now", "today", "later")


def _as_list(value: str | list[str] | None) -> list[str] | None:
    if value is None:
        return None
    return [value] if isinstance(value, str) else list(value)


class Person(BaseModel):
    name: str
    email: str


class Rule(BaseModel):
    category: list[str] | None = None
    urgency: list[str] | None = None
    assign: str | None = None
    reply_within_hours: float | None = None
    alert: bool = False

    _lists = field_validator("category", "urgency", mode="before")(_as_list)

    def matches(self, category: str, urgency: str) -> bool:
        return ((self.category is None or category in self.category)
                and (self.urgency is None or urgency in self.urgency))


class Reply(BaseModel):
    sender_name: str = "The team"
    draft_for: list[str] = []
    allow_prices: bool = False
    phone: str | None = None


class Rules(BaseModel):
    business: str
    description: str = ""
    timezone: str = "UTC"
    categories: dict[str, str]
    urgency: dict[str, str]
    people: dict[str, Person]
    rules: list[Rule]
    reply: Reply = Reply()
    keywords: dict[str, list[str]] = {}
    urgent_keywords: list[str] = []

    @model_validator(mode="after")
    def check(self) -> "Rules":
        if set(self.categories) != set(CATEGORIES):
            raise ValueError(f"categories must be exactly {CATEGORIES}")
        if set(self.urgency) != set(URGENCIES):
            raise ValueError(f"urgency must be exactly {URGENCIES}")
        for i, rule in enumerate(self.rules):
            for c in rule.category or []:
                if c not in CATEGORIES:
                    raise ValueError(f"rule {i + 1}: unknown category '{c}'")
            for u in rule.urgency or []:
                if u not in URGENCIES:
                    raise ValueError(f"rule {i + 1}: unknown urgency '{u}'")
            if rule.assign is not None and rule.assign not in self.people:
                raise ValueError(f"rule {i + 1}: unknown person '{rule.assign}'")
        # every category/urgency pair must be routed, so nothing falls through silently
        for c in CATEGORIES:
            for u in URGENCIES:
                if not any(r.matches(c, u) for r in self.rules):
                    raise ValueError(f"no rule matches category '{c}' with urgency '{u}'")
        return self

    def route(self, category: str, urgency: str) -> Rule:
        return next(r for r in self.rules if r.matches(category, urgency))


def load_rules(path: str | Path) -> Rules:
    with open(path, encoding="utf-8") as f:
        return Rules.model_validate(yaml.safe_load(f))
