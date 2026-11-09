from src.miniapp import normalize_email


def test_email_is_trimmed_and_lowercased():
    assert normalize_email("  Alice.Example@Example.COM \n") == "alice.example@example.com"
    assert normalize_email("BOB@EXAMPLE.COM") == "bob@example.com"
