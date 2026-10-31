import math
import os
import secrets
import uuid
from functools import wraps
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import click
from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template, request,
    send_from_directory, session, url_for,
)
from flask_admin import Admin, AdminIndexView, expose
from flask_admin.contrib.sqla import ModelView
from flask_login import (
    LoginManager, current_user, login_required, login_user, logout_user,
)
from flask_uploads import UploadNotAllowed, UploadSet, configure_uploads
from flask_wtf import CSRFProtect, FlaskForm
from flask_wtf.csrf import generate_csrf
from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.exceptions import HTTPException
from wtforms import FloatField
from wtforms.validators import InputRequired, NumberRange, ValidationError

from forms import CheckoutForm, LoginForm, QuantityForm, RegisterForm
from models import Board, Firmware, Order, OrderItem, ORDER_STATUSES, User, db, init_database


login_manager = LoginManager()
csrf = CSRFProtect()
firmware_files = UploadSet("firmware", extensions=("bin", "elf", "hex", "uf2"))
CURRENCY = "USD"
PRODUCT_FIELDS = (
    "name", "manufacturer", "mcu", "architecture", "cores", "clock_mhz",
    "flash_kb", "stock", "price_cents", "description", "is_listed",
)


def money(cents):
    return f"${cents // 100:,}.{cents % 100:02d}"


def catalog_filters():
    return (
        Board.is_listed.is_(True),
        Board.manufacturer == "STMicroelectronics",
        Board.mcu.ilike("STM32%"),
        Board.price_cents > 0,
    )


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def cart_entries():
    cart = dict(session.get("cart", {}))
    entries = []
    missing = []
    for board_id, quantity in cart.items():
        board = db.session.get(Board, int(board_id))
        if board is None:
            missing.append(board_id)
            continue
        entries.append({
            "board": board,
            "quantity": quantity,
            "total_cents": board.price_cents * quantity,
            "available": board.is_for_sale and board.stock >= quantity,
        })
    if missing:
        for board_id in missing:
            del cart[board_id]
        session["cart"] = cart
        flash("Products that no longer exist were removed from your cart.", "info")
    return entries


def board_to_dict(board):
    return {
        "id": board.id, "name": board.name, "manufacturer": board.manufacturer,
        "mcu": board.mcu, "family": board.family, "architecture": board.architecture,
        "cores": board.cores, "clock_mhz": board.clock_mhz,
        "flash_kb": board.flash_kb, "stock": board.stock,
        "price_cents": board.price_cents, "currency": CURRENCY,
        "description": board.description, "is_listed": board.is_listed,
    }


def board_from_payload(payload, board=None):
    unknown = set(payload) - set(PRODUCT_FIELDS)
    if unknown:
        return None, f"Unknown fields: {', '.join(sorted(unknown))}."
    if not payload:
        return None, "Provide at least one product field."
    values = {field: getattr(board, field) for field in PRODUCT_FIELDS} if board else {
        "manufacturer": "STMicroelectronics", "description": "", "is_listed": True,
    }
    values.update(payload)
    for field, limit in (
        ("name", 100), ("manufacturer", 80), ("mcu", 80),
        ("architecture", 40), ("description", 2000),
    ):
        value = values.get(field)
        if not isinstance(value, str) or len(value.strip()) > limit:
            return None, f"{field} must be text up to {limit} characters."
        values[field] = value.strip()
        if field != "description" and not values[field]:
            return None, f"{field} cannot be empty."
    values["mcu"] = values["mcu"].upper()
    if (
        values["manufacturer"] != "STMicroelectronics"
        or not values["mcu"].startswith("STM32")
    ):
        return None, "Only STMicroelectronics STM32 devices can be sold in this shop."
    for field, minimum, maximum in (
        ("cores", 1, 16), ("clock_mhz", 1, 10000),
        ("flash_kb", 1, 2_000_000), ("stock", 0, 1_000_000),
        ("price_cents", 1, 1_000_000),
    ):
        value = values.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, f"{field} must be a number."
        if field != "clock_mhz" and not isinstance(value, int):
            return None, f"{field} must be a whole number."
        if not minimum <= value <= maximum or not math.isfinite(value):
            return None, f"{field} must be between {minimum} and {maximum}."
    if not isinstance(values["is_listed"], bool):
        return None, "is_listed must be true or false."
    target = board if board is not None else Board()
    for field, value in values.items():
        setattr(target, field, value)
    return target, None


