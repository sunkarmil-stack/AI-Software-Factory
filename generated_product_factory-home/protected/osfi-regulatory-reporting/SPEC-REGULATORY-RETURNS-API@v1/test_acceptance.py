"""PROTECTED acceptance tests (QA-owned; do not edit via implementation branches) — from SPEC-REGULATORY-RETURNS-API@v1."""

def test_ac_1(call):
    """REQ-1 / AC-1: GIVEN a reporting user in tenant finance with scope returns:read WHEN it lists returns THEN the three finance returns are returned with status 200"""
    status, body = call('GET', '/returns', {'X-Principal': 'fin-1', 'X-Tenant': 'finance', 'X-Scopes': 'returns:read'}, None)
    assert status == 200, body
    assert len(body['items']) == 3, body

def test_ac_2(call):
    """REQ-2 / AC-2: GIVEN a reporting user in tenant finance with returns:read WHEN it reads LR-2026-10-31 THEN the return is returned with its status and headline ratio and without reviewer notes"""
    status, body = call('GET', '/returns/LR-2026-10-31', {'X-Principal': 'fin-1', 'X-Tenant': 'finance', 'X-Scopes': 'returns:read'}, None)
    assert status == 200, body
    assert body.get('status') == 'certified', body
    assert body.get('headline_pct') == 4.28, body
    assert 'reviewer_notes' not in body, body

def test_ac_3(call):
    """REQ-2 / AC-3: GIVEN a user in tenant retail with returns:read WHEN it reads finance return LR-2026-10-31 THEN 404 is returned and no return data is disclosed"""
    status, body = call('GET', '/returns/LR-2026-10-31', {'X-Principal': 'ret-1', 'X-Tenant': 'retail', 'X-Scopes': 'returns:read'}, None)
    assert status == 404, body
    assert 'headline_pct' not in body, body

def test_ac_4(call):
    """REQ-2 / AC-4: GIVEN a finance user without returns:read WHEN it lists returns THEN the request is denied with 403"""
    status, body = call('GET', '/returns', {'X-Principal': 'fin-2', 'X-Tenant': 'finance', 'X-Scopes': 'other:read'}, None)
    assert status == 403, body

def test_ac_5(call):
    """REQ-3 / AC-5: GIVEN a filer in tenant finance with returns:file and step-up authentication WHEN it records receipt RRS-20261113-0042 for certified return LA-2026-10-31 THEN the receipt is recorded with status 201"""
    status, body = call('POST', '/returns/LA-2026-10-31/receipts', {'X-Principal': 'fin-1', 'X-Tenant': 'finance', 'X-Scopes': 'returns:file', 'X-MFA': 'true'}, {'receipt_ref': 'RRS-20261113-0042', 'filed_by': 'fin-1'})
    assert status == 201, body
    assert body.get('receipt_ref') == 'RRS-20261113-0042', body
    assert body.get('parent') == 'LA-2026-10-31', body

def test_ac_6(call):
    """REQ-3 / AC-6: GIVEN a filer with returns:file but without step-up authentication WHEN it records a receipt THEN the request is denied with 403"""
    status, body = call('POST', '/returns/LA-2026-10-31/receipts', {'X-Principal': 'fin-1', 'X-Tenant': 'finance', 'X-Scopes': 'returns:file'}, {'receipt_ref': 'RRS-20261113-0042', 'filed_by': 'fin-1'})
    assert status == 403, body

def test_ac_7(call):
    """REQ-3 / AC-7: GIVEN a filer with returns:file and step-up authentication WHEN it records a receipt for LR-2026-07-31, which is already filed THEN the request is rejected with 409"""
    status, body = call('POST', '/returns/LR-2026-07-31/receipts', {'X-Principal': 'fin-1', 'X-Tenant': 'finance', 'X-Scopes': 'returns:file', 'X-MFA': 'true'}, {'receipt_ref': 'RRS-20260828-0007', 'filed_by': 'fin-1'})
    assert status == 409, body

def test_ac_8(call):
    """REQ-3 / AC-8: GIVEN a filer with returns:file and step-up authentication WHEN it records a receipt with a reference shorter than 8 characters THEN the request is rejected with 400"""
    status, body = call('POST', '/returns/LA-2026-10-31/receipts', {'X-Principal': 'fin-1', 'X-Tenant': 'finance', 'X-Scopes': 'returns:file', 'X-MFA': 'true'}, {'receipt_ref': 'RRS-1', 'filed_by': 'fin-1'})
    assert status == 400, body
