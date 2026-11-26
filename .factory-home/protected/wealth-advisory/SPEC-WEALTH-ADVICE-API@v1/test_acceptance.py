"""PROTECTED acceptance tests (QA-owned; do not edit via implementation branches) — from SPEC-WEALTH-ADVICE-API@v1."""

def test_ac_1(call):
    """REQ-1 / AC-1: GIVEN an advisor in tenant wealth with scope advice:read WHEN it requests account acc-900 THEN the account is returned with status 200"""
    status, body = call('GET', '/accounts/acc-900', {'X-Principal': 'adv-1', 'X-Tenant': 'wealth', 'X-Scopes': 'advice:read'}, None)
    assert status == 200, body
    assert body.get('id') == 'acc-900', body
    assert body.get('currency') == 'USD', body

def test_ac_2(call):
    """REQ-2 / AC-2: GIVEN an advisor in tenant wealth with advice:read WHEN it requests retail account acc-100 THEN 404 is returned and no account data is disclosed"""
    status, body = call('GET', '/accounts/acc-100', {'X-Principal': 'adv-1', 'X-Tenant': 'wealth', 'X-Scopes': 'advice:read'}, None)
    assert status == 404, body
    assert 'balance_cents' not in body, body

def test_ac_3(call):
    """REQ-3 / AC-3: GIVEN an advisor in tenant wealth with advice:write and step-up authentication WHEN it records a buy of 250000 cents of CDN-BOND-AGG on acc-900 THEN the recommendation is recorded with status 201"""
    status, body = call('POST', '/accounts/acc-900/recommendations', {'X-Principal': 'adv-1', 'X-Tenant': 'wealth', 'X-Scopes': 'advice:write', 'X-MFA': 'true'}, {'product_code': 'CDN-BOND-AGG', 'side': 'buy', 'amount_cents': 250000, 'rationale': 'Rebalances fixed income back to the IPS target for a medium risk profile'})
    assert status == 201, body
    assert body.get('amount_cents') == 250000, body
    assert body.get('parent') == 'acc-900', body

def test_ac_4(call):
    """REQ-3 / AC-4: GIVEN an advisor with advice:write and step-up authentication WHEN it records a recommendation without a rationale THEN the request is rejected with 400"""
    status, body = call('POST', '/accounts/acc-900/recommendations', {'X-Principal': 'adv-1', 'X-Tenant': 'wealth', 'X-Scopes': 'advice:write', 'X-MFA': 'true'}, {'product_code': 'CDN-BOND-AGG', 'side': 'buy', 'amount_cents': 250000})
    assert status == 400, body

def test_ac_5(call):
    """REQ-3 / AC-5: GIVEN an advisor with advice:write but without step-up authentication WHEN it records a recommendation THEN the request is denied with 403"""
    status, body = call('POST', '/accounts/acc-900/recommendations', {'X-Principal': 'adv-1', 'X-Tenant': 'wealth', 'X-Scopes': 'advice:write'}, {'product_code': 'CDN-BOND-AGG', 'side': 'buy', 'amount_cents': 250000, 'rationale': 'Rebalances fixed income back to the IPS target'})
    assert status == 403, body
