from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_cache import CachedAgentWorkflow, HarnessCache, Pointer, WorkflowComparison, compare_reports, estimate_tokens


def _filler(topic: str, count: int = 80) -> list[str]:
    return [
        f"# {topic} background note {index}: deterministic detail for broad source context."
        for index in range(count)
    ]


DEMO_PROJECT: dict[str, str] = {
    "orders.py": "\n".join(
        [
            "def reserve_inventory(order):",
            "    sku = order['sku']",
            "    quantity = order['quantity']",
            "    return inventory.reserve(sku, quantity, retry='short')",
            "",
            "def release_inventory(order):",
            "    return inventory.release(order['sku'])",
            "",
            *_filler("orders"),
        ]
    ),
    "payments.py": "\n".join(
        [
            "def capture_payment(invoice):",
            "    gateway = gateway_for(invoice['currency'])",
            "    return gateway.capture(invoice['amount'], idempotency_key=invoice['id'])",
            "",
            "def refund_payment(invoice):",
            "    return gateway_for(invoice['currency']).refund(invoice['id'])",
            "",
            *_filler("payments"),
        ]
    ),
    "notifications.py": "\n".join(
        [
            "def send_receipt(customer, receipt):",
            "    channel = customer.preferred_channel",
            "    return delivery.send(channel, receipt, template='purchase_receipt')",
            "",
            "def send_refund_notice(customer, refund):",
            "    return delivery.send(customer.preferred_channel, refund)",
            "",
            *_filler("notifications"),
        ]
    ),
}


TASKS = (
    {
        "agent_id": "agent_orders",
        "task": "find the inventory reservation retry behavior",
        "required_tags": ["orders"],
        "pointer_id": "ptr_orders_reserve",
        "path": "orders.py",
    },
    {
        "agent_id": "agent_payments",
        "task": "find payment capture idempotency behavior",
        "required_tags": ["payments"],
        "pointer_id": "ptr_payments_capture",
        "path": "payments.py",
    },
    {
        "agent_id": "agent_notifications",
        "task": "find receipt delivery template behavior",
        "required_tags": ["notifications"],
        "pointer_id": "ptr_notifications_receipt",
        "path": "notifications.py",
    },
    {
        "agent_id": "agent_orders_2",
        "task": "check order inventory reservation again",
        "required_tags": ["orders"],
        "pointer_id": "ptr_orders_reserve",
        "path": "orders.py",
    },
)


@dataclass(frozen=True)
class DemoResult:
    no_cache_tokens: int
    cache_tokens: int
    comparison: WorkflowComparison


def build_cache() -> HarnessCache:
    cache = HarnessCache()
    for path, text in DEMO_PROJECT.items():
        cache.add_source("random_shop", path, text, "demo_commit_001")

    cache.add_pointer(
        Pointer(
            pointer_id="ptr_orders_reserve",
            source_id="random_shop",
            source_type="code",
            path="orders.py",
            title="inventory reservation retry behavior",
            line_start=1,
            line_end=4,
            anchors=("reserve_inventory", "retry='short'"),
            tags=("orders", "inventory", "retry"),
            source_version="demo_commit_001",
            trust_score=0.8,
        ),
        level="l2",
    )
    cache.add_pointer(
        Pointer(
            pointer_id="ptr_payments_capture",
            source_id="random_shop",
            source_type="code",
            path="payments.py",
            title="payment capture idempotency behavior",
            line_start=1,
            line_end=3,
            anchors=("capture_payment", "idempotency_key"),
            tags=("payments", "capture", "idempotency"),
            source_version="demo_commit_001",
            trust_score=0.8,
        ),
        level="l2",
    )
    cache.add_pointer(
        Pointer(
            pointer_id="ptr_notifications_receipt",
            source_id="random_shop",
            source_type="code",
            path="notifications.py",
            title="receipt delivery template behavior",
            line_start=1,
            line_end=3,
            anchors=("send_receipt", "purchase_receipt"),
            tags=("notifications", "receipt", "template"),
            source_version="demo_commit_001",
            trust_score=0.8,
        ),
        level="l2",
    )
    return cache


def run_no_cache_baseline() -> int:
    return sum(estimate_tokens(DEMO_PROJECT[task["path"]]) for task in TASKS)


def run_demo() -> DemoResult:
    cache = build_cache()
    workflow = CachedAgentWorkflow(cache)
    reports = []
    for task in TASKS:
        session = workflow.start_task(task["agent_id"], task["task"])
        ranges = session.find_evidence(limit=5, required_tags=task["required_tags"])
        if not ranges:
            session.read_full_source("random_shop", task["path"])
            reports.append(session.complete(success=False))
            continue
        pointer_id = ranges[0].pointer.pointer_id
        session.use(pointer_id)
        session.cite(pointer_id, correct=True)
        session.test_passed(pointer_id)
        reports.append(session.complete(success=True, supported_key_claims=1, total_key_claims=1))

    comparison = compare_reports(reports)
    return DemoResult(
        no_cache_tokens=run_no_cache_baseline(),
        cache_tokens=comparison.tokens_with_cache,
        comparison=comparison,
    )


def main() -> None:
    result = run_demo()
    print("Harness Cache token savings demo")
    print(f"No cache estimated tokens: {result.no_cache_tokens}")
    print(f"Harness Cache estimated tokens: {result.cache_tokens}")
    print(f"Saved estimated tokens: {result.comparison.saved_tokens}")
    print(f"Token I/O reduction: {result.comparison.token_io_reduction:.1%}")
    print(f"Saves tokens: {result.comparison.saves_tokens}")


if __name__ == "__main__":
    main()
