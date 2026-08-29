"""PAN redaction: card numbers must be masked to their last four digits before
anything is stored. The last four are kept (PCI-DSS allows it); the rest go."""

from northwind.services import scrub


def test_masks_visa_keeps_last_four():
    out = scrub.mask_pans("Charge on 4111 1111 1111 1111 today")
    assert "4111 1111 1111" not in out      # the rest is gone
    assert out.endswith("1111 today")        # last four survive
    assert "•" in out


def test_masks_bare_16_digit_pan():
    # A real, Luhn-valid Visa test number with no separators.
    out = scrub.mask_pans("ref 4111111111111111")
    assert "4111111111111111" not in out
    assert out.endswith("1111")
    assert out.count("•") == 12               # 16 digits − last 4 kept


def test_masks_amex_15_digit_grouping():
    # Amex prints 4-6-5; last four (0005) must remain.
    out = scrub.mask_pans("AMEX 3782 822463 10005")
    assert "822463" not in out
    assert out.endswith("0005")


def test_hyphen_separated_pan():
    out = scrub.mask_pans("card 4111-1111-1111-1111")
    assert out.endswith("1111")
    assert "4111-1111-1111" not in out


def test_does_not_touch_short_numbers():
    # Dates, amounts, phone numbers, invoice refs under 13 digits are untouched.
    for safe in ("Invoice 2026-06-30", "R 1 234 567.89", "Tel 0821234567",
                 "Order #INV-00482"):
        assert scrub.mask_pans(safe) == safe


def test_does_not_clobber_long_non_card_number():
    # A 16-digit run that fails Luhn (e.g. a long reference) is left alone.
    assert not scrub._luhn_ok("1234567890123456")
    assert scrub.mask_pans("ref 1234567890123456") == "ref 1234567890123456"


def test_non_strings_pass_through():
    assert scrub.mask_pans(None) is None
    assert scrub.mask_pans(1234) == 1234


def test_contains_pan_flag():
    assert scrub.contains_pan("4111 1111 1111 1111")
    assert not scrub.contains_pan("just a normal merchant name")


def test_scrub_obj_walks_nested_json():
    blob = {"vendor": "Autostop", "note": "paid with 4111 1111 1111 1111",
            "items": ["fuel", "card 4111111111111111"]}
    cleaned = scrub.scrub_obj(blob)
    assert not scrub.contains_pan(cleaned["note"])
    assert not scrub.contains_pan(cleaned["items"][1])
    assert cleaned["vendor"] == "Autostop"       # untouched


# ── Wider PII (scrub_pii, used on receipt-OCR output) ─────────────────────────

def test_scrub_pii_masks_email_keeps_domain():
    out = scrub.scrub_pii("Contact john.doe@gmail.com for queries")
    assert "john.doe" not in out
    assert "@gmail.com" in out                # domain kept for legibility
    assert "•" in out


def test_scrub_pii_masks_sa_phone_keeps_last_three():
    for raw in ("Tel 0821234567", "Call 082 123 4567", "cell +27 82 123 4567"):
        out = scrub.scrub_pii(raw)
        assert "1234567" not in out           # the run is broken up
        assert out.endswith("567")            # last three survive
        assert "•" in out


def test_scrub_pii_masks_sa_id_fully():
    # A real-format SA ID (YYMMDD + Luhn-valid): 8001015009087.
    assert scrub._looks_like_sa_id("8001015009087")
    out = scrub.scrub_pii("ID 8001015009087 on file")
    assert "8001015009087" not in out
    assert not any(ch.isdigit() for ch in out.split("ID ")[1].split(" on")[0])


def test_scrub_pii_still_masks_pans():
    out = scrub.scrub_pii("card 4111 1111 1111 1111")
    assert not scrub.contains_pan(out)
    assert out.endswith("1111")               # PAN last-4 behaviour preserved


def test_scrub_pii_leaves_amounts_and_dates():
    # A 13-digit reference that isn't a card or ID, an amount, and a date are safe.
    for safe in ("Total R1 234.56", "Date 2026-06-30", "Invoice INV-00482"):
        assert scrub.scrub_pii(safe) == safe


def test_scrub_pii_obj_walks_nested_json():
    blob = {"vendor": "Greenfields",
            "customer": {"email": "sam@example.co.za", "phone": "0119876543"},
            "id_number": "8001015009087"}
    cleaned = scrub.scrub_pii_obj(blob)
    assert "sam@" not in cleaned["customer"]["email"]
    assert cleaned["customer"]["phone"].endswith("543")
    assert "8001015009087" not in cleaned["id_number"]
    assert cleaned["vendor"] == "Greenfields"
