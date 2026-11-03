"""Fictional jobs for the demo: about 9 weeks, the same every time (fixed seed), with planted issues.

Planted, relative to the last full week before `today`:
- revenue down about 40% on the week before (should be flagged)
- two invoices more than 14 days overdue (flagged), one only a few days late (not flagged)
"""
from __future__ import annotations

import datetime as dt
import random

from .report import Job, week_start

CUSTOMERS = ["Maria Lopez", "Tom Becker", "Priya Nair", "Greg Hill", "Riverside Cafe", "Lakeside Dental",
             "Alice Wong", "Ben Ortiz", "Sanjay Rao", "Helen Shaw", "Omar Haddad", "Linda Park", "Ivy Chen",
             "Ruth Adams", "Ken Moore", "Carmen Ruiz", "Paul Grant", "Nina Brooks", "Leo Grant", "Frank Miller"]
TYPES = {"Plumbing repair": (150, 450), "Heating service": (180, 600), "Electrical repair": (120, 400),
         "Handyman": (90, 300), "Water heater install": (1200, 1900)}


def sample_jobs(today: dt.date, weeks: int = 9) -> list[Job]:
    rng = random.Random(7)
    last_full = week_start(today) - dt.timedelta(days=7)
    first = last_full - dt.timedelta(days=7 * (weeks - 2))
    jobs: list[Job] = []
    n = 2300
    for w in range(weeks):
        start = first + dt.timedelta(days=7 * w)
        if start > today:
            break
        quiet = start == last_full
        count = 6 if quiet else rng.randint(10, 14)
        for _ in range(count):
            day = start + dt.timedelta(days=rng.randint(0, 5))
            if day > today:
                continue
            kind = rng.choice([t for t in TYPES if not (quiet and t == "Water heater install")])
            lo, hi = TYPES[kind]
            n += 1
            status = "scheduled" if day >= today else rng.choices(["completed", "cancelled"], [12, 1])[0]
            jobs.append(Job(date=day, customer=rng.choice(CUSTOMERS), job_type=kind, status=status,
                            amount=round(rng.uniform(lo, hi) / 5) * 5 if status != "cancelled" else 0,
                            invoice_number=f"INV-{n}" if status == "completed" else "",
                            invoice_status="paid" if status == "completed" else "",
                            due_date=day + dt.timedelta(days=14) if status == "completed" else None))
    # the week before the quiet one had a big install, so the drop is clear
    big_day = last_full - dt.timedelta(days=5)
    n += 1
    jobs.append(Job(date=big_day, customer="Riverside Cafe", job_type="Water heater install", status="completed",
                    amount=1650, invoice_number=f"INV-{n}", invoice_status="paid",
                    due_date=big_day + dt.timedelta(days=14)))
    # planted unpaid invoices
    plant = [(today - dt.timedelta(days=40), "Lakeside Dental", "Plumbing repair", 640),
             (today - dt.timedelta(days=33), "Frank Miller", "Electrical repair", 260),
             (today - dt.timedelta(days=17), "Nina Brooks", "Heating service", 410)]
    for day, customer, kind, amount in plant:
        n += 1
        jobs.append(Job(date=day, customer=customer, job_type=kind, status="completed", amount=amount,
                        invoice_number=f"INV-{n}", invoice_status="unpaid", due_date=day + dt.timedelta(days=14)))
    return sorted(jobs, key=lambda j: (j.date, j.invoice_number))
