"""Investor profile: a short risk questionnaire plus basic financial facts.

The score measures *willingness* to take risk. *Capacity* (time horizon,
emergency fund, debt) is enforced separately by the planner as hard limits,
so a very keen investor who needs the money next year still gets a
cautious plan.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Callable

import psycopg


@dataclass(frozen=True)
class Question:
    key: str
    text: str
    options: tuple[tuple[str, int], ...]   # (label, points 0..3)


QUESTIONS: tuple[Question, ...] = (
    Question("horizon", "When will you need most of this money?", (
        ("Within 1 year", 0), ("In 1-3 years", 1), ("In 3-5 years", 2),
        ("In 5-10 years", 3), ("In more than 10 years", 3))),
    Question("drop_reaction", "Your shares fall 20% in three months. What do you do?", (
        ("Sell everything to stop the loss", 0), ("Sell some", 1),
        ("Hold and wait", 2), ("Buy more while prices are low", 3))),
    Question("goal", "What matters most for this money?", (
        ("Not losing any of it", 0), ("Regular income (dividends/interest)", 1),
        ("A mix of income and growth", 2), ("Maximum long-term growth", 3))),
    Question("income_stability", "How stable is your income?", (
        ("Irregular or uncertain", 0), ("Somewhat stable", 1),
        ("Stable (e.g. permanent job)", 2), ("Very stable with several sources", 3))),
    Question("experience", "What have you invested in before?", (
        ("Nothing yet", 0), ("SACCO, MMF or fixed deposits", 1),
        ("A few shares or bonds", 2), ("Shares for several years through ups and downs", 3))),
    Question("share_of_savings", "What share of your total savings is this money?", (
        ("More than 75%", 0), ("50-75%", 1), ("25-50%", 2), ("Less than 25%", 3))),
    Question("outcome_range", "Which one-year range of results would you pick?", (
        ("+4% to +8% (no loss)", 0), ("-5% to +15%", 1),
        ("-15% to +30%", 2), ("-30% to +50%", 3))),
)

# answer index of the horizon question -> planning horizon in years
HORIZON_YEARS = (1, 2, 4, 7, 15)


def score_answers(answers: dict[str, int]) -> int:
    """answers maps question key -> chosen option index. Returns 0-100."""
    total = maximum = 0
    for q in QUESTIONS:
        if q.key not in answers:
            raise ValueError(f"Missing answer for {q.key!r}")
        idx = answers[q.key]
        if not 0 <= idx < len(q.options):
            raise ValueError(f"{q.key}: option {idx} out of range 0-{len(q.options) - 1}")
        total += q.options[idx][1]
        maximum += max(p for _, p in q.options)
    return round(100 * total / maximum)


def tolerance_for(score: int) -> str:
    if score < 35:
        return "conservative"
    if score < 65:
        return "moderate"
    return "aggressive"


@dataclass
class Profile:
    display_name: str
    monthly_income: Decimal | None
    monthly_expenses: Decimal | None
    monthly_investable: Decimal
    high_interest_debt: Decimal = Decimal(0)
    emergency_months_target: int = 6
    answers: dict[str, int] = field(default_factory=dict)
    goals: str | None = None
    max_stock_pct: Decimal = Decimal(20)
    max_sector_pct: Decimal = Decimal(40)
    id: int | None = None

    @property
    def risk_score(self) -> int:
        return score_answers(self.answers)

    @property
    def risk_tolerance(self) -> str:
        return tolerance_for(self.risk_score)

    @property
    def horizon_years(self) -> int:
        return HORIZON_YEARS[self.answers["horizon"]]


# ---------------------------------------------------------------- persistence


def save_profile(conn: psycopg.Connection, p: Profile) -> int:
    values = dict(
        display_name=p.display_name, risk_tolerance=p.risk_tolerance, risk_score=p.risk_score,
        horizon_years=p.horizon_years, monthly_income=p.monthly_income,
        monthly_expenses=p.monthly_expenses, monthly_investable=p.monthly_investable,
        high_interest_debt=p.high_interest_debt, emergency_months_target=p.emergency_months_target,
        questionnaire=json.dumps(p.answers), goals=p.goals,
        max_stock_pct=p.max_stock_pct, max_sector_pct=p.max_sector_pct,
    )
    cols = list(values)
    if p.id is None:
        row = conn.execute(
            f"INSERT INTO investor_profiles({', '.join(cols)}) "
            f"VALUES ({', '.join(f'%({c})s' for c in cols)}) RETURNING id", values).fetchone()
        p.id = row["id"]
    else:
        conn.execute(
            f"UPDATE investor_profiles SET {', '.join(f'{c} = %({c})s' for c in cols)}, "
            f"updated_at = now() WHERE id = %(id)s", {**values, "id": p.id})
    return p.id


def load_profile(conn: psycopg.Connection, profile_id: int) -> Profile:
    r = conn.execute("SELECT * FROM investor_profiles WHERE id = %s", (profile_id,)).fetchone()
    if r is None:
        raise LookupError(f"No profile with id {profile_id}")
    answers = r["questionnaire"] or {}
    if isinstance(answers, str):
        answers = json.loads(answers)
    return Profile(
        id=r["id"], display_name=r["display_name"], monthly_income=r["monthly_income"],
        monthly_expenses=r["monthly_expenses"], monthly_investable=r["monthly_investable"] or Decimal(0),
        high_interest_debt=r["high_interest_debt"], emergency_months_target=r["emergency_months_target"],
        answers=answers, goals=r["goals"], max_stock_pct=r["max_stock_pct"],
        max_sector_pct=r["max_sector_pct"],
    )


def resolve_profile_id(conn: psycopg.Connection, requested: int | None) -> int:
    """Explicit --profile wins; otherwise the only (or first) profile."""
    if requested is not None:
        return requested
    row = conn.execute("SELECT min(id) AS id, count(*) AS n FROM investor_profiles").fetchone()
    if not row["n"]:
        raise LookupError("No investor profile yet — run `nse-agent profile create` first")
    return row["id"]


def set_emergency_fund(conn: psycopg.Connection, profile_id: int, amount: Decimal) -> None:
    conn.execute(
        """INSERT INTO other_holdings(profile_id, asset_class, name, amount)
           VALUES (%s, 'emergency_cash', 'Emergency fund', %s)
           ON CONFLICT (profile_id, asset_class, name)
           DO UPDATE SET amount = EXCLUDED.amount, as_of = CURRENT_DATE""",
        (profile_id, amount))


# ---------------------------------------------------------------- interactive


def _ask_money(ask: Callable[[str], str], prompt: str, *, optional: bool = False) -> Decimal | None:
    while True:
        raw = ask(f"{prompt} (KES){' [blank to skip]' if optional else ''}: ").strip().replace(",", "")
        if not raw and optional:
            return None
        try:
            v = Decimal(raw)
            if v >= 0:
                return v
        except Exception:
            pass
        print("  Please enter a number, e.g. 25000")


def _ask_choice(ask: Callable[[str], str], q: Question) -> int:
    print(f"\n{q.text}")
    for i, (label, _) in enumerate(q.options, start=1):
        print(f"  {i}. {label}")
    while True:
        raw = ask("Choose a number: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(q.options):
            return int(raw) - 1
        print(f"  Enter 1-{len(q.options)}")


def run_questionnaire(ask: Callable[[str], str] = input) -> tuple[Profile, Decimal]:
    """Interactive flow. Returns the profile and the current emergency savings."""
    print("Let's build your investor profile. Figures stay in your own database.\n")
    name = ask("Your name: ").strip() or "Me"
    income = _ask_money(ask, "Monthly take-home income", optional=True)
    expenses = _ask_money(ask, "Monthly living expenses", optional=True)
    investable = _ask_money(ask, "Amount you can invest each month")
    savings = _ask_money(ask, "Emergency savings you already have (cash/MMF you won't invest)")
    debt = _ask_money(ask, "High-interest debt: mobile loans, credit cards, shylocks (0 if none)")
    answers = {q.key: _ask_choice(ask, q) for q in QUESTIONS}
    goals = ask("\nIn a sentence, what is this money for? [optional]: ").strip() or None
    return Profile(display_name=name, monthly_income=income, monthly_expenses=expenses,
                   monthly_investable=investable, high_interest_debt=debt, answers=answers,
                   goals=goals), savings


def profile_from_dict(d: dict[str, Any]) -> tuple[Profile, Decimal]:
    """Non-interactive: build from a JSON file (see templates/profile.json)."""
    def dec(k, default=None):
        v = d.get(k, default)
        return None if v is None else Decimal(str(v))
    # JSON uses the option numbers shown in the questionnaire (1-based)
    answers = {k: int(v) - 1 for k, v in d["answers"].items() if not k.startswith("_")}
    score_answers(answers)  # validate early
    return Profile(
        display_name=d.get("name", "Me"), monthly_income=dec("monthly_income"),
        monthly_expenses=dec("monthly_expenses"), monthly_investable=dec("monthly_investable", 0),
        high_interest_debt=dec("high_interest_debt", 0),
        emergency_months_target=int(d.get("emergency_months_target", 6)),
        answers=answers, goals=d.get("goals"),
        max_stock_pct=dec("max_stock_pct", 20), max_sector_pct=dec("max_sector_pct", 40),
    ), dec("emergency_savings", 0)


def describe(p: Profile) -> dict[str, Any]:
    d = asdict(p)
    d.update(risk_score=p.risk_score, risk_tolerance=p.risk_tolerance, horizon_years=p.horizon_years)
    return d
