import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, List, Tuple

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    CallbackQuery,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

load_dotenv()

# =========================
# الإعدادات
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@da7lan").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/da7lan").strip()
BRAND_NAME = os.getenv("BRAND_NAME", "Shabakati").strip()
DB_PATH = os.getenv("DB_PATH", "bot_data.db").strip()

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN غير موجود داخل ملف .env")

if not OWNER_ID_RAW.isdigit():
    raise ValueError("OWNER_ID يجب أن يكون Telegram ID رقمي")

OWNER_ID = int(OWNER_ID_RAW)

DEFAULT_NETWORKS = [
    ("مقداد نت", "meqdad_net"),
    ("شبكة زين", "zain_net"),
]

DEFAULT_PACKAGES = [
    ("كرت 12 ساعة", 20),
    ("كرت 3 ساعات", 45),
    ("كرت يوم", 100),
    ("كرت أسبوع", 300),
]

MIN_QTY = 1
MAX_QTY = 5

UPLOAD_CACHE: dict = {}
ADMIN_CACHE: dict = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# =========================
# دالة مساعدة لعرض اسم المشتري
# =========================
def get_buyer_display(order) -> str:
    """يجيب اسم المشتري بأفضل طريقة متاحة."""
    if order["buyer_username"]:
        return f"@{order['buyer_username']}"
    if order["buyer_first_name"]:
        name = order["buyer_first_name"]
        if order["buyer_last_name"]:
            name += f" {order['buyer_last_name']}"
        return name
    return f"مستخدم #{order['buyer_id']}"


def get_user_display(user) -> str:
    """يجيب اسم المستخدم من كائن تيليجرام."""
    if user.username:
        return f"@{user.username}"
    if user.first_name:
        name = user.first_name
        if user.last_name:
            name += f" {user.last_name}"
        return name
    return f"مستخدم #{user.id}"


