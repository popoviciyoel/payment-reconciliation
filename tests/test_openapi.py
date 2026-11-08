"""The OpenAPI docs: webhook bodies are documented, and every example matches its model."""

import pytest

from app.api.webhooks import WebhookAck
from app.domain.models import PaymentCreated, TransactionSettled
from app.domain.schemas import LinkedTransaction, Payment, PaymentList, UnmatchedTransaction
from app.main import app


def _assert_known_keys(model, data):
    """Models ignore unknown keys, so check them explicitly (including nested lists)."""
    unknown = set(data) - set(model.model_fields)
    assert not unknown, f"{model.__name__} example has unknown fields {unknown}"


@pytest.mark.parametrize("model", [PaymentCreated, TransactionSettled, WebhookAck, Payment, PaymentList])
def test_examples_match_their_models(model):
    for example in model.model_config["json_schema_extra"]["examples"]:
        model.model_validate(example)
        _assert_known_keys(model, example)
        for p in example.get("payments", [example] if model is Payment else []):
            _assert_known_keys(Payment, p)
            for t in p["transactions"]:
                _assert_known_keys(LinkedTransaction, t)
        for t in example.get("unmatched_transactions", []):
            _assert_known_keys(UnmatchedTransaction, t)


@pytest.mark.parametrize("path, model", [
    ("/webhooks/payments", PaymentCreated),
    ("/webhooks/transactions", TransactionSettled),
])
def test_webhook_docs_show_body_signature_header_and_errors(path, model):
    op = app.openapi()["paths"][path]["post"]
    body = op["requestBody"]["content"]["application/json"]["schema"]
    assert body["examples"] == model.model_config["json_schema_extra"]["examples"]
    assert [p["name"] for p in op["parameters"]] == ["X-Webhook-Signature"]
    assert {"200", "400", "401", "422", "429", "500"} <= set(op["responses"])
