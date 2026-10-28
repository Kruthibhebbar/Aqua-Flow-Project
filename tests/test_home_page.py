"""Public home page: honest copy, correct nav per session, real customer summary."""

from datetime import datetime, timedelta

from tests.helpers import create_user, login

FAKE = ["10,000", "ISO Certified", "ISO certified", "24/7", "Emergency Delivery", "Your City, State",
        "Scheduled Subscription", "thousands of happy"]


def _html(client):
    """Visible markup only: CSS and HTML comments are developer notes, not page content."""
    import re
    r = client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    html = re.sub(r"<style.*?</style>", "", html, flags=re.S)
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


def test_no_fabricated_claims_remain(client):
    html = _html(client)
    for text in FAKE:
        assert text not in html, text
    assert "Math.random" not in html and "particles-container" not in html


def test_new_headline_ctas_and_sections(client):
    html = _html(client)
    assert "Water Delivered." in html and "Convenience Tracked." in html
    assert "Book a Water Tanker" in html and "See How It Works" in html
    assert 'href="#how-it-works"' in html and 'id="how-it-works"' in html
    assert "Built for Convenient Water Delivery" in html
    assert "Everything You Need to" in html and "Ready to Schedule Your Water Delivery?" in html
    assert f"&copy; {datetime.now().year} AquaFlow" in html


def test_logged_out_nav_and_no_database_summary(client):
    html = _html(client)
    assert 'href="/login"' in html and 'href="/signup"' in html
    assert "Logout" not in html and 'id="my-bookings"' not in html
    # booking CTA follows the existing login flow
    assert 'href="/login" class="book-primary-btn"' in html


def test_logged_in_nav_has_logout_not_login(client, app):
    email, pw = create_user(app, name="Asha Rao")
    login(client, email, pw)
    html = _html(client)
    assert "Logout (Asha)" in html and 'class="nav-btn"><i class="fa-solid fa-right-to-bracket"></i> Login' not in html
    assert 'href="/booking" class="book-primary-btn"' in html and 'href="/dashboard"' in html


def test_logged_in_without_bookings_gets_empty_state_not_sample_numbers(client, app):
    email, pw = create_user(app)
    login(client, email, pw)
    html = _html(client)
    assert 'id="my-bookings"' in html and "no bookings yet" in html


def test_logged_in_summary_uses_only_own_bookings(client, app):
    email, pw = create_user(app)
    other, _ = create_user(app, email="o@example.com", name="Other Person")
    soon = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    for who, qty, st in [(email, 3000, "Pending"), (other, 7777, "Pending")]:
        app.bookings_collection.insert_one({
            "user_email": who, "status": st, "quantity": qty, "water_type": "Fresh Water",
            "delivery_date": soon, "created_at": datetime.now(), "fullname": "x"})
    login(client, email, pw)
    html = _html(client)
    assert "3000 L" in html and "7777" not in html and soon in html


def test_contact_cards_follow_configuration(client, monkeypatch):
    monkeypatch.setenv("CONTACT_PHONE", "")
    monkeypatch.setenv("CONTACT_EMAIL", "help@example.org")
    monkeypatch.delenv("CONTACT_ADDRESS", raising=False)
    html = _html(client)
    assert "mailto:help@example.org" in html and "tel:" not in html and "Visit Us" not in html


def test_contact_section_hidden_when_nothing_configured(client, monkeypatch):
    monkeypatch.setenv("CONTACT_PHONE", ""); monkeypatch.setenv("CONTACT_EMAIL", "")
    html = _html(client)
    assert "Get In" not in html
    assert 'href="#contact"' not in html


def test_nav_and_footer_links_point_to_existing_routes(client):
    import re
    html = _html(client)
    for path in set(re.findall(r'href="(/[a-z\-_]+)"', html)):
        if path.startswith("/static"):
            continue
        assert client.get(path).status_code in (200, 302, 301), path


def test_menu_button_is_accessible(client):
    html = _html(client)
    assert 'aria-controls="navButtons"' in html and 'aria-expanded="false"' in html and "Escape" in html


def test_hero_keeps_the_tanker_image_as_a_real_visible_image(client):
    html = _html(client)
    hero = html.split('id="home"')[1].split("</section>")[0]
    assert 'class="hero-truck"' in hero and "aquaflow-truck-hero.webp" in hero
    assert 'alt="AquaFlow water tanker truck"' in hero and 'width="1000" height="596"' in hero
    assert "Water Delivered." in hero and "Convenience Tracked." in hero
    assert "Book a Water Tanker" in hero and "See How It Works" in hero


def test_hero_feature_row_floating_card_and_wave(client):
    html = _html(client)
    hero = html.split('id="home"')[1].split('<!-- ===== ABOUT')[0]
    for t in ("Easy Online Booking", "Choose Your Schedule", "Track Delivery Status", "Secure Customer Account"):
        assert t in hero
    assert "Water Delivery, Made Simpler" in hero and "hero-wave" in hero and "hero-city" not in html
    # the capability showcase now sits below the hero, not directly under it
    assert html.index("Built for Convenient Water Delivery") > html.index('id="about"')
