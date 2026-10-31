import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app
from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from werkzeug.security import check_password_hash, generate_password_hash


db = SQLAlchemy()
ORDER_STATUSES = ("pending", "processing", "shipped")


def utc_now():
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(40), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="member")
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    @property
    def is_admin(self):
        return self.role == "admin"

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __str__(self):
        return self.username


class Board(db.Model):
    # Keep the original table so existing accounts and firmware links still work.
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    manufacturer = db.Column(
        db.String(80), nullable=False, default="STMicroelectronics"
    )
    mcu = db.Column(db.String(80), nullable=False)
    architecture = db.Column(db.String(40), nullable=False)
    cores = db.Column(db.Integer, nullable=False, default=1)
    clock_mhz = db.Column(db.Float, nullable=False)
    flash_kb = db.Column(db.Integer, nullable=False)
    stock = db.Column(db.Integer, nullable=False, default=0)
    price_cents = db.Column(db.Integer, nullable=False, default=0)
    is_listed = db.Column(db.Boolean, nullable=False, default=True)
    description = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    firmware = db.relationship(
        "Firmware",
        backref="board",
        cascade="all, delete-orphan",
        order_by="Firmware.created_at.desc()",
    )
    __table_args__ = (
        db.CheckConstraint("stock >= 0", name="nonnegative_stock"),
        db.CheckConstraint("price_cents >= 0", name="nonnegative_price"),
    )

    @property
    def family(self):
        return self.mcu[:7].upper()

    @property
    def is_for_sale(self):
        return (
            self.is_listed
            and self.price_cents > 0
            and self.mcu.upper().startswith("STM32")
            and self.manufacturer == "STMicroelectronics"
        )

    def __str__(self):
        return self.name


class Firmware(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    board_id = db.Column(db.Integer, db.ForeignKey("board.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    label = db.Column(db.String(100), nullable=False)
    original_name = db.Column(db.String(180), nullable=False)
    stored_name = db.Column(db.String(180), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    uploader = db.relationship("User", backref="firmware_uploads")


class Order(db.Model):
    __tablename__ = "customer_order"

    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(24), unique=True, nullable=False)
    checkout_key = db.Column(db.String(64), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    full_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    address = db.Column(db.String(400), nullable=False)
    city = db.Column(db.String(80), nullable=False)
    postal_code = db.Column(db.String(20), nullable=False)
    country = db.Column(db.String(80), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    total_cents = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    customer = db.relationship("User", backref="orders")
    items = db.relationship(
        "OrderItem", backref="order", cascade="all, delete-orphan", order_by="OrderItem.id"
    )
    __table_args__ = (
        db.CheckConstraint(
            "status IN ('pending', 'processing', 'shipped')", name="valid_order_status"
        ),
        db.CheckConstraint("total_cents > 0", name="positive_order_total"),
    )

    def __str__(self):
        return self.reference


class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(
        db.Integer, db.ForeignKey("customer_order.id"), nullable=False
    )
    board_id = db.Column(db.Integer, db.ForeignKey("board.id"), nullable=False)
    product_name = db.Column(db.String(100), nullable=False)
    product_mcu = db.Column(db.String(80), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price_cents = db.Column(db.Integer, nullable=False)
    __table_args__ = (
        db.CheckConstraint("quantity > 0", name="positive_order_quantity"),
        db.CheckConstraint("unit_price_cents > 0", name="positive_item_price"),
    )

    @property
    def total_cents(self):
        return self.quantity * self.unit_price_cents


DEMO_PRODUCTS = [
    dict(
        name="NUCLEO-F446RE", mcu="STM32F446RET6", architecture="Arm Cortex-M4",
        cores=1, clock_mhz=180, flash_kb=512, stock=12, price_cents=2500,
        description="An everyday development board for motor control, sensors, and "
        "embedded prototypes. Includes an on-board ST-LINK debugger.",
    ),
    dict(
        name="NUCLEO-H743ZI2", mcu="STM32H743ZIT6", architecture="Arm Cortex-M7",
        cores=1, clock_mhz=480, flash_kb=2048, stock=6, price_cents=4200,
        description="More headroom for demanding applications. A high-performance "
        "STM32H7 Nucleo-144 board with on-board ST-LINK.",
    ),
    dict(
        name="NUCLEO-L476RG", mcu="STM32L476RGT6", architecture="Arm Cortex-M4",
        cores=1, clock_mhz=80, flash_kb=1024, stock=9, price_cents=2400,
        description="Build battery-conscious devices with the ultra-low-power "
        "STM32L4 family and familiar Nucleo expansion connectors.",
    ),
    dict(
        name="NUCLEO-G071RB", mcu="STM32G071RBT6", architecture="Arm Cortex-M0+",
        cores=1, clock_mhz=64, flash_kb=128, stock=15, price_cents=1800,
        description="A practical entry point for learning STM32, connecting "
        "peripherals, and building cost-conscious embedded systems.",
    ),
    dict(
        name="STM32F407G-DISC1", mcu="STM32F407VGT6", architecture="Arm Cortex-M4",
        cores=1, clock_mhz=168, flash_kb=1024, stock=0, price_cents=3500,
        description="Explore the STM32F4 platform with a Discovery board, on-board "
        "motion sensing, audio features, and an integrated debugger.",
    ),
    dict(
        name="NUCLEO-U575ZI-Q", mcu="STM32U575ZIT6Q", architecture="Arm Cortex-M33",
        cores=1, clock_mhz=160, flash_kb=2048, stock=4, price_cents=3900,
        description="An ultra-low-power STM32U5 development platform with a "
        "Cortex-M33 core for your next connected-device prototype.",
    ),
]


def init_database():
    inspector = inspect(db.engine)
    board_exists = inspector.has_table("board")
    columns = (
        {column["name"] for column in inspector.get_columns("board")}
        if board_exists else set()
    )
    upgrading = board_exists and not {"price_cents", "is_listed"} <= columns
    if upgrading:
        if db.engine.dialect.name != "sqlite":
            raise RuntimeError("The legacy database upgrade requires SQLite.")
        database_path = db.engine.url.database
        if database_path and database_path != ":memory:":
            source = Path(database_path)
            backup = source.with_name(f"{source.stem}.before-store{source.suffix}")
            if not backup.exists():
                with closing(sqlite3.connect(source)) as old_db, closing(sqlite3.connect(backup)) as copy:
                    old_db.backup(copy)
                current_app.logger.info("Legacy database backed up to %s", backup)
        with db.engine.begin() as connection:
            if "price_cents" not in columns:
                connection.execute(text(
                    "ALTER TABLE board ADD COLUMN price_cents INTEGER NOT NULL DEFAULT 0"
                ))
            if "is_listed" not in columns:
                connection.execute(text(
                    "ALTER TABLE board ADD COLUMN is_listed BOOLEAN NOT NULL DEFAULT 0"
                ))

    db.create_all()
    if not board_exists or upgrading:
        for sample in DEMO_PRODUCTS:
            board = Board.query.filter_by(name=sample["name"]).first()
            if board is None:
                db.session.add(Board(**sample))
            elif board.mcu.upper().startswith("STM32"):
                if not board.price_cents:
                    board.price_cents = sample["price_cents"]
                board.is_listed = True
        db.session.commit()
