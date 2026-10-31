import io
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.sql.dml import Update
from werkzeug.security import generate_password_hash

os.environ["DATABASE_URL"] = "sqlite://"

from main import Board, Firmware, Order, User, app as default_app, create_app, db


PASSWORD = "correct-horse-battery"
PASSWORD_HASH = generate_password_hash(PASSWORD)


def tearDownModule():
    with default_app.app_context():
        db.session.remove()
        db.engine.dispose()


class STM32StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "WTF_CSRF_ENABLED": False,
            "UPLOADED_FIRMWARE_DEST": self.temp_dir.name,
        })
        self.client = self.app.test_client()
        with self.app.app_context():
            for name, role in (("member", "member"), ("second", "member"), ("admin", "admin")):
                db.session.add(User(
                    username=name, email=f"{name}@example.com",
                    password_hash=PASSWORD_HASH, role=role,
                ))
            db.session.commit()
            self.board_id = Board.query.filter_by(name="NUCLEO-F446RE").one().id
            self.second_board_id = Board.query.filter_by(name="NUCLEO-G071RB").one().id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.temp_dir.cleanup()

    def sign_in(self, username="member", client=None):
        client = client or self.client
        response = client.post(
            "/login", data={"username": username, "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 302)
        return response

    def add_product(self, board_id=None, quantity=1, client=None):
        client = client or self.client
        response = client.post(
            f"/cart/add/{board_id or self.board_id}", data={"quantity": quantity}
        )
        self.assertEqual(response.status_code, 302)
        return response

    def checkout_data(self, client=None):
        client = client or self.client
        self.assertEqual(client.get("/checkout").status_code, 200)
        with client.session_transaction() as session:
            key = session["checkout_key"]
        return {
            "checkout_key": key,
            "full_name": "Demo Customer",
            "email": "demo@example.com",
            "address": "123 Example Street",
            "city": "Tunis",
            "postal_code": "1000",
            "country": "Tunisia",
        }

    @staticmethod
    def product_payload():
        return {
            "name": "NUCLEO-F401RE",
            "manufacturer": "STMicroelectronics",
            "mcu": "STM32F401RET6",
            "architecture": "Arm Cortex-M4",
            "cores": 1, "clock_mhz": 84, "flash_kb": 512,
            "stock": 10, "price_cents": 2200,
            "description": "A sample STM32 development board.",
        }

    def test_storefront_has_only_priced_stm32_products(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Shop the collection", response.data)
        self.assertIn(b"$25.00", response.data)
        self.assertNotIn(b"ESP32", response.data)
        self.assertNotIn(b"Arduino", response.data)
        data = self.client.get("/api/products").json
        self.assertEqual(data["count"], 6)
        self.assertEqual(data["currency"], "USD")
        for product in data["items"]:
            self.assertTrue(product["mcu"].startswith("STM32"))
            self.assertGreater(product["price_cents"], 0)
            self.assertEqual(product["manufacturer"], "STMicroelectronics")
        self.assertEqual(self.client.get("/api/boards").json, data)

    def test_product_detail_search_and_family_filter(self):
        response = self.client.get(f"/products/{self.board_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"STM32F446RET6", response.data)
        self.assertIn(b"Add to cart", response.data)
        search = self.client.get("/?q=F446")
        self.assertIn(b"NUCLEO-F446RE", search.data)
        self.assertNotIn(b"NUCLEO-H743ZI2", search.data)
        family = self.client.get("/?family=STM32H7")
        self.assertIn(b"NUCLEO-H743ZI2", family.data)
        self.assertNotIn(b"NUCLEO-F446RE", family.data)
        empty = self.client.get("/?q=not-a-device")
        self.assertIn(b"No devices match", empty.data)
        self.assertEqual(self.client.get("/api/products/99999").status_code, 404)

    def test_guest_cart_add_update_and_remove(self):
        self.add_product(quantity=2)
        self.add_product(quantity=1)
        with self.client.session_transaction() as session:
            self.assertEqual(session["cart"][str(self.board_id)], 3)
        self.assertIn(b"$75.00", self.client.get("/cart").data)
        self.client.post(f"/cart/update/{self.board_id}", data={"quantity": 4})
        with self.client.session_transaction() as session:
            self.assertEqual(session["cart"][str(self.board_id)], 4)
        self.client.post(f"/cart/remove/{self.board_id}")
        self.assertIn(b"Add an STM32 device", self.client.get("/cart").data)

    def test_cart_rejects_invalid_quantities_and_sold_out_devices(self):
        for quantity in ("0", "-1", "1.5", "oops", "51", "13"):
            with self.subTest(quantity=quantity):
                self.add_product(quantity=quantity)
                with self.client.session_transaction() as session:
                    self.assertFalse(session.get("cart"))
        with self.app.app_context():
            sold_out_id = Board.query.filter_by(stock=0).one().id
        self.add_product(board_id=sold_out_id)
        with self.client.session_transaction() as session:
            self.assertFalse(session.get("cart"))

    def test_cart_update_rejects_over_stock(self):
        self.add_product(quantity=2)
        self.client.post(f"/cart/update/{self.board_id}", data={"quantity": 13})
        with self.client.session_transaction() as session:
            self.assertEqual(session["cart"][str(self.board_id)], 2)
        self.assertEqual(self.client.post("/cart/remove/99999").status_code, 404)

    def test_checkout_requires_sign_in_and_preserves_guest_cart(self):
        self.add_product()
        response = self.client.get("/checkout")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login?next=/checkout", response.location)
        self.sign_in()
        self.assertEqual(self.client.get("/checkout").status_code, 200)
        with self.client.session_transaction() as session:
            self.assertEqual(session["cart"][str(self.board_id)], 1)

    def test_checkout_creates_order_updates_stock_and_clears_cart(self):
        self.sign_in()
        self.add_product(quantity=2)
        self.add_product(self.second_board_id)
        data = self.checkout_data()
        data["total_cents"] = "1"
        response = self.client.post("/checkout", data=data)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/orders/", response.location)
        with self.app.app_context():
            order = Order.query.one()
            self.assertEqual(order.total_cents, 6800)
            self.assertEqual(order.status, "pending")
            self.assertEqual(len(order.items), 2)
            self.assertEqual(Board.query.filter_by(name="NUCLEO-F446RE").one().stock, 10)
            self.assertEqual(sum(item.total_cents for item in order.items), 6800)
        with self.client.session_transaction() as session:
            self.assertNotIn("cart", session)
            self.assertNotIn("checkout_key", session)
        self.assertIn(b"No payment has been taken", self.client.get(response.location).data)

    def test_checkout_replay_does_not_duplicate_order(self):
        self.sign_in()
        self.add_product()
        data = self.checkout_data()
        first = self.client.post("/checkout", data=data)
        repeated = self.client.post("/checkout", data=data)
        self.assertEqual(repeated.location, first.location)
        with self.app.app_context():
            self.assertEqual(Order.query.count(), 1)
            self.assertEqual(db.session.get(Board, self.board_id).stock, 11)

    def test_checkout_validates_delivery_details(self):
        self.sign_in()
        self.add_product()
        data = self.checkout_data()
        data.update(full_name=" ", address="x", email="invalid")
        response = self.client.post("/checkout", data=data)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"This field is required", response.data)
        with self.app.app_context():
            self.assertEqual(Order.query.count(), 0)
            self.assertEqual(db.session.get(Board, self.board_id).stock, 12)

    def test_empty_cart_and_stale_stock_cannot_be_checked_out(self):
        self.sign_in()
        self.assertEqual(self.client.get("/checkout").location, "/cart")
        self.add_product(quantity=2)
        data = self.checkout_data()
        with self.app.app_context():
            db.session.get(Board, self.board_id).stock = 1
            db.session.commit()
        response = self.client.post("/checkout", data=data)
        self.assertEqual(response.location, "/cart")
        self.assertIn(b"Review unavailable items", self.client.get("/cart").data)
        with self.app.app_context():
            self.assertEqual(Order.query.count(), 0)

    def test_checkout_rolls_back_all_stock_when_second_update_fails(self):
        self.sign_in()
        self.add_product()
        self.add_product(self.second_board_id)
        data = self.checkout_data()
        execute = db.session.execute
        updates = 0

        def stock_race(statement, *args, **kwargs):
            nonlocal updates
            if isinstance(statement, Update):
                updates += 1
                if updates == 2:
                    return SimpleNamespace(rowcount=0)
            return execute(statement, *args, **kwargs)

        with patch.object(db.session, "execute", side_effect=stock_race):
            response = self.client.post("/checkout", data=data)
        self.assertEqual(response.location, "/cart")
        self.assertEqual(updates, 2)
        with self.app.app_context():
            self.assertEqual(Order.query.count(), 0)
            self.assertEqual(db.session.get(Board, self.board_id).stock, 12)
            self.assertEqual(db.session.get(Board, self.second_board_id).stock, 15)

    def test_checkout_key_must_belong_to_current_session(self):
        self.sign_in()
        self.add_product()
        data = self.checkout_data()
        data["checkout_key"] = "0" * 32
        self.assertEqual(self.client.post("/checkout", data=data).status_code, 400)
        with self.app.app_context():
            self.assertEqual(Order.query.count(), 0)

    def test_concurrent_checkouts_cannot_oversell_or_duplicate_an_order(self):
        for replay in (False, True):
            with self.subTest(replay=replay):
                path = Path(self.temp_dir.name) / f"concurrent-{replay}.db"
                concurrent_app = create_app({
                    "TESTING": True, "SECRET_KEY": "concurrency-test",
                    "SQLALCHEMY_DATABASE_URI": f"sqlite:///{path}",
                    "WTF_CSRF_ENABLED": False,
                    "UPLOADED_FIRMWARE_DEST": self.temp_dir.name,
                })
                initial_stock = 5 if replay else 1
                try:
                    with concurrent_app.app_context():
                        for name in ("buyer1", "buyer2"):
                            db.session.add(User(
                                username=name, email=f"{name}@example.com",
                                password_hash=PASSWORD_HASH,
                            ))
                        product = Board.query.filter_by(name="NUCLEO-F446RE").one()
                        product.stock = initial_stock
                        product_id = product.id
                        db.session.commit()
                    clients = [concurrent_app.test_client(), concurrent_app.test_client()]
                    forms = []
                    for client, name in zip(clients, ("buyer1", "buyer2")):
                        self.sign_in(name, client)
                        self.add_product(product_id, client=client)
                        forms.append(self.checkout_data(client))
                    if replay:
                        clients[1].set_cookie("session", clients[0].get_cookie("session").value)
                        forms[1] = forms[0]
                    barrier = threading.Barrier(2)

                    def place_order(index):
                        barrier.wait(timeout=10)
                        return clients[index].post("/checkout", data=forms[index])

                    with ThreadPoolExecutor(max_workers=2) as pool:
                        responses = list(pool.map(place_order, range(2)))
                    self.assertTrue(all(response.status_code == 302 for response in responses))
                    if replay:
                        self.assertEqual(responses[0].location, responses[1].location)
                        for client in clients:
                            with client.session_transaction() as session:
                                self.assertNotIn("cart", session)
                    with concurrent_app.app_context():
                        self.assertEqual(Order.query.count(), 1)
                        self.assertEqual(db.session.get(Board, product_id).stock, initial_stock - 1)
                finally:
                    with concurrent_app.app_context():
                        db.session.remove()
                        db.engine.dispose()

    def test_orders_are_private_and_keep_original_prices(self):
        self.sign_in()
        self.add_product(quantity=2)
        response = self.client.post("/checkout", data=self.checkout_data())
        with self.app.app_context():
            order_id = Order.query.one().id
            db.session.get(Board, self.board_id).price_cents = 9999
            db.session.commit()
        self.assertIn(b"$50.00", self.client.get(response.location).data)
        self.assertEqual(self.client.get("/api/orders").json["count"], 1)
        self.assertEqual(self.client.get(f"/api/orders/{order_id}").json["total_cents"], 5000)
        other = self.app.test_client()
        self.sign_in("second", other)
        self.assertEqual(other.get(f"/orders/{order_id}").status_code, 403)
        self.assertEqual(other.get(f"/api/orders/{order_id}").status_code, 403)
        self.assertEqual(other.get("/api/orders").json["count"], 0)
        self.assertEqual(self.app.test_client().get("/api/orders").status_code, 401)

    def test_admin_dashboard_inventory_and_order_status(self):
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.sign_in()
        self.assertEqual(self.client.get("/admin/").status_code, 403)
        self.assertEqual(self.client.get("/admin/inventory/").status_code, 403)
        self.add_product()
        self.client.post("/checkout", data=self.checkout_data())
        with self.app.app_context():
            order_id = Order.query.one().id
        self.assertEqual(
            self.client.post(f"/admin/orders/{order_id}/status", data={"status": "shipped"}).status_code,
            403,
        )
        self.client.post("/logout")
        self.sign_in("admin")
        page = self.client.get("/admin/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Sales dashboard", page.data)
        self.assertIn(b"$25.00", page.data)
        for path in ("/admin/inventory/", "/admin/inventory/new/", "/admin/sales/", "/admin/firmware/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.client.post(f"/admin/orders/{order_id}/status", data={"status": "shipped"})
        self.client.post(f"/admin/orders/{order_id}/status", data={"status": "pending"})
        with self.app.app_context():
            self.assertEqual(db.session.get(Order, order_id).status, "shipped")
        self.assertEqual(
            self.client.post(f"/admin/orders/{order_id}/status", data={"status": "bogus"}).status_code,
            400,
        )

    def test_admin_product_api_create_update_and_archive(self):
        payload = self.product_payload()
        self.assertEqual(self.client.post("/api/products", json=payload).status_code, 401)
        self.sign_in()
        self.assertEqual(self.client.post("/api/products", json=payload).status_code, 403)
        self.client.post("/logout")
        self.sign_in("admin")
        response = self.client.post("/api/products", json=payload)
        self.assertEqual(response.status_code, 201)
        product_id = response.json["id"]
        self.assertEqual(self.client.post("/api/products", json=payload).status_code, 409)
        update = self.client.patch(f"/api/products/{product_id}", json={"price_cents": 2300, "stock": 0})
        self.assertEqual(update.status_code, 200)
        self.assertEqual(update.json["price_cents"], 2300)
        self.assertEqual(update.json["stock"], 0)
        self.assertEqual(self.client.delete(f"/api/products/{product_id}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/products/{product_id}").status_code, 404)
        with self.app.app_context():
            self.assertFalse(db.session.get(Board, product_id).is_listed)

    def test_api_rejects_non_stm32_devices_bad_prices_and_unknown_fields(self):
        self.sign_in("admin")
        for changes in (
            {"mcu": "ESP32", "manufacturer": "Espressif"},
            {"price_cents": -1}, {"price_cents": 19.99}, {"price_cents": True},
            {"price_cents": 10 ** 100},
            {"stock": -1}, {"stock": 1.5}, {"clock_mhz": float("nan")},
            {"is_listed": "true"}, {"unexpected": 5},
        ):
            with self.subTest(changes=changes):
                payload = self.product_payload()
                payload.update(changes)
                self.assertEqual(self.client.post("/api/products", json=payload).status_code, 400)
        self.assertEqual(self.client.post("/api/products", json=[]).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/products/{self.board_id}", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/boards", json=self.product_payload()).status_code, 201)

    def test_archiving_keeps_order_history_and_blocks_cart(self):
        self.sign_in()
        self.add_product()
        self.client.post("/checkout", data=self.checkout_data())
        with self.app.app_context():
            order_id = Order.query.one().id
        self.client.post("/logout")
        self.sign_in("admin")
        self.client.delete(f"/api/products/{self.board_id}")
        self.add_product()
        with self.client.session_transaction() as session:
            self.assertFalse(session.get("cart"))
        self.assertEqual(self.client.get(f"/orders/{order_id}").status_code, 200)
        with self.app.app_context():
            self.assertEqual(Order.query.one().items[0].product_name, "NUCLEO-F446RE")

    def test_registration_passwords_and_login_redirects(self):
        response = self.client.post("/register", data={
            "username": " newcustomer ", "email": "new@example.com",
            "password": PASSWORD, "role": "admin",
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            user = User.query.filter_by(username="newcustomer").one()
            self.assertEqual(user.role, "member")
            self.assertNotEqual(user.password_hash, PASSWORD)
            self.assertTrue(user.check_password(PASSWORD))
        self.client.post("/logout")
        response = self.client.post("/login?next=https://example.net", data={
            "username": "newcustomer", "password": PASSWORD,
        })
        self.assertEqual(response.location, "/")
        self.client.post("/logout")
        self.assertIn(b"Invalid username or password", self.client.post(
            "/login", data={"username": "newcustomer", "password": "wrong"}
        ).data)

    def test_firmware_upload_download_and_metadata_are_private(self):
        self.sign_in()
        response = self.client.post(f"/boards/{self.board_id}/firmware", data={
            "label": "private-blink-v1",
            "firmware": (io.BytesIO(b"firmware image"), "blink.hex"),
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            firmware_id = Firmware.query.one().id
        response = self.client.get(f"/firmware/{firmware_id}/download")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"firmware image")
        response.close()
        other = self.app.test_client()
        self.sign_in("second", other)
        self.assertEqual(other.get(f"/firmware/{firmware_id}/download").status_code, 403)
        self.assertNotIn(b"private-blink-v1", other.get(f"/products/{self.board_id}").data)
        self.assertNotIn(b"private-blink-v1", self.app.test_client().get(f"/products/{self.board_id}").data)
        self.assertEqual(self.client.get("/_uploads/firmware/blink.hex").status_code, 404)

    def test_invalid_firmware_files_and_oversized_requests_are_rejected(self):
        self.sign_in()
        for filename in ("bad.html", "bad.exe", "no_extension"):
            self.client.post(f"/boards/{self.board_id}/firmware", data={
                "label": "not allowed", "firmware": (io.BytesIO(b"test"), filename),
            })
        with self.app.app_context():
            self.assertEqual(Firmware.query.count(), 0)
        self.app.config["MAX_CONTENT_LENGTH"] = 100
        self.assertEqual(self.client.post(f"/boards/{self.board_id}/firmware", data={
            "label": "too large", "firmware": (io.BytesIO(b"x" * 200), "large.hex"),
        }).status_code, 413)

    def test_csrf_protects_cart_checkout_api_and_admin_forms(self):
        self.sign_in("admin")
        self.app.config["WTF_CSRF_ENABLED"] = True
        self.assertEqual(self.client.post(
            f"/cart/add/{self.board_id}", data={"quantity": 1}
        ).status_code, 400)
        token = self.client.get("/api/csrf").json["csrf_token"]
        self.assertEqual(self.client.post(
            f"/cart/add/{self.board_id}", data={"quantity": 1, "csrf_token": token}
        ).status_code, 302)
        self.assertEqual(self.client.post(
            "/api/products", json=self.product_payload()
        ).status_code, 400)
        self.assertEqual(self.client.post(
            "/api/products", json=self.product_payload(), headers={"X-CSRFToken": token}
        ).status_code, 201)
        admin_form = self.client.get("/admin/inventory/new/")
        self.assertIn(b'name="csrf_token"', admin_form.data)
        self.assertIn(b'name="clock_mhz"', admin_form.data)
        payload = self.product_payload()
        payload.pop("manufacturer")
        payload.update(name="NUCLEO-F401RE-CSRF", is_listed="y", csrf_token=token)
        self.assertEqual(self.client.post("/admin/inventory/new/", data=payload).status_code, 302)
        data = self.checkout_data()
        self.assertEqual(self.client.post("/checkout", data=data).status_code, 400)
        data["csrf_token"] = token
        self.assertEqual(self.client.post("/checkout", data=data).status_code, 302)
        self.assertEqual(self.client.post("/logout").status_code, 400)

    def test_static_assets_security_headers_and_friendly_errors(self):
        response = self.client.get("/")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("script-src 'self' 'nonce-", response.headers["Content-Security-Policy"])
        css = self.client.get("/static/style.css")
        self.assertEqual(css.status_code, 200)
        cached_css = self.client.get(
            "/static/style.css", headers={"If-None-Match": css.headers["ETag"]}
        )
        self.assertEqual(cached_css.status_code, 304)
        css.close()
        cached_css.close()
        self.assertIn(b"Back to the shop", self.client.get("/not-found").data)
        self.sign_in()
        self.assertEqual(self.client.get("/orders").headers["Cache-Control"], "no-store")

    def test_admin_cli_creates_validated_administrator(self):
        result = self.app.test_cli_runner().invoke(
            args=["create-admin"],
            input=f"newadmin\nnewadmin@example.com\n{PASSWORD}\n{PASSWORD}\n",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        with self.app.app_context():
            self.assertTrue(User.query.filter_by(username="newadmin").one().is_admin)


class LegacyDatabaseTests(unittest.TestCase):
    def test_upgrade_preserves_accounts_firmware_and_inventory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript("""
                    CREATE TABLE user (
                        id INTEGER PRIMARY KEY, username VARCHAR(40) UNIQUE NOT NULL,
                        email VARCHAR(120) UNIQUE NOT NULL, password_hash VARCHAR(256) NOT NULL,
                        role VARCHAR(20) NOT NULL, created_at DATETIME NOT NULL
                    );
                    CREATE TABLE board (
                        id INTEGER PRIMARY KEY, name VARCHAR(100) UNIQUE NOT NULL,
                        manufacturer VARCHAR(80) NOT NULL, mcu VARCHAR(80) NOT NULL,
                        architecture VARCHAR(40) NOT NULL, cores INTEGER NOT NULL,
                        clock_mhz FLOAT NOT NULL, flash_kb INTEGER NOT NULL,
                        stock INTEGER NOT NULL, description TEXT NOT NULL,
                        created_at DATETIME NOT NULL
                    );
                    CREATE TABLE firmware (
                        id INTEGER PRIMARY KEY, board_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL, label VARCHAR(100) NOT NULL,
                        original_name VARCHAR(180) NOT NULL,
                        stored_name VARCHAR(180) UNIQUE NOT NULL, created_at DATETIME NOT NULL
                    );
                """)
                connection.execute(
                    "INSERT INTO user VALUES (1, ?, ?, ?, 'admin', '2026-10-24 00:00:00')",
                    ("existing", "existing@example.com", PASSWORD_HASH),
                )
                connection.execute(
                    "INSERT INTO board VALUES (41, 'NUCLEO-F446RE', 'STMicroelectronics', "
                    "'STM32F446RET6', 'Arm Cortex-M4', 1, 180, 512, 7, "
                    "'Original description', '2026-10-24 00:00:00')"
                )
                connection.execute(
                    "INSERT INTO board VALUES (42, 'ESP32-DevKitC', 'Espressif', 'ESP32', "
                    "'Xtensa LX6', 2, 240, 4096, 8, 'Legacy board', '2026-10-24 00:00:00')"
                )
                connection.execute(
                    "INSERT INTO firmware VALUES (5, 41, 1, 'old-build', "
                    "'old.hex', 'stored.hex', '2026-10-24 00:00:00')"
                )
                connection.commit()
            upload_dir = Path(temp_dir) / "uploads"
            upload_dir.mkdir()
            (upload_dir / "stored.hex").write_bytes(b"existing firmware")
            config = {
                "TESTING": True, "SECRET_KEY": "migration-test",
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{path}",
                "WTF_CSRF_ENABLED": False,
                "UPLOADED_FIRMWARE_DEST": str(upload_dir),
            }
            for _ in range(2):
                app = create_app(config)
                with app.app_context():
                    user = db.session.get(User, 1)
                    self.assertTrue(user.is_admin)
                    self.assertTrue(user.check_password(PASSWORD))
                    board = db.session.get(Board, 41)
                    self.assertEqual(board.stock, 7)
                    self.assertEqual(board.description, "Original description")
                    self.assertEqual(board.price_cents, 2500)
                    self.assertTrue(board.is_listed)
                    self.assertFalse(db.session.get(Board, 42).is_listed)
                    self.assertEqual(db.session.get(Firmware, 5).board_id, 41)
                    self.assertEqual(Board.query.count(), 7)
                    db.session.remove()
                    db.engine.dispose()
                self.assertEqual(app.test_client().get("/api/products").json["count"], 6)
                with app.app_context():
                    db.session.remove()
                    db.engine.dispose()
            self.assertTrue(path.with_name("legacy.before-store.db").exists())
            self.assertEqual((upload_dir / "stored.hex").read_bytes(), b"existing firmware")


if __name__ == "__main__":
    unittest.main()
