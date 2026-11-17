def test_the_privacy_policy_renders(client, db):
    response = client.get("/social/privacy/")

    assert response.status_code == 200
    assert b"never store non-public posts" in response.content