# =========================
# قاعدة البيانات
# =========================
class Database:
    def __init__(self, path: str):
        self.path = path
        self.init_db()

    @contextmanager
    def get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self):
        with self.get_conn() as conn:
            cur = conn.cursor()

            cur.execute("""
            CREATE TABLE IF NOT EXISTS networks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                identifier TEXT NOT NULL UNIQUE,
                image_file_id TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                price INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                network_id INTEGER NOT NULL,
                package_id INTEGER NOT NULL,
                card_info TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('available', 'sold')) DEFAULT 'available',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (network_id) REFERENCES networks(id),
                FOREIGN KEY (package_id) REFERENCES packages(id),
                UNIQUE(network_id, package_id, card_info)
            )
            """)

            # ✅ جدول الطلبات مع إضافة buyer_first_name و buyer_last_name
            cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                network_id INTEGER NOT NULL,
                buyer_id INTEGER NOT NULL,
                buyer_username TEXT,
                buyer_first_name TEXT,
                buyer_last_name TEXT,
                payment_method TEXT NOT NULL,
                price INTEGER NOT NULL,
                package_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                card_ids TEXT,
                telegram_payment_charge_id TEXT,
                provider_payment_charge_id TEXT,
                status TEXT NOT NULL DEFAULT 'completed',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT,
                FOREIGN KEY (network_id) REFERENCES networks(id),
                FOREIGN KEY (package_id) REFERENCES packages(id)
            )
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                network_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (network_id) REFERENCES networks(id)
            )
            """)

            # ✅ Migration: أضف الأعمدة الجديدة لو ما كانت موجودة
            existing_cols = [
                row[1] for row in cur.execute("PRAGMA table_info(orders)").fetchall()
            ]
            if "buyer_first_name" not in existing_cols:
                cur.execute("ALTER TABLE orders ADD COLUMN buyer_first_name TEXT")
            if "buyer_last_name" not in existing_cols:
                cur.execute("ALTER TABLE orders ADD COLUMN buyer_last_name TEXT")

            for net_name, net_identifier in DEFAULT_NETWORKS:
                cur.execute("SELECT id FROM networks WHERE identifier = ?", (net_identifier,))
                if cur.fetchone() is None:
                    cur.execute(
                        "INSERT INTO networks (name, identifier, is_active) VALUES (?, ?, 1)",
                        (net_name, net_identifier)
                    )

            for pkg_name, pkg_price in DEFAULT_PACKAGES:
                cur.execute("SELECT id FROM packages WHERE name = ?", (pkg_name,))
                if cur.fetchone() is None:
                    cur.execute(
                        "INSERT INTO packages (name, price, is_active) VALUES (?, ?, 1)",
                        (pkg_name, pkg_price)
                    )

    # ========= الشبكات =========
    def add_network(self, name: str, identifier: str) -> bool:
        try:
            with self.get_conn() as conn:
                conn.execute(
                    "INSERT INTO networks (name, identifier, is_active) VALUES (?, ?, 1)",
                    (name.strip(), identifier.strip())
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def list_networks(self, active_only: bool = False):
        with self.get_conn() as conn:
            query = "SELECT * FROM networks WHERE is_active = 1 ORDER BY id ASC" if active_only \
                else "SELECT * FROM networks ORDER BY id ASC"
            return conn.execute(query).fetchall()

    def get_network_by_id(self, network_id: int):
        with self.get_conn() as conn:
            return conn.execute("SELECT * FROM networks WHERE id = ?", (network_id,)).fetchone()

    def set_network_image(self, network_id: int, image_file_id: str):
        with self.get_conn() as conn:
            conn.execute(
                "UPDATE networks SET image_file_id = ? WHERE id = ?",
                (image_file_id, network_id)
            )

    def remove_network_image(self, network_id: int):
        with self.get_conn() as conn:
            conn.execute("UPDATE networks SET image_file_id = NULL WHERE id = ?", (network_id,))

    def toggle_network_status(self, network_id: int):
        with self.get_conn() as conn:
            conn.execute("""
                UPDATE networks
                SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END
                WHERE id = ?
            """, (network_id,))

    def delete_network(self, network_id: int) -> Tuple[bool, str]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")

            cur.execute("SELECT * FROM networks WHERE id = ?", (network_id,))
            network = cur.fetchone()
            if not network:
                conn.rollback()
                return False, "الشبكة غير موجودة في قاعدة البيانات."

            network_name = network["name"]

            cur.execute(
                "SELECT COUNT(*) AS cnt FROM orders WHERE network_id = ?",
                (network_id,)
            )
            orders_count = cur.fetchone()["cnt"]
            if orders_count > 0:
                conn.rollback()
                return (
                    False,
                    f"⚠️ لا يمكن حذف الشبكة <b>{network_name}</b>.\n"
                    f"يوجد <b>{orders_count}</b> طلب مرتبط بها في سجل الطلبات.\n"
                    "احذف الطلبات المرتبطة أولاً أو عطّل الشبكة بدلاً من حذفها."
                )

            cur.execute(
                "SELECT COUNT(*) AS cnt FROM cards WHERE network_id = ?",
                (network_id,)
            )
            cards_count = cur.fetchone()["cnt"]

            cur.execute("DELETE FROM cards WHERE network_id = ?", (network_id,))
            cur.execute("DELETE FROM networks WHERE id = ?", (network_id,))

            conn.commit()
            return (
                True,
                f"✅ تم حذف الشبكة <b>{network_name}</b> نهائياً.\n"
                f"🗑️ تم حذف <b>{cards_count}</b> كرت مرتبط بها."
            )
        except Exception as e:
            conn.rollback()
            logger.exception("delete_network error")
            return False, f"حدث خطأ غير متوقع أثناء الحذف: {str(e)}"
        finally:
            conn.close()

    # ========= الباقات =========
    def add_package(self, name: str, price: int) -> bool:
        try:
            with self.get_conn() as conn:
                conn.execute(
                    "INSERT INTO packages (name, price, is_active) VALUES (?, ?, 1)",
                    (name.strip(), price)
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def list_packages(self, active_only: bool = False):
        with self.get_conn() as conn:
            query = "SELECT * FROM packages WHERE is_active = 1 ORDER BY id ASC" if active_only \
                else "SELECT * FROM packages ORDER BY id ASC"
            return conn.execute(query).fetchall()

    def get_package_by_id(self, package_id: int):
        with self.get_conn() as conn:
            return conn.execute("SELECT * FROM packages WHERE id = ?", (package_id,)).fetchone()

    def update_package_price(self, package_id: int, price: int):
        with self.get_conn() as conn:
            conn.execute("UPDATE packages SET price = ? WHERE id = ?", (price, package_id))

    def toggle_package_status(self, package_id: int):
        with self.get_conn() as conn:
            conn.execute("""
                UPDATE packages
                SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END
                WHERE id = ?
            """, (package_id,))

    def delete_package(self, package_id: int) -> Tuple[bool, str]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")

            cur.execute("SELECT * FROM packages WHERE id = ?", (package_id,))
            package = cur.fetchone()
            if not package:
                conn.rollback()
                return False, "الباقة غير موجودة في قاعدة البيانات."

            pkg_name = package["name"]

            cur.execute(
                "SELECT COUNT(*) AS cnt FROM orders WHERE package_id = ?",
                (package_id,)
            )
            orders_count = cur.fetchone()["cnt"]
            if orders_count > 0:
                conn.rollback()
                return (
                    False,
                    f"⚠️ لا يمكن حذف الباقة <b>{pkg_name}</b>.\n"
                    f"يوجد <b>{orders_count}</b> طلب مرتبط بها في سجل الطلبات.\n"
                    "عطّل الباقة بدلاً من حذفها للحفاظ على سجل الطلبات."
                )

            cur.execute(
                "SELECT COUNT(*) AS cnt FROM cards WHERE package_id = ?",
                (package_id,)
            )
            cards_count = cur.fetchone()["cnt"]

            cur.execute("DELETE FROM cards WHERE package_id = ?", (package_id,))
            cur.execute("DELETE FROM packages WHERE id = ?", (package_id,))

            conn.commit()
            return (
                True,
                f"✅ تم حذف الباقة <b>{pkg_name}</b> نهائياً.\n"
                f"🗑️ تم حذف <b>{cards_count}</b> كرت مرتبط بها."
            )
        except Exception as e:
            conn.rollback()
            logger.exception("delete_package error")
            return False, f"حدث خطأ غير متوقع أثناء الحذف: {str(e)}"
        finally:
            conn.close()

    # ========= المستخدمون =========
    def upsert_user(self, user_id: int, username: Optional[str], full_name: str, network_id: Optional[int]):
        with self.get_conn() as conn:
            conn.execute("""
            INSERT INTO users (user_id, username, full_name, network_id, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                network_id=COALESCE(excluded.network_id, users.network_id),
                updated_at=CURRENT_TIMESTAMP
            """, (user_id, username, full_name, network_id))

    def update_user_network(self, user_id: int, network_id: int):
        with self.get_conn() as conn:
            conn.execute("""
            INSERT INTO users (user_id, network_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                network_id=excluded.network_id,
                updated_at=CURRENT_TIMESTAMP
            """, (user_id, network_id))

    def get_user_network_id(self, user_id: int) -> Optional[int]:
        with self.get_conn() as conn:
            row = conn.execute("SELECT network_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return row["network_id"] if row and row["network_id"] else None

    # ========= الكروت =========
    def add_cards_bulk(self, network_id: int, package_id: int, cards: List[str]) -> Tuple[int, int]:
        clean_cards = [c.strip() for c in cards if c.strip()]
        if not clean_cards:
            return 0, 0

        inserted = 0
        duplicates = 0

        with self.get_conn() as conn:
            for card in clean_cards:
                try:
                    conn.execute("""
                        INSERT INTO cards (network_id, package_id, card_info, status)
                        VALUES (?, ?, ?, 'available')
                    """, (network_id, package_id, card))
                    inserted += 1
                except sqlite3.IntegrityError:
                    duplicates += 1

        return inserted, duplicates

    def count_available_cards(self, network_id: int, package_id: int) -> int:
        with self.get_conn() as conn:
            row = conn.execute("""
                SELECT COUNT(*) AS cnt FROM cards
                WHERE network_id = ? AND package_id = ? AND status = 'available'
            """, (network_id, package_id)).fetchone()
            return row["cnt"] if row else 0

    def get_available_packages_for_network(self, network_id: int):
        with self.get_conn() as conn:
            return conn.execute("""
                SELECT p.id, p.name, p.price, p.is_active, COUNT(c.id) AS available_count
                FROM packages p
                LEFT JOIN cards c ON c.package_id = p.id
                    AND c.network_id = ? AND c.status = 'available'
                WHERE p.is_active = 1
                GROUP BY p.id, p.name, p.price, p.is_active
                HAVING available_count > 0
                ORDER BY p.id ASC
            """, (network_id,)).fetchall()

    def sell_available_cards_bulk(
        self,
        network_id: int,
        package_id: int,
        quantity: int,
        buyer_id: int,
        buyer_username: Optional[str],
        buyer_first_name: Optional[str],
        buyer_last_name: Optional[str],
        price: int,
        telegram_payment_charge_id: str,
        provider_payment_charge_id: str,
    ) -> List[sqlite3.Row]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")

            cur.execute("""
                SELECT * FROM cards
                WHERE network_id = ? AND package_id = ? AND status = 'available'
                ORDER BY id ASC LIMIT ?
            """, (network_id, package_id, quantity))
            cards = cur.fetchall()

            if len(cards) < quantity:
                conn.rollback()
                return []

            for card in cards:
                cur.execute(
                    "UPDATE cards SET status = 'sold' WHERE id = ? AND status = 'available'",
                    (card["id"],)
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return []

            card_ids = ",".join(str(c["id"]) for c in cards)
            cur.execute("""
                INSERT INTO orders (
                    network_id, buyer_id, buyer_username, buyer_first_name, buyer_last_name,
                    payment_method, price, package_id, quantity, card_ids,
                    telegram_payment_charge_id, provider_payment_charge_id,
                    status, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, 'stars', ?, ?, ?, ?, ?, ?, 'completed', ?, ?)
            """, (
                network_id, buyer_id, buyer_username, buyer_first_name, buyer_last_name,
                price, package_id, quantity, card_ids,
                telegram_payment_charge_id, provider_payment_charge_id,
                datetime.utcnow().isoformat(), datetime.utcnow().isoformat(),
            ))

            conn.commit()
            return cards
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ========= الطلبات البنكية =========
    def create_bank_order(
        self,
        network_id: int,
        buyer_id: int,
        buyer_username: Optional[str],
        buyer_first_name: Optional[str],
        buyer_last_name: Optional[str],
        package_id: int,
        quantity: int,
        price: int,
    ) -> int:
        with self.get_conn() as conn:
            cur = conn.execute("""
                INSERT INTO orders (
                    network_id, buyer_id, buyer_username, buyer_first_name, buyer_last_name,
                    payment_method, price, package_id, quantity,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'bank', ?, ?, ?, 'pending_bank_transfer', ?)
            """, (
                network_id, buyer_id, buyer_username, buyer_first_name, buyer_last_name,
                price, package_id, quantity, datetime.utcnow().isoformat(),
            ))
            return cur.lastrowid

    def list_pending_bank_orders(self):
        with self.get_conn() as conn:
            return conn.execute("""
                SELECT o.*, n.name AS network_name, p.name AS package_name
                FROM orders o
                JOIN networks n ON n.id = o.network_id
                JOIN packages p ON p.id = o.package_id
                WHERE o.payment_method = 'bank' AND o.status = 'pending_bank_transfer'
                ORDER BY o.id DESC
            """).fetchall()

    def get_order_by_id(self, order_id: int):
        with self.get_conn() as conn:
            return conn.execute("""
                SELECT o.*, n.name AS network_name, p.name AS package_name, p.price AS package_price
                FROM orders o
                JOIN networks n ON n.id = o.network_id
                JOIN packages p ON p.id = o.package_id
                WHERE o.id = ?
            """, (order_id,)).fetchone()

    def complete_bank_order(self, order_id: int) -> List[sqlite3.Row]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")

            cur.execute("""
                SELECT * FROM orders
                WHERE id = ? AND payment_method = 'bank' AND status = 'pending_bank_transfer'
            """, (order_id,))
            order = cur.fetchone()
            if not order:
                conn.rollback()
                return []

            cur.execute("""
                SELECT * FROM cards
                WHERE network_id = ? AND package_id = ? AND status = 'available'
                ORDER BY id ASC LIMIT ?
            """, (order["network_id"], order["package_id"], order["quantity"]))
            cards = cur.fetchall()

            if len(cards) < order["quantity"]:
                conn.rollback()
                return []

            for card in cards:
                cur.execute(
                    "UPDATE cards SET status = 'sold' WHERE id = ? AND status = 'available'",
                    (card["id"],)
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return []

            cur.execute("""
                UPDATE orders SET status = 'completed', card_ids = ?, completed_at = ?
                WHERE id = ?
            """, (",".join(str(c["id"]) for c in cards), datetime.utcnow().isoformat(), order_id))

            conn.commit()
            return cards
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def cancel_bank_order(self, order_id: int) -> bool:
        with self.get_conn() as conn:
            cur = conn.execute("""
                UPDATE orders SET status = 'cancelled'
                WHERE id = ? AND payment_method = 'bank' AND status = 'pending_bank_transfer'
            """, (order_id,))
            return cur.rowcount > 0

    # ========= الإحصائيات =========
    def get_stats(self) -> dict:
        with self.get_conn() as conn:
            def count(q, *args):
                return conn.execute(q, args).fetchone()[0]

            return {
                "active_networks": count("SELECT COUNT(*) FROM networks WHERE is_active = 1"),
                "active_packages": count("SELECT COUNT(*) FROM packages WHERE is_active = 1"),
                "available_cards": count("SELECT COUNT(*) FROM cards WHERE status = 'available'"),
                "sold_cards": count("SELECT COUNT(*) FROM cards WHERE status = 'sold'"),
                "completed_orders": count("SELECT COUNT(*) FROM orders WHERE status = 'completed'"),
                "sales_total": count("SELECT COALESCE(SUM(price),0) FROM orders WHERE status='completed'"),
                "pending_bank_orders": count("SELECT COUNT(*) FROM orders WHERE status='pending_bank_transfer'"),
            }


db = Database(DB_PATH)


# =========================
# أدوات مساعدة
# =========================
def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def parse_card_info(card_info: str) -> Tuple[str, str]:
    if ":" in card_info:
        parts = card_info.split(":", 1)
        return parts[0].strip() or "غير محدد", parts[1].strip() or "غير محددة"
    return card_info.strip() or "غير محدد", "غير محددة"


def format_cards_message(network_name: str, package_name: str, cards: List[sqlite3.Row]) -> str:
    lines = [
        "✅ <b>تمت العملية بنجاح</b>",
        "━━━━━━━━━━━━━━",
        f"🌐 <b>الشبكة:</b> {network_name}",
        f"📦 <b>الباقة:</b> {package_name}",
        f"🔢 <b>الكمية:</b> {len(cards)}",
        "━━━━━━━━━━━━━━",
        "",
    ]
    for idx, card in enumerate(cards, start=1):
        username, password = parse_card_info(card["card_info"])
        lines += [
            f"🎫 <b>الكرت {idx}</b>",
            f"👤 <b>المستخدم:</b> <code>{username}</code>",
            f"🔑 <b>كلمة المرور:</b> <code>{password}</code>",
            "",
        ]
    lines += [
        "━━━━━━━━━━━━━━",
        "💡 اضغط على البيانات لنسخها.",
        f"📞 <b>المشرف:</b> {SUPPORT_USERNAME}",
        f"شكراً لاستخدامك <b>{BRAND_NAME}</b> 💙",
    ]
    return "\n".join(lines)


# =========================
# لوحات المفاتيح
# =========================
def get_networks_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🌐 {n['name']}", callback_data=f"select_network:{n['id']}")]
        for n in db.list_networks(active_only=True)
    ]
    rows.append([InlineKeyboardButton("ℹ️ مساعدة", callback_data="show_help")])
    return InlineKeyboardMarkup(rows)


def get_packages_keyboard(network_id: int) -> InlineKeyboardMarkup:
    rows = []
    for pkg in db.get_available_packages_for_network(network_id):
        rows.append([
            InlineKeyboardButton(f"🛒 {pkg['name']}", callback_data=f"choose_package:{pkg['id']}"),
            InlineKeyboardButton(f"⭐ {pkg['price']}", callback_data=f"choose_package:{pkg['id']}"),
        ])
        rows.append([
            InlineKeyboardButton(f"📦 المتوفر: {pkg['available_count']}", callback_data=f"choose_package:{pkg['id']}")
        ])
    rows.append([InlineKeyboardButton("🔄 تحديث", callback_data="refresh_menu")])
    rows.append([InlineKeyboardButton("🌐 تغيير الشبكة", callback_data="change_network")])
    return InlineKeyboardMarkup(rows)


def get_quantity_keyboard(package_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1", callback_data=f"qty:{package_id}:1"),
            InlineKeyboardButton("2", callback_data=f"qty:{package_id}:2"),
            InlineKeyboardButton("3", callback_data=f"qty:{package_id}:3"),
        ],
        [
            InlineKeyboardButton("4", callback_data=f"qty:{package_id}:4"),
            InlineKeyboardButton("5", callback_data=f"qty:{package_id}:5"),
        ],
        [InlineKeyboardButton("⬅️ رجوع للباقات", callback_data="back_to_packages")],
        [InlineKeyboardButton("🌐 تغيير الشبكة", callback_data="change_network")],
    ])


def get_payment_methods_keyboard(package_id: int, qty: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ الدفع بنجوم تيليجرام", callback_data=f"pay_stars:{package_id}:{qty}")],
        [InlineKeyboardButton("🏦 التحويل البنكي", callback_data=f"pay_bank:{package_id}:{qty}")],
        [InlineKeyboardButton("🔢 تغيير الكمية", callback_data=f"change_qty:{package_id}")],
        [InlineKeyboardButton("⬅️ رجوع للباقات", callback_data="back_to_packages")],
        [InlineKeyboardButton("🌐 تغيير الشبكة", callback_data="change_network")],
    ])


def get_upload_networks_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"{'✅' if n['is_active'] else '⛔'} {n['name']}",
            callback_data=f"upload_net:{n['id']}"
        )]
        for n in db.list_networks()
    ]
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="admin_home")])
    return InlineKeyboardMarkup(rows)


def get_upload_packages_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"{'✅' if p['is_active'] else '⛔'} {p['name']} - {p['price']} ⭐",
            callback_data=f"upload_pkg:{p['id']}"
        )]
        for p in db.list_packages()
    ]
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="admin_home")])
    return InlineKeyboardMarkup(rows)


def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 رفع كروت من ملف", callback_data="admin_upload_cards")],
        [InlineKeyboardButton("🖼️ تعيين صورة شبكة", callback_data="admin_set_network_image")],
        [InlineKeyboardButton("🗑️ حذف صورة شبكة", callback_data="admin_remove_network_image")],
        [InlineKeyboardButton("📡 إدارة الشبكات", callback_data="admin_networks")],
        [InlineKeyboardButton("📦 إدارة الباقات", callback_data="admin_packages")],
        [InlineKeyboardButton("🏦 الطلبات البنكية", callback_data="admin_bank_orders")],
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="admin_stats")],
    ])


def get_network_image_select_keyboard(action_prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🌐 {n['name']}", callback_data=f"{action_prefix}:{n['id']}")]
        for n in db.list_networks()
    ]
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="admin_home")])
    return InlineKeyboardMarkup(rows)


def get_networks_manage_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for n in db.list_networks():
        status_icon = "✅" if n["is_active"] else "⛔"
        rows.append([
            InlineKeyboardButton(
                f"{status_icon} {n['name']}",
                callback_data=f"toggle_network:{n['id']}"
            ),
            InlineKeyboardButton(
                "🗑️ حذف",
                callback_data=f"delete_network:{n['id']}"
            ),
        ])
    rows.append([InlineKeyboardButton("➕ إضافة شبكة", callback_data="admin_add_network")])
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="admin_home")])
    return InlineKeyboardMarkup(rows)


def get_delete_confirm_keyboard(network_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⚠️ نعم، احذف نهائياً",
                callback_data=f"confirm_delete_network:{network_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ إلغاء",
                callback_data="admin_networks"
            )
        ],
    ])


def get_packages_manage_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for p in db.list_packages():
        status_icon = "✅" if p["is_active"] else "⛔"
        rows.append([
            InlineKeyboardButton(
                f"{status_icon} {p['name']} - {p['price']}⭐",
                callback_data=f"pkg_view:{p['id']}"
            ),
            InlineKeyboardButton(
                "🗑️ حذف",
                callback_data=f"delete_package:{p['id']}"
            ),
        ])
    rows.append([InlineKeyboardButton("➕ إضافة باقة", callback_data="admin_add_package")])
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="admin_home")])
    return InlineKeyboardMarkup(rows)


def get_package_actions_keyboard(package_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 تعديل السعر", callback_data=f"pkg_edit_price:{package_id}")],
        [InlineKeyboardButton("🔁 تفعيل/تعطيل", callback_data=f"pkg_toggle:{package_id}")],
        [InlineKeyboardButton("🗑️ حذف الباقة نهائياً", callback_data=f"delete_package:{package_id}")],
        [InlineKeyboardButton("⬅️ رجوع", callback_data="admin_packages")],
    ])


def get_delete_package_confirm_keyboard(package_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "⚠️ نعم، احذف نهائياً",
            callback_data=f"confirm_delete_package:{package_id}"
        )],
        [InlineKeyboardButton("⬅️ إلغاء", callback_data="admin_packages")],
    ])


def get_pending_orders_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"🧾 طلب #{o['id']} - {o['network_name']} / {o['package_name']}",
            callback_data=f"bank_order_view:{o['id']}"
        )]
        for o in db.list_pending_bank_orders()
    ]
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="admin_home")])
    return InlineKeyboardMarkup(rows)


def get_bank_order_actions_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأكيد وتسليم", callback_data=f"bank_order_complete:{order_id}")],
        [InlineKeyboardButton("❌ إلغاء الطلب", callback_data=f"bank_order_cancel:{order_id}")],
        [InlineKeyboardButton("⬅️ رجوع", callback_data="admin_bank_orders")],
    ])


# =========================
# نصوص الواجهة
# =========================
def get_welcome_text() -> str:
    return (
        f"✨ <b>أهلاً بك في {BRAND_NAME}</b>\n"
        "━━━━━━━━━━━━━━\n"
        "🎯 اختر الشبكة التي تريد الشراء منها\n"
        "🛒 ثم اختر الباقة والكمية المناسبة\n"
        "💳 وبعدها اختر طريقة الدفع\n"
        "━━━━━━━━━━━━━━\n"
        f"📞 <b>المشرف:</b> {SUPPORT_USERNAME}"
    )


def get_network_menu_text(network_name: str) -> str:
    return (
        f"🌐 <b>{network_name}</b>\n"
        "━━━━━━━━━━━━━━\n"
        "📦 اختر الباقة المناسبة لك\n"
        "⚡ الأسعار بالنجوم\n"
        "🔄 يمكنك تحديث القائمة في أي وقت\n"
        "━━━━━━━━━━━━━━\n"
        f"📞 <b>الدعم:</b> {SUPPORT_USERNAME}"
    )


def get_quantity_text(network_name: str, package_name: str, price: int, available: int) -> str:
    return (
        "🎯 <b>اختيار الكمية</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🌐 <b>الشبكة:</b> {network_name}\n"
        f"📦 <b>الباقة:</b> {package_name}\n"
        f"⭐ <b>سعر الوحدة:</b> {price}\n"
        f"📦 <b>المتوفر:</b> {available}\n"
        "━━━━━━━━━━━━━━\n"
        "اختر عدد الكروت المطلوبة:"
    )


def get_payment_choice_text(network_name: str, package_name: str, qty: int, total_price: int) -> str:
    return (
        "💳 <b>تفاصيل الطلب</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🌐 <b>الشبكة:</b> {network_name}\n"
        f"📦 <b>الباقة:</b> {package_name}\n"
        f"🔢 <b>الكمية:</b> {qty}\n"
        f"💰 <b>الإجمالي:</b> {total_price} ⭐\n"
        "━━━━━━━━━━━━━━\n"
        "اختر طريقة الدفع:"
    )


def get_admin_home_text() -> str:
    return (
        "👨‍💻 <b>لوحة تحكم المشرف</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        "📤 رفع كروت من ملف\n"
        "🖼️ تعيين/حذف صورة شبكة\n"
        "📡 إدارة الشبكات\n"
        "📦 إدارة الباقات\n"
        "🏦 إدارة الطلبات البنكية\n"
        "📊 إحصائيات"
    )


# =========================
# send_or_edit_network_menu
# =========================
async def send_or_edit_network_menu(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    network,
):
    text = get_network_menu_text(network["name"])
    keyboard = get_packages_keyboard(network["id"])
    image_file_id = network["image_file_id"]

    is_callback = isinstance(update_or_query, CallbackQuery)

    if image_file_id:
        if is_callback:
            try:
                await update_or_query.message.delete()
            except Exception:
                pass
            await context.bot.send_photo(
                chat_id=update_or_query.message.chat_id,
                photo=image_file_id,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        else:
            await update_or_query.message.reply_photo(
                photo=image_file_id,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
    else:
        if is_callback:
            await update_or_query.edit_message_text(
                text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
        else:
            await update_or_query.message.reply_text(
                text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )


# =========================
# أوامر المستخدم
# =========================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return

    db.upsert_user(
        user_id=user.id,
        username=user.username,
        full_name=user.full_name,
        network_id=db.get_user_network_id(user.id),
    )

    await update.message.reply_text(
        get_welcome_text(),
        reply_markup=get_networks_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(
        "📘 <b>طريقة الاستخدام</b>\n"
        "━━━━━━━━━━━━━━\n"
        "1) اختر الشبكة\n"
        "2) اختر الباقة\n"
        "3) اختر الكمية\n"
        "4) اختر طريقة الدفع\n"
        "5) الدفع بالنجوم → تسليم فوري\n"
        "━━━━━━━━━━━━━━\n"
        f"📞 <b>المشرف:</b> {SUPPORT_USERNAME}\n"
        "🏦 التحويل البنكي يتم تأكيده من المشرف.",
        parse_mode=ParseMode.HTML,
    )


# =========================
# Callbacks المستخدم
# =========================
async def show_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📘 <b>كيف تشتري؟</b>\n"
        "━━━━━━━━━━━━━━\n"
        "🌐 اختر الشبكة\n"
        "📦 اختر الباقة\n"
        "🔢 اختر الكمية\n"
        "💳 اختر طريقة الدفع\n"
        "✅ استلم الكرت فوراً\n"
        "━━━━━━━━━━━━━━\n"
        f"📞 <b>المشرف:</b> {SUPPORT_USERNAME}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع للشبكات", callback_data="change_network")]
        ]),
    )


async def select_network_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        network_id = int(query.data.split(":")[1])
        network = db.get_network_by_id(network_id)

        if not network or not network["is_active"]:
            await query.answer("هذه الشبكة غير متاحة حالياً", show_alert=True)
            return

        db.update_user_network(query.from_user.id, network_id)
        await send_or_edit_network_menu(query, context, network)

    except Exception:
        logger.exception("select_network_callback error")
        await query.answer("حدث خطأ، حاول مجدداً", show_alert=True)


async def choose_package_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        user_id = query.from_user.id
        network_id = db.get_user_network_id(user_id)
        if not network_id:
            await query.edit_message_text(
                get_welcome_text(),
                reply_markup=get_networks_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

        package_id = int(query.data.split(":")[1])
        package = db.get_package_by_id(package_id)
        network = db.get_network_by_id(network_id)

        if not package or not package["is_active"] or not network:
            await query.answer("حدث خطأ", show_alert=True)
            return

        available = db.count_available_cards(network_id, package_id)
        if available <= 0:
            await query.answer("نفدت الكروت من هذا النوع حالياً", show_alert=True)
            return

        await query.edit_message_text(
            get_quantity_text(network["name"], package["name"], package["price"], available),
            reply_markup=get_quantity_keyboard(package_id),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("choose_package_callback error")
        await query.answer("حدث خطأ، حاول مجدداً", show_alert=True)


async def choose_qty_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, package_id_raw, qty_raw = query.data.split(":")
        package_id = int(package_id_raw)
        qty = int(qty_raw)

        network_id = db.get_user_network_id(query.from_user.id)
        if not network_id:
            await query.answer("اختر شبكة أولاً", show_alert=True)
            return

        package = db.get_package_by_id(package_id)
        network = db.get_network_by_id(network_id)

        if not package or not network:
            await query.answer("حدث خطأ", show_alert=True)
            return

        available = db.count_available_cards(network_id, package_id)
        if available < qty:
            await query.answer(f"المتوفر فقط {available}", show_alert=True)
            return

        await query.edit_message_text(
            get_payment_choice_text(network["name"], package["name"], qty, package["price"] * qty),
            reply_markup=get_payment_methods_keyboard(package_id, qty),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("choose_qty_callback error")
        await query.answer("حدث خطأ، حاول مجدداً", show_alert=True)


async def change_qty_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        package_id = int(query.data.split(":")[1])
        network_id = db.get_user_network_id(query.from_user.id)

        if not network_id:
            await query.answer("اختر شبكة أولاً", show_alert=True)
            return

        package = db.get_package_by_id(package_id)
        network = db.get_network_by_id(network_id)

        if not package or not network:
            await query.answer("حدث خطأ", show_alert=True)
            return

        available = db.count_available_cards(network_id, package_id)
        await query.edit_message_text(
            get_quantity_text(network["name"], package["name"], package["price"], available),
            reply_markup=get_quantity_keyboard(package_id),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("change_qty_callback error")
        await query.answer("حدث خطأ، حاول مجدداً", show_alert=True)


async def back_to_packages_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        network_id = db.get_user_network_id(query.from_user.id)
        if not network_id:
            await query.edit_message_text(
                get_welcome_text(),
                reply_markup=get_networks_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

        network = db.get_network_by_id(network_id)
        if not network:
            await query.answer("الشبكة غير موجودة", show_alert=True)
            return

        await send_or_edit_network_menu(query, context, network)
    except Exception:
        logger.exception("back_to_packages_callback error")
        await query.answer("حدث خطأ، حاول مجدداً", show_alert=True)


async def change_network_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        get_welcome_text(),
        reply_markup=get_networks_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def refresh_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("✅ تم التحديث")

    try:
        network_id = db.get_user_network_id(query.from_user.id)
        if not network_id:
            await query.edit_message_text(
                get_welcome_text(),
                reply_markup=get_networks_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

        network = db.get_network_by_id(network_id)
        if not network:
            await query.answer("الشبكة غير موجودة", show_alert=True)
            return

        await send_or_edit_network_menu(query, context, network)
    except Exception:
        logger.exception("refresh_menu_callback error")


# =========================
# الدفع بالنجوم
# =========================
async def pay_stars_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, package_id_raw, qty_raw = query.data.split(":")
        package_id = int(package_id_raw)
        qty = int(qty_raw)

        network_id = db.get_user_network_id(query.from_user.id)
        if not network_id:
            await query.answer("اختر شبكة أولاً", show_alert=True)
            return

        network = db.get_network_by_id(network_id)
        package = db.get_package_by_id(package_id)

        if not network or not package or not package["is_active"]:
            await query.answer("الطلب غير صالح", show_alert=True)
            return

        available = db.count_available_cards(network_id, package_id)
        if available < qty:
            await query.answer(f"المتوفر فقط {available}", show_alert=True)
            return

        total_stars = package["price"] * qty
        payload = f"buycard|{network_id}|{package_id}|{qty}"

        await query.edit_message_text(
            "⏳ <b>جارٍ إرسال فاتورة الدفع بالنجوم...</b>\n"
            "━━━━━━━━━━━━━━\n"
            f"🌐 <b>الشبكة:</b> {network['name']}\n"
            f"📦 <b>الباقة:</b> {package['name']}\n"
            f"🔢 <b>الكمية:</b> {qty}\n"
            f"⭐ <b>الإجمالي:</b> {total_stars} نجمة\n"
            "━━━━━━━━━━━━━━\n"
            "ستصلك الفاتورة الآن، اضغط عليها للدفع.",
            parse_mode=ParseMode.HTML,
        )

        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title=f"شراء كرت - {network['name']}",
            description=f"{package['name']} × {qty} من شبكة {network['name']}",
            payload=payload,
            currency="XTR",
            prices=[LabeledPrice(label=f"{package['name']} × {qty}", amount=total_stars)],
            provider_token="",
        )

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                "💡 <b>لم تتمكن من الدفع بالنجوم؟</b>\n"
                "━━━━━━━━━━━━━━\n"
                "يمكنك الدفع عن طريق التحويل البنكي\n"
                "أو العودة لاختيار باقة أخرى."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🏦 تحويل بنكي بدلاً عن النجوم",
                    callback_data=f"pay_bank:{package_id}:{qty}"
                )],
                [InlineKeyboardButton("⬅️ رجوع للباقات", callback_data="back_to_packages")],
                [InlineKeyboardButton("🌐 تغيير الشبكة", callback_data="change_network")],
            ]),
        )

    except Exception:
        logger.exception("pay_stars_callback error")
        await query.answer("حدث خطأ، حاول مجدداً", show_alert=True)


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    try:
        parts = query.invoice_payload.split("|", 3)
        if len(parts) != 4 or parts[0] != "buycard":
            await query.answer(ok=False, error_message="بيانات الطلب غير صالحة.")
            return

        network_id, package_id, qty = int(parts[1]), int(parts[2]), int(parts[3])
        available = db.count_available_cards(network_id, package_id)

        if available < qty:
            await query.answer(ok=False, error_message=f"المتوفر حالياً {available} فقط.")
            return

        await query.answer(ok=True)
    except Exception:
        logger.exception("precheckout_callback error")
        await query.answer(ok=False, error_message="حدث خطأ أثناء التحقق.")


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.successful_payment:
        return

    payment = message.successful_payment
    try:
        parts = payment.invoice_payload.split("|", 3)
        if len(parts) != 4 or parts[0] != "buycard":
            await message.reply_text(f"مشكلة في الطلب، تواصل مع المشرف: {SUPPORT_USERNAME}")
            return

        network_id, package_id, qty = int(parts[1]), int(parts[2]), int(parts[3])
        user = update.effective_user

        # ✅ تمرير الاسم الكامل عند البيع
        cards = db.sell_available_cards_bulk(
            network_id=network_id,
            package_id=package_id,
            quantity=qty,
            buyer_id=user.id,
            buyer_username=user.username,
            buyer_first_name=user.first_name,
            buyer_last_name=user.last_name,
            price=payment.total_amount,
            telegram_payment_charge_id=payment.telegram_payment_charge_id,
            provider_payment_charge_id=payment.provider_payment_charge_id,
        )

        if len(cards) < qty:
            await message.reply_text(f"تم الدفع لكن تعذر التسليم. تواصل مع المشرف: {SUPPORT_USERNAME}")
            return

        network = db.get_network_by_id(network_id)
        package = db.get_package_by_id(package_id)
        await message.reply_text(
            format_cards_message(network["name"], package["name"], cards),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("successful_payment_handler error")
        await message.reply_text(f"حدث خطأ أثناء التسليم، تواصل مع المشرف: {SUPPORT_USERNAME}")


# =========================
# التحويل البنكي
# =========================
async def pay_bank_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, package_id_raw, qty_raw = query.data.split(":")
        package_id = int(package_id_raw)
        qty = int(qty_raw)

        network_id = db.get_user_network_id(query.from_user.id)
        if not network_id:
            await query.answer("اختر شبكة أولاً", show_alert=True)
            return

        package = db.get_package_by_id(package_id)
        network = db.get_network_by_id(network_id)

        if not package or not network:
            await query.answer("حدث خطأ", show_alert=True)
            return

        available = db.count_available_cards(network_id, package_id)
        if available < qty:
            await query.answer(f"المتوفر فقط {available}", show_alert=True)
            return

        total_price = package["price"] * qty
        user = query.from_user

        # ✅ تخزين الاسم الكامل عند إنشاء الطلب
        order_id = db.create_bank_order(
            network_id=network_id,
            buyer_id=user.id,
            buyer_username=user.username,
            buyer_first_name=user.first_name,
            buyer_last_name=user.last_name,
            package_id=package_id,
            quantity=qty,
            price=total_price,
        )

        # ✅ عرض الاسم بشكل صحيح للمستخدم
        buyer_display = get_user_display(user)

        await query.edit_message_text(
            "🏦 <b>تم تسجيل طلبك بنجاح</b>\n"
            "━━━━━━━━━━━━━━\n"
            f"🆔 <b>رقم الطلب:</b> {order_id}\n"
            f"🌐 <b>الشبكة:</b> {network['name']}\n"
            f"📦 <b>الباقة:</b> {package['name']}\n"
            f"🔢 <b>الكمية:</b> {qty}\n"
            f"💰 <b>الإجمالي:</b> {total_price} ⭐\n"
            "━━━━━━━━━━━━━━\n"
            "اضغط أدناه للتواصل مع المشرف وإتمام التحويل.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📞 التواصل مع المشرف", url=SUPPORT_URL)],
                [InlineKeyboardButton("⬅️ رجوع للباقات", callback_data="back_to_packages")],
                [InlineKeyboardButton("🌐 تغيير الشبكة", callback_data="change_network")],
            ]),
        )

        # ✅ رسالة الأدمن مع الاسم الحقيقي + رابط للحساب
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    "🏦 <b>طلب تحويل بنكي جديد</b>\n"
                    "━━━━━━━━━━━━━━\n"
                    f"🆔 رقم الطلب: <b>{order_id}</b>\n"
                    f"🌐 الشبكة: {network['name']}\n"
                    f"📦 الباقة: {package['name']}\n"
                    f"🔢 الكمية: {qty}\n"
                    f"💰 الإجمالي: {total_price} ⭐\n"
                    "━━━━━━━━━━━━━━\n"
                    f"👤 الاسم: <b>{buyer_display}</b>\n"
                    f"🔗 الحساب: <a href=\"tg://user?id={user.id}\">{buyer_display}</a>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    except Exception:
        logger.exception("pay_bank_callback error")
        await query.answer("حدث خطأ، حاول مجدداً", show_alert=True)


# =========================
# لوحة المشرف
# =========================
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_owner(update.effective_user.id):
        return

    ADMIN_CACHE.pop(update.effective_user.id, None)
    await update.message.reply_text(
        get_admin_home_text(),
        reply_markup=get_admin_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def admin_home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    ADMIN_CACHE.pop(query.from_user.id, None)
    await query.edit_message_text(
        get_admin_home_text(),
        reply_markup=get_admin_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def admin_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    s = db.get_stats()
    await query.edit_message_text(
        "📊 <b>إحصائيات النظام</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🌐 الشبكات النشطة: {s['active_networks']}\n"
        f"📦 الباقات النشطة: {s['active_packages']}\n"
        f"🟢 الكروت المتاحة: {s['available_cards']}\n"
        f"🔴 الكروت المباعة: {s['sold_cards']}\n"
        f"✅ الطلبات المكتملة: {s['completed_orders']}\n"
        f"🏦 الطلبات البنكية المعلقة: {s['pending_bank_orders']}\n"
        f"💰 إجمالي المبيعات: {s['sales_total']}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="admin_home")]
        ]),
    )


async def admin_networks_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    lines = ["📡 <b>إدارة الشبكات</b>\n━━━━━━━━━━━━━━"]
    for net in db.list_networks():
        lines.append(
            f"• <b>{net['name']}</b>\n"
            f"المعرف: <code>{net['identifier']}</code>\n"
            f"الحالة: {'مفعلة ✅' if net['is_active'] else 'معطلة ⛔'}\n"
            f"صورة: {'نعم 🖼️' if net['image_file_id'] else 'لا'}"
        )

    await query.edit_message_text(
        "\n\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=get_networks_manage_keyboard(),
    )


async def toggle_network_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    db.toggle_network_status(int(query.data.split(":")[1]))
    await admin_networks_callback(update, context)


async def admin_add_network_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    ADMIN_CACHE[query.from_user.id] = {"step": "waiting_add_network"}
    await query.edit_message_text(
        "➕ <b>إضافة شبكة جديدة</b>\n"
        "━━━━━━━━━━━━━━\n"
        "أرسل الآن بهذا الشكل:\n\n"
        "<code>identifier|اسم الشبكة</code>\n\n"
        "مثال:\n<code>zain_plus|شبكة زين بلس</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="admin_networks")]
        ]),
    )


async def delete_network_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    try:
        network_id = int(query.data.split(":")[1])
        network = db.get_network_by_id(network_id)

        if not network:
            await query.answer("الشبكة غير موجودة", show_alert=True)
            return

        with db.get_conn() as conn:
            cards_count = conn.execute(
                "SELECT COUNT(*) FROM cards WHERE network_id = ?", (network_id,)
            ).fetchone()[0]
            orders_count = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE network_id = ?", (network_id,)
            ).fetchone()[0]

        status_text = "مفعلة ✅" if network["is_active"] else "معطلة ⛔"

        warning_text = (
            "🚨 <b>تحذير: حذف شبكة نهائياً</b>\n"
            "━━━━━━━━━━━━━━\n"
            f"🌐 <b>الشبكة:</b> {network['name']}\n"
            f"🔑 <b>المعرف:</b> <code>{network['identifier']}</code>\n"
            f"📌 <b>الحالة:</b> {status_text}\n"
            f"🎫 <b>الكروت المرتبطة:</b> {cards_count}\n"
            f"🧾 <b>الطلبات المرتبطة:</b> {orders_count}\n"
            "━━━━━━━━━━━━━━\n"
        )

        if orders_count > 0:
            warning_text += (
                "❌ <b>لا يمكن الحذف!</b>\n"
                f"يوجد <b>{orders_count}</b> طلب مرتبط بهذه الشبكة.\n"
                "عطّل الشبكة بدلاً من حذفها للحفاظ على سجل الطلبات."
            )
            await query.edit_message_text(
                warning_text,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ رجوع لإدارة الشبكات", callback_data="admin_networks")]
                ]),
            )
        else:
            warning_text += (
                "⚠️ <b>هذا الإجراء لا يمكن التراجع عنه!</b>\n"
                f"سيتم حذف الشبكة وجميع كروتها البالغة <b>{cards_count}</b> كرت بشكل دائم."
            )
            await query.edit_message_text(
                warning_text,
                parse_mode=ParseMode.HTML,
                reply_markup=get_delete_confirm_keyboard(network_id),
            )

    except Exception:
        logger.exception("delete_network_callback error")
        await query.answer("حدث خطأ، حاول مجدداً", show_alert=True)


async def confirm_delete_network_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    try:
        network_id = int(query.data.split(":")[1])
        success, message = db.delete_network(network_id)

        if success:
            await query.edit_message_text(
                f"🗑️ <b>تم الحذف النهائي</b>\n"
                "━━━━━━━━━━━━━━\n"
                f"{message}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📡 إدارة الشبكات", callback_data="admin_networks")],
                    [InlineKeyboardButton("🏠 الرئيسية", callback_data="admin_home")],
                ]),
            )
        else:
            await query.edit_message_text(
                f"❌ <b>فشل الحذف</b>\n"
                "━━━━━━━━━━━━━━\n"
                f"{message}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ رجوع لإدارة الشبكات", callback_data="admin_networks")]
                ]),
            )

    except Exception:
        logger.exception("confirm_delete_network_callback error")
        await query.answer("حدث خطأ أثناء تنفيذ الحذف", show_alert=True)


async def admin_set_network_image_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    await query.edit_message_text(
        "🖼️ <b>تعيين صورة شبكة</b>\n━━━━━━━━━━━━━━\nاختر الشبكة:",
        parse_mode=ParseMode.HTML,
        reply_markup=get_network_image_select_keyboard("set_net_image"),
    )


async def admin_remove_network_image_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    await query.edit_message_text(
        "🗑️ <b>حذف صورة شبكة</b>\n━━━━━━━━━━━━━━\nاختر الشبكة:",
        parse_mode=ParseMode.HTML,
        reply_markup=get_network_image_select_keyboard("remove_net_image"),
    )


async def set_net_image_choose_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    network_id = int(query.data.split(":")[1])
    network = db.get_network_by_id(network_id)
    if not network:
        await query.answer("الشبكة غير موجودة", show_alert=True)
        return

    ADMIN_CACHE[query.from_user.id] = {"step": "waiting_network_image_photo", "network_id": network_id}

    await query.edit_message_text(
        f"🖼️ <b>رفع صورة للشبكة</b>\n━━━━━━━━━━━━━━\n"
        f"🌐 <b>{network['name']}</b>\n\nأرسل الصورة الآن.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="admin_home")]
        ]),
    )


async def remove_net_image_choose_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    network_id = int(query.data.split(":")[1])
    network = db.get_network_by_id(network_id)
    if not network:
        await query.answer("الشبكة غير موجودة", show_alert=True)
        return

    db.remove_network_image(network_id)
    await query.edit_message_text(
        f"✅ تم حذف صورة الشبكة: <b>{network['name']}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="admin_home")]
        ]),
    )


async def admin_packages_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    lines = ["📦 <b>إدارة الباقات</b>\n━━━━━━━━━━━━━━"]
    for pkg in db.list_packages():
        lines.append(
            f"• <b>{pkg['name']}</b>\n"
            f"السعر: {pkg['price']} ⭐\n"
            f"الحالة: {'مفعلة ✅' if pkg['is_active'] else 'معطلة ⛔'}"
        )

    await query.edit_message_text(
        "\n\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=get_packages_manage_keyboard(),
    )


async def admin_add_package_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    ADMIN_CACHE[query.from_user.id] = {"step": "waiting_add_package"}
    await query.edit_message_text(
        "➕ <b>إضافة باقة جديدة</b>\n"
        "━━━━━━━━━━━━━━\n"
        "أرسل بهذا الشكل:\n\n"
        "<code>اسم الباقة|السعر</code>\n\n"
        "مثال:\n<code>كرت شهر|500</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="admin_packages")]
        ]),
    )


async def package_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    package_id = int(query.data.split(":")[1])
    pkg = db.get_package_by_id(package_id)
    if not pkg:
        await query.answer("الباقة غير موجودة", show_alert=True)
        return

    await query.edit_message_text(
        f"📦 <b>{pkg['name']}</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"💰 السعر: {pkg['price']} ⭐\n"
        f"🔁 الحالة: {'مفعلة ✅' if pkg['is_active'] else 'معطلة ⛔'}",
        parse_mode=ParseMode.HTML,
        reply_markup=get_package_actions_keyboard(package_id),
    )


async def package_edit_price_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    package_id = int(query.data.split(":")[1])
    pkg = db.get_package_by_id(package_id)
    if not pkg:
        await query.answer("الباقة غير موجودة", show_alert=True)
        return

    ADMIN_CACHE[query.from_user.id] = {"step": "waiting_edit_package_price", "package_id": package_id}
    await query.edit_message_text(
        f"💰 <b>تعديل سعر الباقة</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"📦 الباقة: <b>{pkg['name']}</b>\n"
        f"السعر الحالي: {pkg['price']} ⭐\n\n"
        "أرسل السعر الجديد كرقم فقط.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data=f"pkg_view:{package_id}")]
        ]),
    )


async def package_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    db.toggle_package_status(int(query.data.split(":")[1]))
    await admin_packages_callback(update, context)


async def delete_package_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    try:
        package_id = int(query.data.split(":")[1])
        pkg = db.get_package_by_id(package_id)
        if not pkg:
            await query.answer("الباقة غير موجودة", show_alert=True)
            return

        with db.get_conn() as conn:
            cards_count = conn.execute(
                "SELECT COUNT(*) FROM cards WHERE package_id = ?", (package_id,)
            ).fetchone()[0]
            orders_count = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE package_id = ?", (package_id,)
            ).fetchone()[0]

        status_text = "مفعلة ✅" if pkg["is_active"] else "معطلة ⛔"

        warning_text = (
            "🚨 <b>تحذير: حذف باقة نهائياً</b>\n"
            "━━━━━━━━━━━━━━\n"
            f"📦 <b>الباقة:</b> {pkg['name']}\n"
            f"💰 <b>السعر:</b> {pkg['price']} ⭐\n"
            f"📌 <b>الحالة:</b> {status_text}\n"
            f"🎫 <b>الكروت المرتبطة:</b> {cards_count}\n"
            f"🧾 <b>الطلبات المرتبطة:</b> {orders_count}\n"
            "━━━━━━━━━━━━━━\n"
        )

        if orders_count > 0:
            warning_text += (
                "❌ <b>لا يمكن الحذف!</b>\n"
                f"يوجد <b>{orders_count}</b> طلب مرتبط بهذه الباقة.\n"
                "عطّل الباقة بدلاً من حذفها للحفاظ على سجل الطلبات."
            )
            await query.edit_message_text(
                warning_text,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ رجوع لإدارة الباقات", callback_data="admin_packages")]
                ]),
            )
        else:
            warning_text += (
                "⚠️ <b>هذا الإجراء لا يمكن التراجع عنه!</b>\n"
                f"سيتم حذف الباقة وجميع كروتها البالغة <b>{cards_count}</b> كرت بشكل دائم."
            )
            await query.edit_message_text(
                warning_text,
                parse_mode=ParseMode.HTML,
                reply_markup=get_delete_package_confirm_keyboard(package_id),
            )

    except Exception:
        logger.exception("delete_package_callback error")
        await query.answer("حدث خطأ، حاول مجدداً", show_alert=True)


async def confirm_delete_package_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    try:
        package_id = int(query.data.split(":")[1])
        success, message = db.delete_package(package_id)

        if success:
            await query.edit_message_text(
                "🗑️ <b>تم الحذف النهائي</b>\n"
                "━━━━━━━━━━━━━━\n"
                f"{message}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📦 إدارة الباقات", callback_data="admin_packages")],
                    [InlineKeyboardButton("🏠 الرئيسية", callback_data="admin_home")],
                ]),
            )
        else:
            await query.edit_message_text(
                "❌ <b>فشل الحذف</b>\n"
                "━━━━━━━━━━━━━━\n"
                f"{message}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ رجوع لإدارة الباقات", callback_data="admin_packages")]
                ]),
            )

    except Exception:
        logger.exception("confirm_delete_package_callback error")
        await query.answer("حدث خطأ أثناء تنفيذ الحذف", show_alert=True)


async def admin_bank_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    orders = db.list_pending_bank_orders()
    if not orders:
        text = "🏦 <b>الطلبات البنكية</b>\n━━━━━━━━━━━━━━\nلا توجد طلبات معلقة."
    else:
        lines = ["🏦 <b>الطلبات البنكية المعلقة</b>\n━━━━━━━━━━━━━━"]
        for o in orders:
            # ✅ عرض الاسم بدل الـ ID
            buyer = get_buyer_display(o)
            lines.append(
                f"🧾 <b>طلب #{o['id']}</b>\n"
                f"👤 {buyer} | 🌐 {o['network_name']}\n"
                f"📦 {o['package_name']} × {o['quantity']} = 💰 {o['price']} ⭐"
            )
        text = "\n\n".join(lines)

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_pending_orders_keyboard(),
    )


async def bank_order_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    order_id = int(query.data.split(":")[1])
    order = db.get_order_by_id(order_id)
    if not order:
        await query.answer("الطلب غير موجود", show_alert=True)
        return

    # ✅ عرض الاسم الكامل + رابط للحساب
    buyer = get_buyer_display(order)

    await query.edit_message_text(
        f"🏦 <b>طلب بنكي #{order['id']}</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"👤 المشتري: <a href=\"tg://user?id={order['buyer_id']}\">{buyer}</a>\n"
        f"🌐 الشبكة: {order['network_name']}\n"
        f"📦 الباقة: {order['package_name']}\n"
        f"🔢 الكمية: {order['quantity']}\n"
        f"💰 الإجمالي: {order['price']} ⭐\n"
        f"📌 الحالة: {order['status']}",
        parse_mode=ParseMode.HTML,
        reply_markup=get_bank_order_actions_keyboard(order_id),
    )


async def bank_order_complete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    order_id = int(query.data.split(":")[1])
    order = db.get_order_by_id(order_id)
    if not order:
        await query.answer("الطلب غير موجود", show_alert=True)
        return

    cards = db.complete_bank_order(order_id)
    if not cards:
        await query.answer("تعذر إكمال الطلب. قد لا يوجد مخزون كافٍ.", show_alert=True)
        return

    try:
        await context.bot.send_message(
            chat_id=order["buyer_id"],
            text=format_cards_message(order["network_name"], order["package_name"], cards),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    buyer = get_buyer_display(order)
    await query.edit_message_text(
        f"✅ <b>تم تأكيد الطلب #{order_id} وتسليم الكروت</b>\n"
        f"👤 المشتري: {buyer}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="admin_bank_orders")]
        ]),
    )


async def bank_order_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    order_id = int(query.data.split(":")[1])
    order = db.get_order_by_id(order_id)
    if not order:
        await query.answer("الطلب غير موجود", show_alert=True)
        return

    if not db.cancel_bank_order(order_id):
        await query.answer("لم يتم إلغاء الطلب", show_alert=True)
        return

    try:
        await context.bot.send_message(
            chat_id=order["buyer_id"],
            text=(
                f"❌ تم إلغاء الطلب البنكي رقم #{order_id}\n"
                f"📞 للاستفسار تواصل مع المشرف: {SUPPORT_USERNAME}"
            ),
        )
    except Exception:
        pass

    buyer = get_buyer_display(order)
    await query.edit_message_text(
        f"✅ تم إلغاء الطلب البنكي #{order_id}\n"
        f"👤 المشتري: {buyer}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="admin_bank_orders")]
        ]),
    )


# =========================
# معالجة النصوص والملفات (المشرف)
# =========================
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_owner(update.effective_user.id):
        return

    state = UPLOAD_CACHE.get(update.effective_user.id)
    if not state or state.get("step") != "waiting_for_txt":
        return

    document = update.message.document
    if not document or not document.file_name.lower().endswith(".txt"):
        await update.message.reply_text("الملف يجب أن يكون بصيغة .txt فقط")
        return

    tg_file = await context.bot.get_file(document.file_id)
    content = bytes(await tg_file.download_as_bytearray()).decode("utf-8", errors="ignore")

    UPLOAD_CACHE[update.effective_user.id] = {"step": "waiting_for_network", "raw_cards_text": content}
    await update.message.reply_text(
        "اختر الشبكة التي تريد إضافة الكروت لها:",
        reply_markup=get_upload_networks_keyboard(),
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_owner(update.effective_user.id):
        return

    state = ADMIN_CACHE.get(update.effective_user.id)
    if not state or state.get("step") != "waiting_network_image_photo":
        return

    photo = update.message.photo
    if not photo:
        return

    network_id = state.get("network_id")
    network = db.get_network_by_id(network_id)
    if not network:
        ADMIN_CACHE.pop(update.effective_user.id, None)
        await update.message.reply_text("الشبكة غير موجودة.")
        return

    db.set_network_image(network_id, photo[-1].file_id)
    ADMIN_CACHE.pop(update.effective_user.id, None)

    await update.message.reply_text(
        f"✅ تم حفظ صورة الشبكة: <b>{network['name']}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع للوحة المشرف", callback_data="admin_home")]
        ]),
    )


async def admin_upload_cards_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    UPLOAD_CACHE[query.from_user.id] = {"step": "waiting_for_txt"}
    await query.edit_message_text(
        "📤 <b>رفع كروت جديدة</b>\n"
        "━━━━━━━━━━━━━━\n"
        "أرسل ملف TXT الآن.\n\n"
        "كل سطر = كرت واحد\n"
        "مثال:\n<code>user123:pass456</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="admin_home")]
        ]),
    )


async def handle_upload_network_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    state = UPLOAD_CACHE.get(query.from_user.id)
    if not state or state.get("step") != "waiting_for_network":
        return

    network_id = int(query.data.split(":")[1])
    network = db.get_network_by_id(network_id)
    if not network:
        await query.answer("الشبكة غير موجودة", show_alert=True)
        return

    UPLOAD_CACHE[query.from_user.id].update({"step": "waiting_for_package", "network_id": network_id})
    await query.edit_message_text(
        f"✅ الشبكة: {network['name']}\n\nاختر الباقة:",
        reply_markup=get_upload_packages_keyboard(),
    )


async def handle_upload_package_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    state = UPLOAD_CACHE.get(query.from_user.id)
    if not state or state.get("step") != "waiting_for_package":
        return

    network_id = state.get("network_id")
    raw_cards_text = state.get("raw_cards_text", "")
    package_id = int(query.data.split(":")[1])

    network = db.get_network_by_id(network_id)
    package = db.get_package_by_id(package_id)

    if not network or not package or not raw_cards_text:
        UPLOAD_CACHE.pop(query.from_user.id, None)
        await query.edit_message_text("حدث خطأ. أعد المحاولة.")
        return

    cards = [line.strip() for line in raw_cards_text.splitlines() if line.strip()]
    inserted, duplicates = db.add_cards_bulk(network_id, package_id, cards)
    UPLOAD_CACHE.pop(query.from_user.id, None)

    await query.edit_message_text(
        f"✅ <b>تم رفع الكروت</b>\n\n"
        f"🌐 الشبكة: {network['name']}\n"
        f"📦 الباقة: {package['name']}\n"
        f"📥 المضاف: {inserted}\n"
        f"♻️ المكرر المتجاهل: {duplicates}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع للوحة المشرف", callback_data="admin_home")]
        ]),
    )


async def admin_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return

    if not is_owner(update.effective_user.id):
        await fallback_handler(update, context)
        return

    user_id = update.effective_user.id
    state = ADMIN_CACHE.get(user_id)

    if state:
        step = state.get("step")
        text = update.message.text.strip()

        if step == "waiting_add_network":
            ADMIN_CACHE.pop(user_id, None)
            if "|" not in text:
                await update.message.reply_text(
                    "الصيغة الصحيحة:\n<code>identifier|اسم الشبكة</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
            identifier, name = [x.strip() for x in text.split("|", 1)]
            if not identifier or not name:
                await update.message.reply_text("يجب كتابة المعرف واسم الشبكة.")
                return
            if db.add_network(name=name, identifier=identifier):
                await update.message.reply_text(
                    f"✅ تم إنشاء الشبكة:\n<b>{name}</b> | <code>{identifier}</code>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_text("⚠️ هذا المعرف مستخدم مسبقاً.")
            return

        if step == "waiting_add_package":
            ADMIN_CACHE.pop(user_id, None)
            if "|" not in text:
                await update.message.reply_text(
                    "الصيغة الصحيحة:\n<code>اسم الباقة|السعر</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
            name, price_raw = [x.strip() for x in text.split("|", 1)]
            if not name or not price_raw.isdigit():
                await update.message.reply_text("يجب كتابة الاسم والسعر بشكل صحيح.")
                return
            if db.add_package(name, int(price_raw)):
                await update.message.reply_text(
                    f"✅ تم إنشاء الباقة:\n<b>{name}</b> - {price_raw} ⭐",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_text("⚠️ هذه الباقة موجودة مسبقاً.")
            return

        if step == "waiting_edit_package_price":
            package_id = state.get("package_id")
            if not text.isdigit():
                await update.message.reply_text("أرسل السعر كرقم فقط.")
                return
            ADMIN_CACHE.pop(user_id, None)
            db.update_package_price(package_id, int(text))
            await update.message.reply_text("✅ تم تعديل السعر بنجاح.")
            return

    await fallback_handler(update, context)


# =========================
# أوامر إضافية للمشرف
# =========================
async def upload_cards_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_owner(update.effective_user.id):
        return

    UPLOAD_CACHE[update.effective_user.id] = {"step": "waiting_for_txt"}
    await update.message.reply_text("📤 أرسل ملف TXT الآن.")


async def stock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_owner(update.effective_user.id):
        return

    networks = db.list_networks()
    if not networks:
        await update.message.reply_text("لا توجد شبكات حالياً.")
        return

    lines = ["📦 <b>المخزون الحالي</b>\n━━━━━━━━━━━━━━"]
    for net in networks:
        lines.append(f"\n🌐 <b>{net['name']}</b>")
        found = False
        for pkg in db.list_packages():
            count = db.count_available_cards(net["id"], pkg["id"])
            if count > 0:
                found = True
                lines.append(f"  • {pkg['name']}: {count}")
        if not found:
            lines.append("  • لا يوجد كروت متاحة")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(
        f"✨ أهلاً بك في <b>{BRAND_NAME}</b>\n"
        "اكتب /start للبدء.\n"
        f"📞 المشرف: {SUPPORT_USERNAME}",
        parse_mode=ParseMode.HTML,
    )


# =========================
# التشغيل
# =========================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # User commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))

    # Admin commands
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("upload_cards", upload_cards_command))
    app.add_handler(CommandHandler("stock", stock_command))

    # User callbacks
    app.add_handler(CallbackQueryHandler(show_help_callback,         pattern=r"^show_help$"))
    app.add_handler(CallbackQueryHandler(select_network_callback,    pattern=r"^select_network:"))
    app.add_handler(CallbackQueryHandler(choose_package_callback,    pattern=r"^choose_package:"))
    app.add_handler(CallbackQueryHandler(choose_qty_callback,        pattern=r"^qty:"))
    app.add_handler(CallbackQueryHandler(change_qty_callback,        pattern=r"^change_qty:"))
    app.add_handler(CallbackQueryHandler(back_to_packages_callback,  pattern=r"^back_to_packages$"))
    app.add_handler(CallbackQueryHandler(change_network_callback,    pattern=r"^change_network$"))
    app.add_handler(CallbackQueryHandler(refresh_menu_callback,      pattern=r"^refresh_menu$"))
    app.add_handler(CallbackQueryHandler(pay_stars_callback,         pattern=r"^pay_stars:"))
    app.add_handler(CallbackQueryHandler(pay_bank_callback,          pattern=r"^pay_bank:"))

    # Admin callbacks
    app.add_handler(CallbackQueryHandler(admin_home_callback,                  pattern=r"^admin_home$"))
    app.add_handler(CallbackQueryHandler(admin_upload_cards_callback,          pattern=r"^admin_upload_cards$"))
    app.add_handler(CallbackQueryHandler(admin_stats_callback,                 pattern=r"^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_networks_callback,              pattern=r"^admin_networks$"))
    app.add_handler(CallbackQueryHandler(toggle_network_callback,              pattern=r"^toggle_network:"))
    app.add_handler(CallbackQueryHandler(admin_add_network_callback,           pattern=r"^admin_add_network$"))
    app.add_handler(CallbackQueryHandler(delete_network_callback,              pattern=r"^delete_network:"))
    app.add_handler(CallbackQueryHandler(confirm_delete_network_callback,      pattern=r"^confirm_delete_network:"))
    app.add_handler(CallbackQueryHandler(admin_set_network_image_callback,     pattern=r"^admin_set_network_image$"))
    app.add_handler(CallbackQueryHandler(admin_remove_network_image_callback,  pattern=r"^admin_remove_network_image$"))
    app.add_handler(CallbackQueryHandler(set_net_image_choose_callback,        pattern=r"^set_net_image:"))
    app.add_handler(CallbackQueryHandler(remove_net_image_choose_callback,     pattern=r"^remove_net_image:"))
    app.add_handler(CallbackQueryHandler(admin_packages_callback,              pattern=r"^admin_packages$"))
    app.add_handler(CallbackQueryHandler(admin_add_package_callback,           pattern=r"^admin_add_package$"))
    app.add_handler(CallbackQueryHandler(package_view_callback,                pattern=r"^pkg_view:"))
    app.add_handler(CallbackQueryHandler(package_edit_price_callback,          pattern=r"^pkg_edit_price:"))
    app.add_handler(CallbackQueryHandler(package_toggle_callback,              pattern=r"^pkg_toggle:"))
    app.add_handler(CallbackQueryHandler(delete_package_callback,              pattern=r"^delete_package:"))
    app.add_handler(CallbackQueryHandler(confirm_delete_package_callback,      pattern=r"^confirm_delete_package:"))
    app.add_handler(CallbackQueryHandler(admin_bank_orders_callback,           pattern=r"^admin_bank_orders$"))
    app.add_handler(CallbackQueryHandler(bank_order_view_callback,             pattern=r"^bank_order_view:"))
    app.add_handler(CallbackQueryHandler(bank_order_complete_callback,         pattern=r"^bank_order_complete:"))
    app.add_handler(CallbackQueryHandler(bank_order_cancel_callback,           pattern=r"^bank_order_cancel:"))
    app.add_handler(CallbackQueryHandler(handle_upload_network_callback,       pattern=r"^upload_net:"))
    app.add_handler(CallbackQueryHandler(handle_upload_package_callback,       pattern=r"^upload_pkg:"))

    # Payment
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    # File / photo / text
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_router))

    print(f"✅ {BRAND_NAME} Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()