def order_to_dict(order):
    return {
        "id": order.id, "reference": order.reference, "status": order.status,
        "total_cents": order.total_cents, "currency": CURRENCY,
        "payment_method": "pay_on_delivery_demo",
        "created_at": order.created_at.isoformat(),
        "items": [
            {
                "product_name": item.product_name, "mcu": item.product_mcu,
                "quantity": item.quantity, "unit_price_cents": item.unit_price_cents,
                "total_cents": item.total_cents,
            }
            for item in order.items
        ],
    }


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "sqlite:///bitbench.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        UPLOADED_FIRMWARE_DEST=str(Path(app.instance_path) / "firmware"),
        UPLOADS_AUTOSERVE=False,
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("HTTPS_ONLY", "").lower()
        in {"1", "true", "yes"},
        WTF_CSRF_TIME_LIMIT=3600,
    )
    if test_config:
        app.config.update(test_config)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOADED_FIRMWARE_DEST"]).mkdir(parents=True, exist_ok=True)
    if not app.config["SECRET_KEY"]:
        key_file = Path(app.instance_path) / "secret.key"
        try:
            with key_file.open("x", encoding="ascii") as handle:
                handle.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        app.config["SECRET_KEY"] = key_file.read_text(encoding="ascii").strip()
        if not app.config["SECRET_KEY"]:
            raise RuntimeError("The instance secret.key file is empty.")

    db.init_app(app)
    login_manager.init_app(app)
    configure_uploads(app, firmware_files)
    login_manager.login_view = "login"
    login_manager.login_message_category = "info"
    app.jinja_env.filters["money"] = money

    @app.before_request
    def prepare_request():
        g.csp_nonce = secrets.token_urlsafe(16)

    csrf.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        if not user_id.isdecimal():
            return None
        return db.session.get(User, int(user_id))

    @login_manager.unauthorized_handler
    def require_sign_in():
        if request.path.startswith("/api/"):
            return jsonify(error="Sign in to access this resource."), 401
        flash("Please sign in to continue.", "info")
        return redirect(url_for("login", next=request.full_path.rstrip("?")))

    @app.context_processor
    def shop_context():
        return {
            "cart_count": sum(session.get("cart", {}).values()),
            "order_statuses": ORDER_STATUSES,
        }

    @app.after_request
    def set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; img-src 'self' data:; "
            f"script-src 'self' 'nonce-{g.csp_nonce}'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
        if current_user.is_authenticated or request.path in {"/cart", "/checkout"}:
            response.headers["Cache-Control"] = "no-store"
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    @app.errorhandler(HTTPException)
    def handle_http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error=error.description), error.code
        return render_template("error.html", error=error), error.code

    class AdminAccess:
        def is_accessible(self):
            return current_user.is_authenticated and current_user.is_admin

        def inaccessible_callback(self, name, **kwargs):
            if current_user.is_authenticated:
                abort(403)
            return redirect(url_for("login", next=request.path))

    class SalesDashboard(AdminAccess, AdminIndexView):
        @expose("/")
        def index(self):
            return self.render(
                "admin/dashboard.html",
                order_count=Order.query.count(),
                order_value=db.session.query(func.coalesce(func.sum(Order.total_cents), 0)).scalar(),
                pending_count=Order.query.filter_by(status="pending").count(),
                product_count=Board.query.filter(*catalog_filters()).count(),
                recent_orders=Order.query.order_by(Order.created_at.desc()).limit(10).all(),
                low_stock=Board.query.filter(*catalog_filters(), Board.stock <= 3).all(),
            )

    class SecureModelView(AdminAccess, ModelView):
        page_size = 20
        form_base_class = FlaskForm

    class ProductAdminView(SecureModelView):
        can_delete = False
        column_list = ("name", "mcu", "price_cents", "stock", "is_listed")
        column_labels = {"price_cents": "Price (USD cents)", "is_listed": "Listed"}
        column_searchable_list = ("name", "mcu")
        form_columns = (
            "name", "mcu", "architecture", "cores", "clock_mhz", "flash_kb",
            "stock", "price_cents", "description", "is_listed",
        )
        form_extra_fields = {
            "clock_mhz": FloatField(
                "Clock speed (MHz)",
                validators=[InputRequired(), NumberRange(min=1, max=10000)],
            )
        }

        def on_model_change(self, form, model, is_created):
            model.manufacturer = "STMicroelectronics"
            payload = {field: getattr(model, field) for field in PRODUCT_FIELDS}
            _, error = board_from_payload(payload, model)
            if error:
                raise ValidationError(error)

    class FirmwareAdminView(SecureModelView):
        can_create = False
        can_edit = False
        can_delete = False
        column_list = ("label", "original_name", "board", "uploader", "created_at")

    class OrderAdminView(SecureModelView):
        can_create = False
        can_edit = False
        can_delete = False
        can_view_details = True
        column_list = ("reference", "customer", "status", "total_cents", "created_at")
        column_labels = {"total_cents": "Total (USD cents)"}
        column_searchable_list = ("reference", "full_name", "email")
        column_filters = ("status",)

    admin = Admin(
        app, name="BitBench Store", index_view=SalesDashboard(name="Sales dashboard"),
        csp_nonce_generator=lambda: g.csp_nonce,
    )
    admin.add_view(ProductAdminView(Board, db, name="Products", endpoint="inventory"))
    admin.add_view(OrderAdminView(Order, db, name="Orders", endpoint="sales"))
    admin.add_view(FirmwareAdminView(Firmware, db, name="Firmware"))

    @app.get("/")
    def dashboard():
        query = request.args.get("q", "").strip()
        family = request.args.get("family", "").strip()
        products = Board.query.filter(*catalog_filters())
        if query:
            search = f"%{query}%"
            products = products.filter(or_(
                Board.name.ilike(search), Board.mcu.ilike(search),
                Board.architecture.ilike(search),
            ))
        if family:
            products = products.filter(func.substr(Board.mcu, 1, 7) == family)
        all_products = Board.query.filter(*catalog_filters()).all()
        return render_template(
            "dashboard.html", boards=products.order_by(Board.name).all(),
            query=query, selected_family=family,
            families=sorted({board.family for board in all_products}),
            board_count=len(all_products),
            available_count=sum(board.stock > 0 for board in all_products),
        )

    @app.get("/boards/<int:board_id>")
    @app.get("/products/<int:board_id>")
    def board_detail(board_id):
        board = db.get_or_404(Board, board_id)
        if not board.is_for_sale:
            abort(404, description="This product is not listed in the shop.")
        uploads = []
        if current_user.is_authenticated:
            files = Firmware.query.filter_by(board_id=board.id)
            if not current_user.is_admin:
                files = files.filter_by(user_id=current_user.id)
            uploads = files.order_by(Firmware.created_at.desc()).all()
        return render_template("board_detail.html", board=board, uploads=uploads)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        form = LoginForm()
        if form.validate_on_submit():
            user = User.query.filter_by(username=form.username.data).first()
            if user and user.check_password(form.password.data) and user.is_active:
                login_user(user)
                flash("Welcome back.", "success")
                next_url = request.args.get("next", "")
                target = urlsplit(urljoin(request.host_url, next_url))
                if (
                    next_url and target.scheme in {"http", "https"}
                    and target.netloc == urlsplit(request.host_url).netloc
                    and "\\" not in next_url
                ):
                    return redirect(next_url)
                return redirect(url_for("dashboard"))
            flash("Invalid username or password.", "error")
        return render_template("login.html", form=form)

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        form = RegisterForm()
        if form.validate_on_submit():
            user = User(
                username=form.username.data, email=form.email.data.lower(), role="member"
            )
            user.set_password(form.password.data)
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("That username or email is already registered.", "error")
            else:
                login_user(user)
                flash("Your customer account is ready.", "success")
                return redirect(url_for("dashboard"))
        return render_template("register.html", form=form)

    @app.post("/logout")
    @login_required
    def logout():
        logout_user()
        session.pop("checkout_key", None)
        flash("You have been signed out.", "success")
        return redirect(url_for("dashboard"))

    @app.get("/cart")
    def view_cart():
        entries = cart_entries()
        return render_template(
            "cart.html", entries=entries,
            total_cents=sum(item["total_cents"] for item in entries),
            can_checkout=bool(entries) and all(item["available"] for item in entries),
        )

    @app.post("/cart/add/<int:board_id>")
    def add_to_cart(board_id):
        board = db.get_or_404(Board, board_id)
        form = QuantityForm()
        cart = dict(session.get("cart", {}))
        if not form.validate_on_submit():
            flash("Choose a quantity from 1 to 50.", "error")
        elif not board.is_for_sale:
            flash("This product is no longer for sale.", "error")
        elif str(board.id) not in cart and len(cart) >= 20:
            flash("Your cart can contain up to 20 different products.", "error")
        else:
            quantity = cart.get(str(board.id), 0) + form.quantity.data
            if quantity > min(board.stock, 50):
                flash(f"Only {min(board.stock, 50)} units can be added to your cart.", "error")
            else:
                cart[str(board.id)] = quantity
                session["cart"] = cart
                flash(f"{board.name} added to your cart.", "success")
        return redirect(url_for("view_cart"))

    @app.post("/cart/update/<int:board_id>")
    def update_cart(board_id):
        cart = dict(session.get("cart", {}))
        if str(board_id) not in cart:
            abort(404, description="This product is not in your cart.")
        board = db.get_or_404(Board, board_id)
        form = QuantityForm()
        if not form.validate_on_submit():
            flash("Choose a quantity from 1 to 50, or remove the product.", "error")
        elif not board.is_for_sale or form.quantity.data > board.stock:
            flash("That quantity is no longer available. Reduce it or remove the product.", "error")
        else:
            cart[str(board_id)] = form.quantity.data
            session["cart"] = cart
            flash("Cart updated.", "success")
        return redirect(url_for("view_cart"))

    @app.post("/cart/remove/<int:board_id>")
    def remove_from_cart(board_id):
        cart = dict(session.get("cart", {}))
        if str(board_id) not in cart:
            abort(404, description="This product is not in your cart.")
        del cart[str(board_id)]
        session["cart"] = cart
        flash("Product removed from your cart.", "success")
        return redirect(url_for("view_cart"))

    def checkout_complete(order):
        if session.get("checkout_key") == order.checkout_key:
            session.pop("cart", None)
            session.pop("checkout_key", None)
        return redirect(url_for("order_detail", order_id=order.id))

    @app.route("/checkout", methods=["GET", "POST"])
    @login_required
    def checkout():
        form = CheckoutForm()
        if request.method == "POST" and form.checkout_key.data:
            existing = Order.query.filter_by(
                checkout_key=form.checkout_key.data, user_id=current_user.id
            ).first()
            if existing:
                return checkout_complete(existing)
        entries = cart_entries()
        if not entries:
            flash("Add an STM32 device to your cart before checking out.", "info")
            return redirect(url_for("view_cart"))
        if not all(item["available"] for item in entries):
            flash("Stock has changed. Please review your cart before checking out.", "error")
            return redirect(url_for("view_cart"))
        if request.method == "GET":
            session.setdefault("checkout_key", secrets.token_hex(16))
            form.checkout_key.data = session["checkout_key"]
            form.email.data = current_user.email
        if form.validate_on_submit():
            if not secrets.compare_digest(
                form.checkout_key.data, session.get("checkout_key", "")
            ):
                abort(400, description="This checkout form has expired. Reload checkout.")
            total = sum(item["total_cents"] for item in entries)
            order = Order(
                reference=f"STM-{uuid.uuid4().hex[:12].upper()}",
                checkout_key=form.checkout_key.data, user_id=current_user.id,
                full_name=form.full_name.data, email=form.email.data.lower(),
                address=form.address.data, city=form.city.data,
                postal_code=form.postal_code.data, country=form.country.data,
                total_cents=total,
            )
            try:
                for entry in entries:
                    board, quantity = entry["board"], entry["quantity"]
                    # A conditional UPDATE checks stock and price again under the write lock.
                    result = db.session.execute(
                        update(Board).where(
                            Board.id == board.id, *catalog_filters(),
                            Board.stock >= quantity,
                            Board.price_cents == board.price_cents,
                        ).values(stock=Board.stock - quantity),
                        execution_options={"synchronize_session": False},
                    )
                    if result.rowcount != 1:
                        db.session.rollback()
                        flash("Stock or prices just changed. Review your cart and try again.", "error")
                        return redirect(url_for("view_cart"))
                    order.items.append(OrderItem(
                        board_id=board.id, product_name=board.name,
                        product_mcu=board.mcu, quantity=quantity,
                        unit_price_cents=board.price_cents,
                    ))
                db.session.add(order)
                db.session.commit()
            except SQLAlchemyError as error:
                db.session.rollback()
                if isinstance(error, IntegrityError):
                    existing = Order.query.filter_by(
                        checkout_key=form.checkout_key.data, user_id=current_user.id
                    ).first()
                    if existing:
                        return checkout_complete(existing)
                app.logger.exception("Could not place the order")
                flash("Your order could not be saved. Nothing was ordered; please try again.", "error")
                return redirect(url_for("view_cart"))
            flash("Demo order placed. No payment has been taken.", "success")
            return checkout_complete(order)
        return render_template(
            "checkout.html", form=form, entries=entries,
            total_cents=sum(item["total_cents"] for item in entries),
        )

    @app.get("/orders")
    @login_required
    def my_orders():
        orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
        return render_template("orders.html", orders=orders)

    def accessible_order(order_id):
        order = db.get_or_404(Order, order_id)
        if order.user_id != current_user.id and not current_user.is_admin:
            abort(403)
        return order

    @app.get("/orders/<int:order_id>")
    @login_required
    def order_detail(order_id):
        return render_template("order_detail.html", order=accessible_order(order_id))

    @app.post("/admin/orders/<int:order_id>/status")
    @admin_required
    def update_order_status(order_id):
        order = db.get_or_404(Order, order_id)
        status = request.form.get("status", "")
        if status not in ORDER_STATUSES:
            abort(400, description="Choose a valid order status.")
        if ORDER_STATUSES.index(status) < ORDER_STATUSES.index(order.status):
            flash("Orders cannot move back to an earlier status.", "error")
        else:
            order.status = status
            db.session.commit()
            flash("Order status updated.", "success")
        return redirect(url_for("order_detail", order_id=order.id))

    @app.post("/boards/<int:board_id>/firmware")
    @login_required
    def upload_firmware(board_id):
        board = db.get_or_404(Board, board_id)
        if not board.is_for_sale:
            abort(404)
        upload = request.files.get("firmware")
        label = request.form.get("label", "").strip()
        if not label or len(label) > 100:
            flash("Enter a firmware label of up to 100 characters.", "error")
        elif upload is None or not upload.filename:
            flash("Choose a firmware file to upload.", "error")
        else:
            original_name = upload.filename.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            extension = Path(original_name).suffix.lower()
            if extension not in {".bin", ".elf", ".hex", ".uf2"}:
                flash("Use a .bin, .elf, .hex, or .uf2 firmware file.", "error")
            else:
                stored_name = None
                try:
                    stored_name = firmware_files.save(
                        upload, name=f"{uuid.uuid4().hex}{extension}"
                    )
                    db.session.add(Firmware(
                        board=board, user_id=current_user.id, label=label,
                        original_name=original_name[:180], stored_name=stored_name,
                    ))
                    db.session.commit()
                except UploadNotAllowed:
                    flash("This firmware file type is not allowed.", "error")
                except (OSError, SQLAlchemyError):
                    db.session.rollback()
                    app.logger.exception("Could not save firmware")
                    if stored_name:
                        try:
                            (Path(app.config["UPLOADED_FIRMWARE_DEST"]) / stored_name).unlink(missing_ok=True)
                        except OSError:
                            app.logger.exception("Could not clean up unsaved firmware")
                    flash("The file could not be saved. Please try again.", "error")
                else:
                    flash("Firmware uploaded to your private files.", "success")
        return redirect(url_for("board_detail", board_id=board.id))

    @app.get("/firmware/<int:firmware_id>/download")
    @login_required
    def download_firmware(firmware_id):
        firmware = db.get_or_404(Firmware, firmware_id)
        if firmware.user_id != current_user.id and not current_user.is_admin:
            abort(403)
        return send_from_directory(
            app.config["UPLOADED_FIRMWARE_DEST"], firmware.stored_name,
            as_attachment=True, download_name=firmware.original_name,
        )

    @app.get("/api/boards")
    @app.get("/api/products")
    def api_boards():
        boards = Board.query.filter(*catalog_filters()).order_by(Board.name).all()
        return jsonify(items=[board_to_dict(board) for board in boards], count=len(boards), currency=CURRENCY)

    @app.get("/api/boards/<int:board_id>")
    @app.get("/api/products/<int:board_id>")
    def api_board(board_id):
        board = db.get_or_404(Board, board_id)
        if not board.is_for_sale:
            abort(404)
        return jsonify(board_to_dict(board))

    @app.post("/api/boards")
    @app.post("/api/products")
    @admin_required
    def api_create_board():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="A JSON object is required."), 400
        board, error = board_from_payload(payload)
        if error:
            return jsonify(error=error), 400
        db.session.add(board)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify(error="A product with that name already exists."), 409
        return jsonify(board_to_dict(board)), 201

    @app.patch("/api/boards/<int:board_id>")
    @app.patch("/api/products/<int:board_id>")
    @admin_required
    def api_update_board(board_id):
        board = db.get_or_404(Board, board_id)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="A JSON object is required."), 400
        _, error = board_from_payload(payload, board)
        if error:
            return jsonify(error=error), 400
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify(error="A product with that name already exists."), 409
        return jsonify(board_to_dict(board))

    @app.delete("/api/boards/<int:board_id>")
    @app.delete("/api/products/<int:board_id>")
    @admin_required
    def api_delete_board(board_id):
        board = db.get_or_404(Board, board_id)
        board.is_listed = False
        db.session.commit()
        return "", 204

    @app.get("/api/csrf")
    @login_required
    def api_csrf():
        return jsonify(csrf_token=generate_csrf())

    @app.get("/api/orders")
    @login_required
    def api_orders():
        orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
        return jsonify(items=[order_to_dict(order) for order in orders], count=len(orders))

    @app.get("/api/orders/<int:order_id>")
    @login_required
    def api_order(order_id):
        return jsonify(order_to_dict(accessible_order(order_id)))

    @app.cli.command("create-admin")
    def create_admin():
        """Create a store administrator interactively."""
        form = RegisterForm(meta={"csrf": False})
        form.username.data = click.prompt("Username").strip()
        form.email.data = click.prompt("Email").strip().lower()
        form.password.data = click.prompt(
            "Password", hide_input=True, confirmation_prompt=True
        )
        if not form.validate():
            raise click.ClickException("; ".join(
                f"{field}: {', '.join(errors)}" for field, errors in form.errors.items()
            ))
        user = User(username=form.username.data, email=form.email.data, role="admin")
        user.set_password(form.password.data)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError as error:
            db.session.rollback()
            raise click.ClickException("That username or email is already registered.") from error
        click.echo(f"Store administrator created for {user.username}.")

    with app.app_context():
        init_database()
    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes"})
