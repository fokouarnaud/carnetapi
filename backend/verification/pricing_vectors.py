"""Check shared arithmetic examples intended for both Django and Flutter."""
import json
from pathlib import Path
import uuid

from sales.services.pricing import LineAmount, PricedBasket, PricingFailure, ReferenceAmount, line_reference_amount, price_basket


def main():
    source = Path(__file__).resolve().parents[2] / "analysis" / "pricing-vectors.json"
    vectors = json.loads(source.read_text(encoding="utf-8"))
    for case in vectors["reference_amounts"]:
        result = line_reference_amount(case["quantity_milli"], case["unit_price_fcfa"], case["unit_kind"])
        assert isinstance(result, ReferenceAmount), result
        assert result.amount_fcfa == case["expected_fcfa"], case
    for case in vectors["baskets"]:
        lines = [LineAmount(uuid.UUID(line["id"]), line["reference_fcfa"], line["own_discount_fcfa"]) for line in case["lines"]]
        result = price_basket(lines, case["global_discount_fcfa"], case["seller_cap_basis_points"])
        if "expected_failure" in case:
            assert isinstance(result, PricingFailure) and result.code == case["expected_failure"], case
        else:
            assert isinstance(result, PricedBasket), result
            assert [line.global_discount_fcfa for line in result.lines] == case["expected_global_discounts_fcfa"], case
            assert [line.net_fcfa for line in result.lines] == case["expected_line_nets_fcfa"], case
            assert result.net_total_fcfa == case["expected_net_total_fcfa"], case
    print(json.dumps({"shared_vector_version": vectors["version"], "reference_cases": len(vectors["reference_amounts"]),
                      "basket_cases": len(vectors["baskets"]), "result": "passed"}))


if __name__ == "__main__":
    main()
