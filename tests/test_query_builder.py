from dyaf.rules.query_builder import build_bool_query, build_rule_query


def test_bool_query_nesting():
    tree = {"logic": "AND", "conditions": [
        {"field": "amount", "operator": "gt", "value": 9000},
        {"logic": "OR", "conditions": [
            {"field": "currency", "operator": "eq", "value": "USD"},
            {"field": "merchant_country", "operator": "in", "value": ["IR", "SY"]},
        ]},
        {"logic": "NOT", "conditions": [{"field": "merchant_id", "operator": "exists"}]},
    ]}
    q = build_bool_query(tree)
    must = q["bool"]["must"]
    assert must[0] == {"range": {"amount": {"gt": 9000}}}
    assert must[1]["bool"]["minimum_should_match"] == 1
    assert must[1]["bool"]["should"][1] == {"terms": {"merchant_country": ["IR", "SY"]}}
    assert must[2]["bool"]["must_not"] == [{"exists": {"field": "merchant_id"}}]


def test_full_rule_query_contains_window_and_aggs():
    defn = {
        "conditions": {"field": "transaction_type_en", "operator": "eq", "value": "Cash Out"},
        "time_window": {"value": 24, "unit": "hours"},
        "group_by": "sender_wallet_id",
        "aggregation": {"type": "sum", "field": "amount"},
    }
    q = build_rule_query(defn, timestamp_field="executed_at")
    assert q["size"] == 0
    musts = q["query"]["bool"]["must"]
    assert {"range": {"executed_at": {"gte": "now-24h", "lte": "now"}}} in musts
    assert q["aggs"]["by_entity"]["terms"]["field"] == "sender_wallet_id"
    assert q["aggs"]["by_entity"]["aggs"]["metric"] == {"sum": {"field": "amount"}}


def test_percentage_query_uses_bucket_script():
    defn = {
        "time_window": {"value": 7, "unit": "days"},
        "group_by": "sender_wallet_id",
        "aggregation": {"type": "percentage",
                        "config": {"numerator_condition":
                                   {"field": "merchant_category", "operator": "eq", "value": "Gambling"}}},
    }
    q = build_rule_query(defn)
    aggs = q["aggs"]["by_entity"]["aggs"]
    assert "numerator" in aggs and "metric" in aggs
    assert "bucket_script" in aggs["metric"]


def test_count_aggregation_has_no_metric_agg():
    defn = {"time_window": {"value": 1, "unit": "hours"}, "group_by": "sender_wallet_id",
            "aggregation": {"type": "count"}}
    q = build_rule_query(defn)
    assert "aggs" not in q["aggs"]["by_entity"]
