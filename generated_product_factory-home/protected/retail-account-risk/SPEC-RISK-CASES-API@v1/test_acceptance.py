"""PROTECTED acceptance tests (QA-owned; do not edit via implementation branches) — from SPEC-RISK-CASES-API@v1."""

def test_ac_1(call):
    """REQ-1 / AC-1: GIVEN an analyst in tenant retail with scope cases:read WHEN it lists cases THEN the three retail cases are returned with status 200"""
    status, body = call('GET', '/cases', {'X-Principal': 'risk-1', 'X-Tenant': 'retail', 'X-Scopes': 'cases:read'}, None)
    assert status == 200, body
    assert len(body['items']) == 3, body

def test_ac_2(call):
    """REQ-2 / AC-2: GIVEN an analyst in tenant retail with cases:read WHEN it reads CASE-1001 THEN the case is returned with its type and score and without the customer identifier"""
    status, body = call('GET', '/cases/CASE-1001', {'X-Principal': 'risk-1', 'X-Tenant': 'retail', 'X-Scopes': 'cases:read'}, None)
    assert status == 200, body
    assert body.get('case_type') == 'fraud', body
    assert body.get('score') == 100, body
    assert 'customer_id' not in body, body

def test_ac_3(call):
    """REQ-2 / AC-3: GIVEN an analyst in tenant retail with cases:read WHEN it reads wealth case CASE-W-01 THEN 404 is returned and no case data is disclosed"""
    status, body = call('GET', '/cases/CASE-W-01', {'X-Principal': 'risk-1', 'X-Tenant': 'retail', 'X-Scopes': 'cases:read'}, None)
    assert status == 404, body
    assert 'score' not in body, body

def test_ac_4(call):
    """REQ-2 / AC-4: GIVEN a retail user without cases:read WHEN it lists cases THEN the request is denied with 403"""
    status, body = call('GET', '/cases', {'X-Principal': 'risk-2', 'X-Tenant': 'retail', 'X-Scopes': 'accounts:read'}, None)
    assert status == 403, body

def test_ac_5(call):
    """REQ-3 / AC-5: GIVEN an analyst in tenant retail with cases:write WHEN it adds a note to open case CASE-1001 THEN the note is recorded with status 201"""
    status, body = call('POST', '/cases/CASE-1001/notes', {'X-Principal': 'risk-1', 'X-Tenant': 'retail', 'X-Scopes': 'cases:write'}, {'note': 'Customer confirmed the card was stolen', 'author': 'risk-1'})
    assert status == 201, body
    assert body.get('parent') == 'CASE-1001', body
    assert body.get('author') == 'risk-1', body

def test_ac_6(call):
    """REQ-3 / AC-6: GIVEN an analyst with cases:write WHEN it adds a note to CASE-1003, which was referred to the AML officer THEN the request is rejected with 409"""
    status, body = call('POST', '/cases/CASE-1003/notes', {'X-Principal': 'risk-1', 'X-Tenant': 'retail', 'X-Scopes': 'cases:write'}, {'note': 'Following up on cash deposits', 'author': 'risk-1'})
    assert status == 409, body

def test_ac_7(call):
    """REQ-3 / AC-7: GIVEN an analyst with cases:write WHEN it adds a note shorter than 10 characters THEN the request is rejected with 400"""
    status, body = call('POST', '/cases/CASE-1001/notes', {'X-Principal': 'risk-1', 'X-Tenant': 'retail', 'X-Scopes': 'cases:write'}, {'note': 'ok', 'author': 'risk-1'})
    assert status == 400, body
