import streamlit as st
import os, requests, json, io, cv2, re
import pandas as pd
import numpy as np
from PIL import Image
from dotenv import load_dotenv
from groq import Groq
from datetime import datetime, timedelta
from streamlit_cookies_manager import EncryptedCookieManager

# --- CONFIGURATION & AUTH ---
st.set_page_config(page_title="Inventory OS", page_icon="📦", layout="centered")

# 1. Environment Loading (Local & Cloud)
load_dotenv() 

def get_secret(key, default=None):
    """Safely get secrets from st.secrets (Cloud) or os.getenv (Local)."""
    try:
        # Some Streamlit versions/environments crash on st.secrets if no .toml exists
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)

GROQ_API_KEY = get_secret("GROQ_API_KEY")
OCR_SPACE_API_KEY = get_secret("OCR_SPACE_API_KEY", "helloworld")
COOKIE_SECRET = get_secret("COOKIE_SECRET", "godrej-inv-secret-key-2024")
PRICE_CACHE = "_price_list_cache.pkl"
DATABASES_DIR = "databases"
EXCEL_FILE = "inventory.xlsx"
TXN_FILE = "transactions.csv"
THRESHOLD_FILE = "thresholds.json"
ORDERS_FILE = "orders.json"

def load_thresholds():
    if os.path.exists(THRESHOLD_FILE):
        with open(THRESHOLD_FILE, "r") as f:
            return json.load(f)
    return {}

def save_thresholds(t_dict):
    with open(THRESHOLD_FILE, "w") as f:
        json.dump(t_dict, f, indent=2)

def load_orders():
    if os.path.exists(ORDERS_FILE):
        try:
            with open(ORDERS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_orders(o_list):
    with open(ORDERS_FILE, "w") as f:
        json.dump(o_list, f, indent=2)

def load_users():
    """Cloud-resilient user loader: falls back to st.secrets if users.json is missing."""
    if os.path.exists("users.json"):
        with open("users.json", "r") as f:
            return json.load(f)
    try:
        # Fallback for Streamlit Cloud Secrets
        if "USERS" in st.secrets:
            return json.loads(st.secrets["USERS"])
    except Exception:
        pass
    # Default fallback for initial Cloud setup or first run
    return {"admin": {"password": "123", "name": "Admin Account"}}

# 1. Persistent Login System (6-hour Encrypted Cookies)
SESSION_HOURS = 6
cookies = EncryptedCookieManager(
    prefix="inv_os_",
    password=COOKIE_SECRET
)
if not cookies.ready():
    st.stop()  # Wait for cookies to load (synchronous & reliable)

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

    # Try Cookie Recovery
    c_user = cookies.get("inv_user")
    c_time = cookies.get("inv_login_time")
    if c_user and c_time:
        try:
            login_dt = datetime.fromisoformat(c_time)
            if datetime.now() - login_dt < timedelta(hours=SESSION_HOURS):
                v_users = load_users()
                if c_user in v_users:
                    st.session_state["authenticated"] = True
                    st.session_state["user"] = c_user
                    st.session_state["name"] = v_users[c_user]["name"]
                    st.session_state["page"] = "Inventory" if c_user == "admin" else "Update Stock"
                    st.session_state.setdefault("transaction_type", None)
        except Exception:
            pass  # Corrupt cookie, just show login

if not st.session_state["authenticated"]:
    st.title("🔒 Staff Login")
    with st.form("Login"):
        username = st.text_input("Username").strip()
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Login"):
            v_users = load_users()
            if username in v_users and v_users[username]["password"] == password:
                # Set persistent cookies (6-hour session)
                cookies["inv_user"] = username
                cookies["inv_login_time"] = datetime.now().isoformat()
                cookies.save()
                st.session_state["authenticated"] = True
                st.session_state["user"] = username
                st.session_state["name"] = v_users[username]["name"]
                st.session_state["page"] = "Inventory" if username == "admin" else "Update Stock"
                st.session_state["transaction_type"] = None
                st.rerun()
            else:
                st.error("Invalid Credentials!")
    st.stop()

# --- CACHED RESOURCES ---
PRICE_CACHE = "_price_list_cache.pkl"

def clean_price(val):
    """Ensure price is a float and truncate to 2 decimal places."""
    try:
        fval = float(str(val).replace(",", "").strip())
        # Use floor/truncation logic for 2 decimals as requested
        return float(f"{int(fval * 100) / 100.0:.2f}")
    except:
        return 0.0

@st.cache_data(show_spinner="📄 Aggregating price lists...")
def load_price_list():
    """Aggregates all .xlsx price lists from the databases/ folder with memory efficiency."""
    try:
        if not os.path.exists(DATABASES_DIR):
            os.makedirs(DATABASES_DIR, exist_ok=True)
            # Migration check: auto-move old file if it still exists in root
            old_path = "priceListHomeFurniture.xlsx"
            if os.path.exists(old_path):
                os.rename(old_path, os.path.join(DATABASES_DIR, old_path))
            else:
                return None
            
        xlsx_files = [f for f in os.listdir(DATABASES_DIR) if f.endswith(".xlsx")]
        if not xlsx_files: return None
        
        # 1. Smart Cache Validation (Check if file list OR timestamps changed)
        latest_mtime = 0
        for f in xlsx_files:
            latest_mtime = max(latest_mtime, os.path.getmtime(os.path.join(DATABASES_DIR, f)))
            
        if os.path.exists(PRICE_CACHE):
            if os.path.getmtime(PRICE_CACHE) > latest_mtime:
                cached_df = pd.read_pickle(PRICE_CACHE)
                # CRITICAL: Verify that the cached files match the folder's current files exactly
                if "Source_File" in cached_df.columns:
                    cached_files = set(cached_df["Source_File"].unique())
                    if cached_files == set(xlsx_files):
                        return cached_df
        
        # 2. Loading All Files (Smart Header Detection & Highly Selective Column Loading)
        CODE_ALIAES = ["LN Code", "Product Code", "Item Code", "Code", "Itemcode", "LNCode"]
        DESC_ALIAES = ["LN Description", "Product Name", "Description", "Item Name", "ProductName", "LNDescription"]
        BASE_PRICE_ALIAES = ["Unit Consumer Basic", "Unit Basic Price", "Base Price", "Basic Price", "Rate", "BasicRate", "Basic"]
        MRP_ALIAES = ["MRP", "Maximum Retail Price", "Consumer Price", "Retail Price", "ConsumerPrice", "MRP Price"]
        
        all_dfs = []
        for xfile in xlsx_files:
            try:
                xls_path = os.path.join(DATABASES_DIR, xfile)
                with pd.ExcelFile(xls_path, engine='openpyxl') as xls:
                    for sheet_name in xls.sheet_names:
                        # --- SMART HEADER FINDER ---
                        best_header_idx = 0
                        max_matches = 0
                        peek_data = pd.read_excel(xls, sheet_name=sheet_name, nrows=20, header=None)
                        
                        for idx, row in peek_data.iterrows():
                            row_vals = [str(v).strip().lower() for v in row.values]
                            matches = 0
                            found_code = False
                            for val in row_vals:
                                if any(alias.lower() in val for alias in CODE_ALIAES):
                                    matches += 2
                                    found_code = True
                                if any(alias.lower() in val for alias in DESC_ALIAES): matches += 1
                                if any(alias.lower() in val for alias in BASE_PRICE_ALIAES): matches += 1
                                    
                            if found_code and matches > max_matches:
                                max_matches = matches
                                best_header_idx = idx
                                
                        df = pd.read_excel(xls, sheet_name=sheet_name, header=best_header_idx)
                        cols = df.columns.tolist()
                        
                        def find_best_col(options, aliases, blacklist=None):
                            options_clean = [str(c).strip().lower() for c in options]
                            for a in aliases:
                                if a.lower() in options_clean:
                                    return options[options_clean.index(a.lower())]
                            for c_idx, c_orig in enumerate(options):
                                c_low = str(c_orig).lower()
                                if blacklist and any(b.lower() in c_low for b in blacklist): continue
                                if any(a.lower() in c_low for a in aliases): return c_orig
                            return None

                        col_code = find_best_col(cols, CODE_ALIAES, blacklist=["HSN", "Tax", "Total", "Unit", "MRP"])
                        col_desc = find_best_col(cols, DESC_ALIAES, blacklist=["Code"])
                        col_base = find_best_col(cols, BASE_PRICE_ALIAES, blacklist=["MRP", "Tax"])
                        col_mrp = find_best_col(cols, MRP_ALIAES, blacklist=["Base", "Basic"])
                        
                        if col_code and col_desc:
                            subset = pd.DataFrame()
                            subset["LN Code"] = df[col_code].astype(str).str.strip().str.upper()
                            subset["LN Description"] = df[col_desc].astype(str).str.strip()
                            subset["Unit Consumer Basic"] = df[col_base].apply(clean_price) if col_base else 0.0
                            subset["MRP"] = df[col_mrp].apply(clean_price) if col_mrp else (df[col_base].apply(clean_price) if col_base else 0.0)
                            subset["Category"] = f"{xfile.replace('.xlsx','')} | {sheet_name}"
                            subset["Source_File"] = xfile
                            
                            # --- DATA CLEANING & VALIDATION ---
                            subset = subset.dropna(subset=["LN Code", "LN Description"])
                            is_alphanumeric = subset["LN Code"].str.match(r"^[A-Z0-9-]+$", na=False, case=False)
                            has_letters = subset["LN Code"].str.contains(r"[A-Z]", na=False, case=False)
                            has_numbers = subset["LN Code"].str.contains(r"[0-9]", na=False)
                            subset = subset[is_alphanumeric & has_letters & has_numbers]
                            all_dfs.append(subset)
                            # 1. Drop NaNs
                            subset = subset.dropna(subset=["LN Code", "LN Description"])
                            # 2. Hybrid Filter: Must contain both Letters and Numbers
                            # This is the most robust way to find "Codes" while ignoring junk text.
                            is_alphanumeric = subset["LN Code"].str.match(r"^[A-Z0-9-]+$", na=False, case=False)
                            has_letters = subset["LN Code"].str.contains(r"[A-Z]", na=False, case=False)
                            has_numbers = subset["LN Code"].str.contains(r"[0-9]", na=False)
                            
                            subset = subset[is_alphanumeric & has_letters & has_numbers]
                            
                            all_dfs.append(subset)
            except Exception as fe:
                st.sidebar.error(f"Error loading {xfile}: {fe}")
                continue
        
        if all_dfs:
            combined = pd.concat(all_dfs, ignore_index=True)
            # Total unique items across all databases
            combined = combined.drop_duplicates(subset=["LN Code"])
            combined = combined.dropna(subset=["LN Code", "LN Description"])
            combined = combined[combined["LN Code"].astype(str).str.lower() != "nan"]
            
            combined.to_pickle(PRICE_CACHE)
            return combined
        return None
    except Exception as e:
        st.session_state["last_ocr_err"] = f"Price List Aggregation Error: {e}"
        return None

price_list_df = load_price_list()

def get_orders_with_shortage():
    """Returns a list of order IDs that have stock shortages among active orders."""
    if not os.path.exists(ORDERS_FILE) or not os.path.exists(EXCEL_FILE):
        return []
        
    orders = load_orders()
    # Active: Pending, Processing
    active_orders = [o for o in orders if o.get("status", "Pending") in ["Pending", "Processing"]]
    if not active_orders: return []
    
    # Quick Inventory Load (Only aggregate quantities)
    inv_qtys = {}
    try:
        xls = pd.ExcelFile(EXCEL_FILE)
        for sn in xls.sheet_names:
            if sn.lower() not in ("sheet1", "sheet 1"):
                sdf = pd.read_excel(xls, sheet_name=sn)
                for _, row in sdf.iterrows():
                    pc = str(row.get("Product Code", ""))
                    if pc: inv_qtys[pc] = inv_qtys.get(pc, 0) + row.get("Quantity", 0)
    except: return []
    
    shortage_ids = []
    for order in active_orders:
        for item in order.get("items", []):
            pc = item.get("product_code")
            needed = item.get("quantity", 0)
            if needed > inv_qtys.get(pc, 0):
                shortage_ids.append(order["order_id"])
                break
    return shortage_ids

# --- SIDEBAR UI ---
st.sidebar.title(f"👤 Welcome, {st.session_state.get('name', 'Staff')}")
st.sidebar.divider()

# Navigation
if st.sidebar.button("📦 View Inventory", use_container_width=True):
    st.session_state["page"] = "Inventory"
    st.rerun()

if st.sidebar.button("🔄 Update Stock", use_container_width=True):
    st.session_state["page"] = "Update Stock"
    st.session_state["transaction_type"] = None
    st.rerun()

if st.sidebar.button("📜 View History", use_container_width=True):
    st.session_state["page"] = "History"
    st.rerun()

shortage_ids = []
try:
    shortage_ids = get_orders_with_shortage()
except: pass

# WhatsApp style badge: Show Count with Red Alert if > 0
btn_label = f"📋 Orders ({len(shortage_ids)}) 🔴" if len(shortage_ids) > 0 else "📋 Orders"
        
if st.sidebar.button(btn_label, use_container_width=True):
    st.session_state["page"] = "Orders"
    st.session_state["order_mode"] = "list"
    st.rerun()

if st.session_state.get("user") == "admin":
    if st.sidebar.button("🛠️ Manage Staff", use_container_width=True):
        st.session_state["page"] = "Manage Staff"
        st.rerun()
if st.session_state.get("user") == "admin":
    if st.sidebar.button("📉 Stock Maintenance", use_container_width=True):
        st.session_state["page"] = "Stock Maintenance"
        st.rerun()

if st.session_state.get("user") == "admin":
    if st.sidebar.button("🗂️ Databases", use_container_width=True):
        st.session_state["page"] = "Databases"
        st.rerun()

if st.session_state.get("user") == "admin":
    if st.sidebar.button("📁 File Explorer", use_container_width=True):
        st.session_state["page"] = "File Explorer"
        st.rerun()

st.sidebar.divider()
if st.session_state.get("user") == "admin":
    # ☁️ CLOUD DATA ONBOARDING (Only shows if core files are missing)
    is_db_empty = not os.path.exists(DATABASES_DIR) or not any(f.endswith(".xlsx") for f in os.listdir(DATABASES_DIR))
    if not os.path.exists(EXCEL_FILE) or is_db_empty:
        with st.sidebar.expander("📂 Initial Data Setup", expanded=True):
            st.info("Setup your environment. Use the 'Databases' menu to upload price lists.")
            
            # Inventory File Setup
            if not os.path.exists(EXCEL_FILE):
                up_inv = st.file_uploader("Upload inventory.xlsx", type="xlsx", key="setup_inv")
                if up_inv:
                    with open(EXCEL_FILE, "wb") as f: f.write(up_inv.getbuffer())
                    st.success("Inventory File Created!"); st.rerun()
            
            # Databases Prompt
            if is_db_empty:
                st.warning("⚠️ No Price Lists found. Go to 'Databases' to upload your golden databases.")
                if st.button("Go to Databases"):
                    st.session_state["page"] = "Databases"
                    st.rerun()
        st.sidebar.divider()

    if st.sidebar.button("🗑️ Clear Inventory", type="secondary"):
        for f in [EXCEL_FILE, TXN_FILE, PRICE_CACHE]:
            if os.path.exists(f):
                os.remove(f)
        st.cache_data.clear()
        st.sidebar.success("Inventory & transactions cleared!")
        st.rerun()

if st.sidebar.button("Logout", use_container_width=True):
    # Safely clear persistent cookies
    try:
        cookies["inv_user"] = ""
        cookies["inv_login_time"] = ""
        cookies.save()
    except Exception:
        pass
    st.session_state["authenticated"] = False
    st.rerun()

if st.session_state.get("user") == "admin":
    with st.sidebar.expander("🛠️ OCR Diagnostic"):
        st.write("**Extracted Text:**")
        st.code(st.session_state.get("raw_ocr_code", "No extraction yet"))
        st.write("**Status:**")
        st.code(st.session_state.get("last_ocr_err", "No errors recorded"))
        if st.button("Reset Log"):
            st.session_state["raw_ocr_code"] = "None"
            st.session_state["last_ocr_err"] = "No errors recorded"
            st.rerun()

# --- CORE FUNCTIONS ---
def sanitize_quantity_strict(val):
    """Strict integer conversion for quantities."""
    try:
        # Handles strings like "5.0", " 10 ", etc.
        qty = int(float(str(val).strip()))
        return max(0, qty)
    except (ValueError, TypeError):
        return 0

def sanitize_quantity(val):
    """Safety guard: no single auto-extraction should exceed 500 units."""
    qty = sanitize_quantity_strict(val)
    if qty > 500: return 1 # Reset to 1 if it smells like a phone number
    return max(1, qty)

def validate_package_format(text):
    """Enforce 'x of y' format where y >= x."""
    if not text or text == "N/A":
        return "1 of 1"
    
    # Try to find digits separated by 'of', '/', '-', or 'out of'
    # Example: "1 of 2", "1/2", "1-2", "1 out of 2"
    match = re.search(r"(\d+)\s*(?:of|out\s*of|/|-)\s*(\d+)", str(text), re.IGNORECASE)
    if match:
        x, y = int(match.group(1)), int(match.group(2))
        if y < x: y = x # Enforce y >= x as requested
        return f"{x} of {y}"
    
    # Fallback: if it's just a single number "2", assume "1 of 2"
    digits = re.findall(r"\d+", str(text))
    if digits:
        val = int(digits[0])
        if val > 1:
            return f"1 of {val}"
    
    return "1 of 1"


def sanitize_product_code(pc):
    """Enforce 15-character 8+SD+5 format."""
    if not pc: return "N/A"
    clean = str(pc).upper().replace(" ", "").strip()
    
    # Standard format: 8 digits + SD + 5 digits
    # Regex to find these components even if 'SD' is garbled or missing
    match = re.search(r"(\d{8})[A-Z0-9]{0,2}(\d{5})", clean)
    if match:
        return f"{match.group(1)}SD{match.group(2)}"
    
    return clean

def hard_extract_math(text):
    """Primary regex for the 15-character 8+SD+5 format."""
    pc, qty = None, None
    # 1. Product Code: 8 digits, SD, 5 digits
    pc_match = re.search(r"(\d{7,9})[A-Z0-9]{1,2}(\d{4,6})", text, re.IGNORECASE)
    if pc_match:
        raw_pre, raw_suf = pc_match.group(1), pc_match.group(2)
        pc = f"{raw_pre[-8:]}SD{raw_suf[:5]}"
        
    # 2. QTY: Using \b (boundaries) to avoid matching phone numbers
    qty_match = re.search(r"(?:NET\s*QUANTITY|UNITS?|QTY)[\s:]*\b(\d+)\b", text, re.IGNORECASE)
    if qty_match: qty = sanitize_quantity(qty_match.group(1))
    return pc, qty

def repair_ocr_code(text):
    """Fuzzy repair for the 15-digit pattern if hard_extract_math missed it."""
    # Look for any sequence of 13-17 chars that smells like a code
    potential = re.findall(r"[A-Z0-9]{13,17}", text.upper())
    for p in potential:
        # If it has an 'S' or 'D' in the middle, try to force it to 8+SD+5
        m = re.search(r"(\d{7,9})[SD]{1,2}(\d{4,6})", p)
        if m:
            return f"{m.group(1)[-8:]}SD{m.group(2)[:5]}"
    return None

def sharpen_image(pil_img):
    img = np.array(pil_img)
    # Balanced sharpen: improves 8/3/1/4 accuracy without creating blooming
    kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
    return Image.fromarray(cv2.filter2D(img, -1, kernel))

def render_order_form(order_to_edit=None):
    """Reusable form for creating and editing orders."""
    st.subheader("📑 Order Details" if not order_to_edit else "📝 Edit Order")
    
    if "cart" not in st.session_state:
        st.session_state["cart"] = []
    
    # Initialize from existing order if editing
    if order_to_edit and not st.session_state.get("editing_id"):
        st.session_state["editing_id"] = order_to_edit["order_id"]
        st.session_state["cart"] = order_to_edit["items"]
        st.session_state["cust_name"] = order_to_edit["customer_name"]
        st.session_state["cust_phone"] = order_to_edit["customer_phone"]
        st.session_state["cust_address"] = order_to_edit["customer_address"]
    
    # --- 1. Cart Section ---
    with st.expander("📦 Add Items to Order", expanded=True):
        if price_list_df is not None:
            p_options = [f"{row['LN Code']} | {row['LN Description']}" for _, row in price_list_df.iterrows()]
            sel_raw = st.selectbox("Search & Select Product", [""] + p_options, key="search_ord")
            
            if sel_raw:
                p_code = sel_raw.split(" | ")[0]
                p_name = sel_raw.split(" | ")[1]
                qty = st.number_input("Purchase Quantity", min_value=1, value=1)
                
                # Inventory Check
                current_stock = 0
                if os.path.exists(EXCEL_FILE):
                    try:
                        all_df = pd.concat(pd.read_excel(EXCEL_FILE, sheet_name=None), ignore_index=True)
                        if "Product Code" in all_df.columns:
                            match = all_df[all_df["Product Code"].astype(str) == str(p_code)]
                            if not match.empty:
                                current_stock = int(match.iloc[0]["Quantity"])
                    except Exception: pass
                
                if qty > current_stock:
                    st.markdown(f"""
                        <div style="background-color: #dc2626; color: white; padding: 10px; border-radius: 5px; margin-bottom: 10px;">
                            <strong>⚠️ Low Stock:</strong> {p_name}<br>
                            Required: {qty} | InStock: {current_stock} | Short by: {qty - current_stock}
                        </div>
                    """, unsafe_allow_html=True)
                
                if st.button("➕ Add to Order"):
                    st.session_state["cart"].append({
                        "product_code": p_code,
                        "product_name": p_name,
                        "quantity": qty
                    })
                    st.toast(f"Added {p_name} to cart")
        
    # --- 2. Current Cart View ---
    if st.session_state["cart"]:
        st.write("**Items in this Order:**")
        cart_data = []
        for idx, item in enumerate(st.session_state["cart"]):
            cart_data.append({
                "Product": item["product_name"],
                "Code": item["product_code"],
                "Qty": item["quantity"]
            })
        
        st.table(cart_data)
        if st.button("🗑️ Clear Entire Cart"):
            st.session_state["cart"] = []
            st.rerun()

    # --- 3. Customer Info ---
    st.divider()
    cust_name = st.text_input("Customer Name", value=st.session_state.get("cust_name", ""))
    cust_phone = st.text_input("Customer Phone", value=st.session_state.get("cust_phone", ""))
    cust_addr = st.text_area("Delivery Address", value=st.session_state.get("cust_address", ""))

    # --- 4. Submit ---
    submit_label = "✅ Save Changes" if order_to_edit else "🚀 Create Order"
    if st.button(submit_label, type="primary"):
        if not st.session_state["cart"]:
            st.error("Please add at least one item to the order.")
        elif not cust_name:
            st.error("Customer Name is required.")
        else:
            orders = load_orders()
            if order_to_edit:
                # Update existing
                for o in orders:
                    if o["order_id"] == order_to_edit["order_id"]:
                        o.update({
                            "customer_name": cust_name,
                            "customer_phone": cust_phone,
                            "customer_address": cust_addr,
                            "items": st.session_state["cart"],
                            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })
                        break
                st.session_state["order_mode"] = "list"
            else:
                # Create new
                new_id = f"ORD-{len(orders)+1:03d}"
                orders.append({
                    "order_id": new_id,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "created_by": st.session_state["user"],
                    "customer_name": cust_name,
                    "customer_phone": cust_phone,
                    "customer_address": cust_addr,
                    "items": st.session_state["cart"],
                    "status": "Pending"
                })
            
            save_orders(orders)
            # Clear state
            for key in ["cart", "cust_name", "cust_phone", "cust_address", "editing_id"]:
                if key in st.session_state: del st.session_state[key]
            
            st.success("Order recorded successfully!")
            st.balloons()
            st.rerun()

def scan_document(pil_image):
    image = np.array(pil_image)
    if len(image.shape) == 3 and image.shape[2] == 4:
        image = cv2.cvtColor(image, cv2.COLOR_RGBA2RGB)
    
    orig = image.copy()
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    
    grad = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    _, thresh = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (50, 15)))
    closed = cv2.morphologyEx(closed, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 50)))
    
    cnts, _ = cv2.findContours(closed.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts: return pil_image
    
    # Text-Density Geometry: Find the densest text-block area
    largest = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(largest) < (image.shape[0] * image.shape[1] * 0.015): # 1.5% minimum area
        return pil_image
        
    x, y, w, h = cv2.boundingRect(largest)
    pad_y = int(h * 0.12)
    pad_x = int(w * 0.12)
    
    y1 = max(0, y - pad_y)
    y2 = min(image.shape[0], y + h + pad_y)
    x1 = max(0, x - pad_x)
    x2 = min(image.shape[1], x + w + pad_x)
    
    return Image.fromarray(orig[y1:y2, x1:x2])


def compress_image(pil_img, max_size=1280):
    """Resize image to a manageable size for mobile/OCR performance."""
    if max(pil_img.size) > max_size:
        ratio = max_size / float(max(pil_img.size))
        new_size = (int(pil_img.size[0] * ratio), int(pil_img.size[1] * ratio))
        return pil_img.resize(new_size, Image.LANCZOS)
    return pil_img

# --- CORE UTILS ---
client = Groq(api_key=GROQ_API_KEY)

def get_ocr_text(img_t):
    """Run OCR on a PIL image and return cleaned text."""
    img_byte_arr = io.BytesIO()
    img_t.save(img_byte_arr, format='JPEG', quality=85)
    files = {"file": ("image.jpg", img_byte_arr.getvalue(), "image/jpeg")}
    data = {"apikey": OCR_SPACE_API_KEY, "language": "eng", "isTable": True, "scale": True, "OCREngine": 2, "isOverlayRequired": False, "filetype": "JPG", "detectOrientation": True}
    try:
        response = requests.post("https://api.ocr.space/parse/image", files=files, data=data, timeout=20)
        if response.status_code == 200:
            ocr_result = response.json()
            if not ocr_result.get("IsErroredOnProcessing"):
                pr = ocr_result.get("ParsedResults", [])
                if pr:
                    t = pr[0].get("ParsedText", "").strip()
                    if t:
                        return re.sub(r'(?:MRP|USP)\s*[\:R]*\s*7\s+(\d+)', r'MRP \1', t, flags=re.IGNORECASE)
                else:
                    st.session_state["last_ocr_err"] = "No Parsed Results in JSON"
            else:
                st.session_state["last_ocr_err"] = ocr_result.get("ErrorMessage", "Unknown OCR processing error")
        else:
            st.session_state["last_ocr_err"] = f"OCR.space API Error {response.status_code}"
    except Exception as e:
        st.session_state["last_ocr_err"] = str(e)
    return ""

def extract_from_image(uploaded_file):
    """Multi-Stage Extraction with Full-Image Fallback."""
    try:
        uploaded_file.seek(0)
        orig_pil = Image.open(uploaded_file)
        
        # --- STAGE 1: THE CROP ---
        proc_img_crop = sharpen_image(scan_document(compress_image(orig_pil)))
        text_crop = get_ocr_text(proc_img_crop)
        res_crop = parse_and_lookup(text_crop) if text_crop else None
        
        # If Stage 1 found a valid Product In Price List, Return it
        if res_crop and res_crop.get("product_name") != "Unknown Product":
            st.session_state["last_ocr_err"] = "Scan Success (Stage: Crop)"
            return res_crop
            
        # --- STAGE 2: THE FALLBACK (Full Image) ---
        st.session_state["last_ocr_err"] = "Fallback: Trying Full Image (Product Not in List)"
        proc_img_full = sharpen_image(compress_image(orig_pil))
        text_full = get_ocr_text(proc_img_full)
        res_full = parse_and_lookup(text_full) if text_full else None
        
        if res_full:
            st.session_state["last_ocr_err"] = "Scan Success (Stage: Full Image)"
            return res_full
            
    except Exception as e:
        st.session_state["last_ocr_err"] = f"Extraction System Error: {e}"
    
    return None

def parse_and_lookup(text):
    """Parse text and verify against Golden Database using Similarity."""
    # 1. Pattern-Based Extraction
    pc, qty = hard_extract_math(text)
    if pc:
        pc = sanitize_product_code(pc) # Enforce format
    if not pc:
        pc = repair_ocr_code(text)
        if pc: pc = sanitize_product_code(pc)
    
    # 2. LLM Extraction (Strict JSON Only)
    prompt = f"JSON ONLY. Fields: product_code (8+SD+5), quantity (int), mrp (num), package (format 'x of y'). Text: {text}"
    try:
        res = client.chat.completions.create(
            messages=[{"role": "system", "content": "Return ONLY JSON with 4 keys: product_code, quantity, mrp, package. For 'package', use 'x of y' format only (e.g. '1 of 2'). Forbidden: dimensions, width, height, length, material."}, {"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant", temperature=0.1, response_format={"type": "json_object"}
        )
        ext_data = json.loads(res.choices[0].message.content)
        # Explicit clean: throw away any rogue fields
        allowed_keys = ["product_code", "quantity", "mrp", "package"]
        ext_data = {k: ext_data[k] for k in allowed_keys if k in ext_data}
    except Exception:
        ext_data = {}

    final_pc = pc if pc else ext_data.get("product_code")
    if not final_pc: return None
    
    final_pc = sanitize_product_code(final_pc)
    st.session_state["raw_ocr_code"] = final_pc # Log for diagnostics

    # 3. Golden Database Lookup & Fuzzy Similarity
    p_name = "Unknown Product"
    base_price = "N/A"
    p_category = "N/A"

    if price_list_df is not None:
        p_code_str = str(final_pc)
        # Try Exact
        matches = price_list_df[price_list_df["LN Code"] == p_code_str]
        
        # Try Fuzzy Correction (Swaps)
        if matches.empty:
            swaps = {"3": "8", "8": "3", "S": "5", "5": "S", "0": "O", "O": "0", "1": "I", "I": "1"}
            for i, char in enumerate(p_code_str):
                if char in swaps:
                    test_code = p_code_str[:i] + swaps[char] + p_code_str[i+1:]
                    matches = price_list_df[price_list_df["LN Code"] == test_code]
                    if not matches.empty:
                        final_pc = test_code
                        break

        # Try Similarity (Levenshtein) - If still not found
        if matches.empty:
            from difflib import SequenceMatcher
            best_sc = 0
            best_code = None
            # Only search nearby if the code is at least 10 chars
            if len(p_code_str) > 10:
                for db_code in price_list_df["LN Code"].head(5000): # Limit search to avoid lag
                    ratio = SequenceMatcher(None, p_code_str, db_code).ratio()
                    if ratio > 0.85 and ratio > best_sc:
                        best_sc = ratio
                        best_code = db_code
                
                if best_code:
                    final_pc = best_code
                    matches = price_list_df[price_list_df["LN Code"] == final_pc]

        if not matches.empty:
            row = matches.iloc[0]
            p_name = str(row.get("LN Description", p_name))
            base_price = str(row.get("Unit Consumer Basic", base_price))
            p_category = str(row.get("Category", p_category))
            db_mrp = row.get("MRP", "N/A")

    qty_val = qty if qty is not None else sanitize_quantity(ext_data.get("quantity", 1))
    package_val = validate_package_format(ext_data.get("package", "N/A"))
    
    # Priority for MRP: Database -> AI Extracted -> Base Price fallback
    mrp_val = ext_data.get("mrp")
    if db_mrp != "N/A":
        mrp_val = db_mrp
        
    mrp_val = clean_price(mrp_val)
    base_price = clean_price(base_price)
    
    return {
        "product_code": str(final_pc), "product_name": p_name, "quantity": qty_val,
        "mrp": mrp_val, "package": package_val, "base_price": base_price, "category": p_category
    }

# --- PAGES ---
current_page = st.session_state.get("page", "Inventory")

# 1. INVENTORY PAGE
if current_page == "Inventory":
    st.title("📦 Premium Inventory Dashboard")
    
    if os.path.exists(EXCEL_FILE):
        try:
            # Aggregate all sheets into one
            all_inv_dfs = []
            xls_display = pd.ExcelFile(EXCEL_FILE)
            for sn in xls_display.sheet_names:
                if sn.lower() not in ("sheet1", "sheet 1"):
                    sdf = pd.read_excel(xls_display, sheet_name=sn)
                    sdf["Category"] = sn
                    all_inv_dfs.append(sdf)
            
            if not all_inv_dfs:
                st.info("Inventory is empty.")
                st.stop()
                
            main_df = pd.concat(all_inv_dfs, ignore_index=True)
            thresholds = load_thresholds()
            
            # --- METRICS ---
            col1, col2, col3 = st.columns(3)
            total_sku = len(main_df["Product Code"].unique())
            total_qty = int(main_df["Quantity"].sum())
            
            # Count Low Stock items using thresholds
            low_stock_count = 0
            for _, row in main_df.iterrows():
                pc = str(row["Product Code"])
                t_val = thresholds.get(pc, 0)
                thresh = t_val.get("min", 0) if isinstance(t_val, dict) else t_val
                if thresh > 0 and row["Quantity"] < thresh: low_stock_count += 1
                
            col1.metric("Total items", total_sku)
            col2.metric("Total units", total_qty)
            col3.metric("Low Stock Alerts", low_stock_count, delta=-low_stock_count, delta_color="inverse")

            st.divider()
            
            # --- SEARCH & NAVIGATION ---
            search_query = st.text_input("🔍 Global Search", placeholder="Search by Code or Product Name in all categories...").lower()
            
            # Tabs for Categories
            categories = ["All Inventory"] + sorted(main_df["Category"].unique())
            tabs = st.tabs(categories)
            
            is_admin = st.session_state.get("user") == "admin"
            edit_mode = False
            if is_admin:
                edit_mode = st.toggle("✏️ Edit Inventory Mode", help="Enable to add, delete, or change products directly")

            for i, cat_name in enumerate(categories):
                with tabs[i]:
                    if cat_name == "All Inventory":
                        # GLOBAL VIEW (Searchable but read-only for simplicity)
                        view_df = main_df.copy()
                        if search_query:
                            view_df = view_df[
                                view_df["Product Code"].astype(str).str.lower().str.contains(search_query) |
                                view_df["Product Name"].astype(str).str.lower().str.contains(search_query)
                            ]
                        
                        if edit_mode:
                            st.info("✏️ **Global Edit Mode**: Changes made here will be synced back to their respective sheets in the Excel file (e.g., Living Room, Bedroom, etc.).")
                            edited_all_df = st.data_editor(
                                view_df, 
                                use_container_width=True, 
                                hide_index=True, 
                                num_rows="dynamic",
                                key="global_editor"
                            )
                            
                            if st.button("💾 Save All Changes Globally", type="primary", use_container_width=True):
                                with st.spinner("Syncing global changes to all sheets..."):
                                    # 1. Group edited data by Category
                                    all_sheets = {}
                                    
                                    # Handle each row based on its Category column
                                    for cat, group in edited_all_df.groupby("Category"):
                                        sn = str(cat).strip() if pd.notna(cat) else "Uncategorized"
                                        # Remove metadata column if it exists in individual sheet view
                                        sheet_data = group.drop(columns=["Category"]) if "Category" in group.columns else group
                                        all_sheets[sn] = sheet_data
                                    
                                    # 2. Add any completely missing sheets if they were in the original but not the editor
                                    original_sheets = main_df["Category"].unique()
                                    for osn in original_sheets:
                                        if osn not in all_sheets:
                                            all_sheets[osn] = main_df[main_df["Category"] == osn].drop(columns=["Category"])

                                    # 3. Save to Excel
                                    try:
                                        with pd.ExcelWriter(EXCEL_FILE, engine='openpyxl') as writer:
                                            for sn, sdf in all_sheets.items():
                                                sdf.to_excel(writer, sheet_name=sn, index=False)
                                        st.success("Global Inventory Synced!")
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"Save failed: {e}")
                        else:
                            # Apply heatmap styling for View Mode
                            def stock_heatmap_all(row):
                                pc = str(row["Product Code"])
                                t_val = thresholds.get(pc, 0)
                                thresh = t_val.get("min", 0) if isinstance(t_val, dict) else t_val
                                qty = row["Quantity"]
                                styles = [""] * len(row)
                                try:
                                    qty_idx = list(view_df.columns).index("Quantity")
                                    if qty <= 0: return ["background-color: #fee2e2; color: #991b1b"] * len(row)
                                    elif qty < thresh: styles[qty_idx] = "background-color: #fef3c7; color: #92400e; font-weight: bold"
                                except: pass
                                return styles

                            styled_all_df = view_df.style.apply(stock_heatmap_all, axis=1)
                            st.dataframe(styled_all_df, use_container_width=True, hide_index=True)
                    else:
                        # CATEGORY-SPECIFIC VIEW
                        cat_df = main_df[main_df["Category"] == cat_name].copy()
                    
                        if search_query:
                            cat_df = cat_df[
                                cat_df["Product Code"].astype(str).str.lower().str.contains(search_query) |
                                cat_df["Product Name"].astype(str).str.lower().str.contains(search_query)
                            ]
                        
                        if edit_mode:
                            st.info(f"Editing **{cat_name}** category. You can add new rows at the bottom or delete using the bin icon.")
                            # Use a unique key for each tab's editor to avoid state collisions
                            edited_cat_df = st.data_editor(
                                cat_df, 
                                use_container_width=True, 
                                hide_index=True, 
                                num_rows="dynamic",
                                column_config={
                                    "Quantity": st.column_config.NumberColumn(
                                        "Quantity",
                                        help="Must be an integer",
                                        min_value=0,
                                        step=1,
                                        format="%d",
                                    ),
                                    "Package": st.column_config.TextColumn(
                                        "Package",
                                        help="Format: x of y",
                                    ),
                                    "Product Code": st.column_config.TextColumn(
                                        "Product Code",
                                        help="Format: 8 digits + SD + 5 digits (e.g. 12345678SD12345)",
                                    )
                                },
                                key=f"editor_{cat_name}"
                            )
                            
                            if st.button(f"💾 Save Changes to {cat_name}", key=f"save_{cat_name}", type="primary"):
                                try:
                                    # Post-edit sanitization to be 100% sure
                                    edited_cat_df["Quantity"] = edited_cat_df["Quantity"].apply(sanitize_quantity_strict)
                                    if "Package" in edited_cat_df.columns:
                                        edited_cat_df["Package"] = edited_cat_df["Package"].apply(validate_package_format)
                                    if "Product Code" in edited_cat_df.columns:
                                        edited_cat_df["Product Code"] = edited_cat_df["Product Code"].apply(sanitize_product_code)

                                    # 1. Load the original Excel
                                    with pd.ExcelWriter(EXCEL_FILE, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
                                        # We only want to update THIS specific sheet
                                        # Since we are in 'replace' mode, we need to preserve OTHER sheets
                                        # But wait, openpyxl 'replace' mode is cleaner:
                                        clean_save_df = edited_cat_df.drop(columns=["Category"]) if "Category" in edited_cat_df.columns else edited_cat_df
                                        clean_save_df.to_excel(writer, sheet_name=cat_name, index=False)
                                    
                                    st.success(f"Changes for {cat_name} saved successfully!")
                                    st.balloons()
                                    st.rerun()
                                except Exception as save_err:
                                    st.error(f"Failed to save {cat_name}: {save_err}")
                        else:
                            if not cat_df.empty:
                                # Apply heatmap styling for View Mode
                                def stock_heatmap_local(row):
                                    pc = str(row["Product Code"])
                                    t_val = thresholds.get(pc, 0)
                                    thresh = t_val.get("min", 0) if isinstance(t_val, dict) else t_val
                                    qty = row["Quantity"]
                                    styles = [""] * len(row)
                                    try:
                                        qty_idx = list(cat_df.columns).index("Quantity")
                                        if qty <= 0: return ["background-color: #fee2e2; color: #991b1b"] * len(row)
                                        elif qty < thresh: styles[qty_idx] = "background-color: #fef3c7; color: #92400e; font-weight: bold"
                                    except: pass
                                    return styles

                                styled_cat_df = cat_df.style.apply(stock_heatmap_local, axis=1)
                                st.dataframe(styled_cat_df, use_container_width=True, hide_index=True)
                            else:
                                st.write("No items in this category matching your search.")


        except Exception as e:
            st.error(f"Error loading inventory dashboard: {e}")
    else:
        st.info("Inventory is empty. Start by updating stock!")

# 2. HISTORY PAGE
elif current_page == "History":
    st.title("📜 Transaction History")
    
    # 1. Date Selector
    sel_date = st.date_input("📅 Select Date", datetime.now().date())
    
    if os.path.exists(TXN_FILE):
        try:
            txn_df = pd.read_csv(TXN_FILE)
            if txn_df.empty:
                st.info("No history recorded yet.")
                st.stop()

            # Ensure minimal columns exist
            for col in ["Timestamp", "User", "Status"]:
                if col not in txn_df.columns: txn_df[col] = "N/A"

            # Filter by Date
            txn_df['Date'] = pd.to_datetime(txn_df['Timestamp']).dt.date
            day_df = txn_df[txn_df['Date'] == sel_date].copy()

            if st.session_state["user"] != "admin":
                # Staff see only their own for that day
                display_df = day_df[day_df["User"].astype(str).str.strip() == str(st.session_state["user"]).strip()].copy()
                st.info(f"Showing history for {st.session_state['name']} on {sel_date}")
            else:
                # Admin see all for that day
                display_df = day_df.copy()
                st.info(f"Administrator View: Showing all transactions for {sel_date}")
                
                # Admin Deletion Button for the day
                if not display_df.empty:
                    if st.button(f"🗑️ Clear All History for {sel_date}", type="secondary", use_container_width=True):
                        # Filter OUT the rows for this date in the ORIGINAL txn_df
                        remaining_df = txn_df[txn_df['Date'] != sel_date]
                        remaining_df.drop(columns=['Date'], inplace=True)
                        remaining_df.to_csv(TXN_FILE, index=False)
                        st.success(f"History for {sel_date} cleared!")
                        st.rerun()
            
            if display_df.empty:
                st.write(f"No transactions found for {sel_date}.")
            else:
                # Column sanitizing for sort
                sort_col = "Timestamp" if "Timestamp" in display_df.columns else display_df.columns[0]
                display_df = display_df.sort_values(sort_col, ascending=False)
                if 'Date' in display_df.columns: display_df.drop(columns=['Date'], inplace=True)

                # Style failed/partial entries
                def style_status(row):
                    color = ""
                    if str(row.get("Status", "")).lower() == "failed": color = "background-color: #ffcccc"
                    elif str(row.get("Status", "")).lower() == "partial": color = "background-color: #fff4cc"
                    return [color] * len(row)

                styled_df = display_df.style.apply(style_status, axis=1)
                st.dataframe(styled_df, use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(f"Error reading history: {e}")
    else:
        st.info("No history recorded yet.")

# 3. STOCK MAINTENANCE PAGE (Admin Only)
elif current_page == "Stock Maintenance" and st.session_state.get("user") == "admin":
    st.title("📉 Minimum Stock Maintenance")
    thresholds = load_thresholds()
    
    # 1. Add/Update Threshold Form
    st.subheader("⚙️ Set Product Threshold")
    if price_list_df is not None:
        # Create search labels: "Code | Description"
        p_options = [f"{row['LN Code']} | {row['LN Description']}" for _, row in price_list_df.iterrows()]
        sel_raw = st.selectbox("Search Product from Price List", [""] + p_options, help="Type to filter products")
        
        if sel_raw:
            p_code = sel_raw.split(" | ")[0]
            p_name = sel_raw.split(" | ")[1]
            t_val = thresholds.get(p_code, 0)
            curr_t = t_val.get("min", 0) if isinstance(t_val, dict) else t_val
            
            with st.form("Set Threshold"):
                st.write(f"Editing: **{p_name}** (`{p_code}`)")
                new_t = st.number_input("Desired Minimum Stock Level", min_value=0, value=curr_t)
                if st.form_submit_button("Confirm & Save"):
                    thresholds[p_code] = {"name": p_name, "min": new_t}
                    save_thresholds(thresholds)
                    st.success(f"Threshold for {p_code} set to {new_t}!")
                    st.rerun()

    # 2. View All Thresholds
    st.divider()
    st.subheader("📋 Minimum Stock Values")
    
    if thresholds:
        # Loop through existing thresholds to add Edit/Delete rows
        for k, v in list(thresholds.items()):
            # Canonicalize data (handle old int or new dict)
            name = v.get("name", "N/A") if isinstance(v, dict) else "N/A"
            min_val = v.get("min", 0) if isinstance(v, dict) else v
            
            row_col1, row_col2 = st.columns([3, 1])
            with row_col1:
                st.write(f"**{name}** (`{k}`): **{min_val}**")
            with row_col2:
                with st.popover("⋮"):
                    # Edit Option
                    st.write(f"Edit Threshold: **{name}**")
                    new_edit_t = st.number_input("New Min Level", min_value=0, value=min_val, key=f"edit_val_{k}")
                    if st.button("Update Value", key=f"btn_edit_{k}", use_container_width=True, type="primary"):
                        thresholds[k] = {"name": name, "min": new_edit_t}
                        save_thresholds(thresholds)
                        st.toast(f"Updated {k} to {new_edit_t}")
                        st.rerun()
                    
                    st.divider()
                    # Delete Option
                    if st.button("🗑️ Delete", key=f"btn_del_{k}", use_container_width=True, type="secondary"):
                        del thresholds[k]
                        save_thresholds(thresholds)
                        st.toast(f"Threshold for {k} deleted.")
                        st.rerun()
    else:
        st.info("No custom thresholds set. Alerting on 0 quantity only.")

# 4. ORDERS PAGE 
elif current_page == "Orders":
    st.title("📋 Order Management")
    
    mode = st.session_state.get("order_mode", "list")
    orders = load_orders()
    shortage_ids = get_orders_with_shortage()
    
    if mode == "list":
        if st.button("➕ Create New Order", type="primary"):
            st.session_state["order_mode"] = "create"
            st.rerun()
            
        st.divider()
        if not orders:
            st.info("No orders placed yet.")
        else:
            # Load Full Inventory once for detail status checks
            inv_qtys = {}
            try:
                xls_check = pd.ExcelFile(EXCEL_FILE)
                for sn in xls_check.sheet_names:
                    if sn.lower() not in ("sheet1", "sheet 1"):
                        sdf = pd.read_excel(xls_check, sheet_name=sn)
                        for _, row in sdf.iterrows():
                            pc = str(row.get("Product Code", ""))
                            if pc: inv_qtys[pc] = inv_qtys.get(pc, 0) + row.get("Quantity", 0)
            except: pass

            for idx, order in enumerate(reversed(orders)):
                is_short = order['order_id'] in shortage_ids
                status_label = " ⚠️ SHORT STOCK" if is_short else ""
                
                with st.expander(f"📦 {order['order_id']} | {order['customer_name']} | {order['timestamp']}{status_label}"):
                    if is_short:
                        st.error(f"🚨 **Attention**: One or more items in this order are currently short of stock.")
                        
                    st.write(f"**Created By:** {order['created_by']}")
                    st.write(f"**Phone:** {order['customer_phone']}")
                    st.write(f"**Address:** {order['customer_address']}")
                    st.write("**Order Items Status:**")
                    
                    for item in order["items"]:
                        p_code = str(item["product_code"])
                        p_name = item["product_name"]
                        ord_qty = item["quantity"]
                        available = inv_qtys.get(p_code, 0)
                        
                        if ord_qty > available:
                            # RED ALERT for Low Stock
                            st.markdown(f"""
                                <div style="background-color: #fee2e2; color: #991b1b; padding: 12px; border-radius: 8px; border: 1px solid #fecaca; margin-bottom: 8px;">
                                    <strong>⚠️ Stock Shortage:</strong> {p_name} (`{p_code}`)<br>
                                    Ordered: {ord_qty} units | Currently Available: {available} units | <strong>Short by: {ord_qty - available}</strong>
                                </div>
                            """, unsafe_allow_html=True)
                        else:
                            # GREEN ALERT for InStock
                            st.markdown(f"""
                                <div style="background-color: #f0fdf4; color: #166534; padding: 12px; border-radius: 8px; border: 1px solid #bbf7d0; margin-bottom: 8px;">
                                    <strong>✅ Stock OK:</strong> {p_name} (`{p_code}`)<br>
                                    Ordered: {ord_qty} units | Currently Available: {available} units
                                </div>
                            """, unsafe_allow_html=True)
                    
                    st.divider()
                    c1, c2 = st.columns(2)
                    if c1.button("📝 Edit", key=f"edit_{order['order_id']}"):
                        st.session_state["order_mode"] = "edit"
                        st.session_state["editing_order"] = order
                        st.rerun()
                    if c2.button("🗑️ Remove", key=f"rem_{order['order_id']}", type="secondary"):
                        orders.remove(order)
                        save_orders(orders)
                        st.toast(f"Removed {order['order_id']}")
                        st.rerun()
                        
    elif mode == "create":
        if st.button("⬅️ Back to List"):
            st.session_state["order_mode"] = "list"
            st.rerun()
        render_order_form()
        
    elif mode == "edit":
        if st.button("⬅️ Back to List"):
            st.session_state["order_mode"] = "list"
            if "editing_order" in st.session_state: del st.session_state["editing_order"]
            st.rerun()
        render_order_form(order_to_edit=st.session_state.get("editing_order"))

# 5. MANAGE STAFF PAGE (Admin Only)
elif current_page == "Manage Staff" and st.session_state.get("user") == "admin":
    st.title("🛠️ Manage Staff Accounts")
    
    # Reload users to ensure freshness
    current_users = load_users()
    
    # 1. List Current Staff
    st.subheader("📋 Current Accounts")
    
    for uid, info in list(current_users.items()):
        if uid == "admin": continue
        col1, col2 = st.columns([3, 1])
        with col1:
            st.write(f"**{info['name']}** (`{uid}`)")
        with col2:
            with st.popover("⋮"):
                if st.button("🗑️ Delete Account", key=f"del_{uid}", use_container_width=True, type="secondary"):
                    del current_users[uid]
                    with open("users.json", "w") as f:
                        json.dump(current_users, f, indent=2)
                    st.toast(f"Account for '{uid}' deleted.")
                    st.rerun()

    st.divider()
    
    # 2. Add New Staff member
    st.subheader("👤 Register New Staff")
    with st.form("Add User"):
        new_uid = st.text_input("Username (e.g. staff2)").strip().lower()
        new_name = st.text_input("Full Name (e.g. John Doe)").strip()
        new_pass = st.text_input("Password", type="password").strip()
        
        if st.form_submit_button("Register Account"):
            if not new_uid or not new_name or not new_pass:
                st.error("All fields are required!")
            elif new_uid in current_users:
                st.error(f"Username '{new_uid}' already exists!")
            else:
                current_users[new_uid] = {"password": new_pass, "name": new_name}
                with open("users.json", "w") as f:
                    json.dump(current_users, f, indent=2)
                st.success(f"Success! Account for {new_name} is now active.")
                st.rerun()

# 4. UPDATE STOCK PAGE
elif current_page == "Update Stock":
    st.title("🔄 Update Stock")
    
    # Step 1: Big Buttons for Selection
    if st.session_state.get("transaction_type") is None:
        c1, c2, c3 = st.columns(3)
        if c1.button("➕ INCOMING\n(Add Stock)", type="primary", use_container_width=True):
            st.session_state["transaction_type"] = "Incoming"
            st.rerun()
        if c2.button("➖ OUTGOING\n(Reduce Stock)", type="primary", use_container_width=True):
            st.session_state["transaction_type"] = "Outgoing"
            st.rerun()
        if c3.button("📋 CREATE\nORDER", type="secondary", use_container_width=True):
            st.session_state["transaction_type"] = "OrderCreation"
            st.rerun()
            
    elif st.session_state.get("transaction_type") == "OrderCreation":
        if st.button("⬅️ Back"):
            st.session_state["transaction_type"] = None
            st.rerun()
        render_order_form()
        
    else:
        transaction_type = st.session_state["transaction_type"]
        st.info(f"Mode: **{transaction_type.upper()}** (Tap 'Update Stock' in menu to change)")
        
        uploaded_files = st.file_uploader("Drop Scan/Label Images Here", type=["jpg", "png", "jpeg", "webp"], accept_multiple_files=True)

        if uploaded_files:
            # --- NEW PREVIEW SECTION ---
            st.write("📸 **Uploaded Previews:**")
            preview_cols = st.columns(4)
            for idx, uf in enumerate(uploaded_files):
                with preview_cols[idx % 4]:
                    st.image(Image.open(uf), use_container_width=True, caption=f"File {idx+1}")
            
            st.divider()
            
            if st.button("🔍 Extract & Process All", type="primary", use_container_width=True):
                # Phase 1: Scan files
                status_container = st.container()
                extracted_items = []
                
                for i, uf in enumerate(uploaded_files, 1):
                    with st.spinner(f"Processing {uf.name}..."):
                        result = extract_from_image(uf)
                    
                    col_icon, col_txt = st.columns([1, 4])
                    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    if result:
                        result["filename"] = uf.name
                        extracted_items.append(result)
                        with col_icon:
                            st.image(Image.open(uf).resize((60, 60)), use_container_width=False)
                        with col_txt:
                            st.write(f"✅ **{uf.name}**\n`{result['product_code']}`")
                    else:
                        # LOG FAILURE TO HISTORY
                        with col_icon:
                            st.error("❌")
                        with col_txt:
                            st.write(f"**{uf.name}**\n*Failed to extract*")
                        
                        log_data = pd.DataFrame([{
                            "Timestamp": current_time,
                            "User": st.session_state["user"],
                            "Type": transaction_type,
                            "Product Code": "N/A",
                            "Product Name": "Extraction Failed",
                            "Category": "N/A",
                            "Qty Diff": 0,
                            "Status": "Failed",
                            "Reason": "OCR/LLM Failed"
                        }])
                        if os.path.exists(TXN_FILE):
                            log_data.to_csv(TXN_FILE, mode='a', header=False, index=False)
                        else:
                            log_data.to_csv(TXN_FILE, index=False)

                if extracted_items:
                    # Phase 2: Process table
                    st.divider()
                    st.subheader(f"📦 Processed Product Codes ({len(extracted_items)}/{len(uploaded_files)})")
                    display_data = []
                    for item in extracted_items:
                        qty_change = item["quantity"] if transaction_type == "Incoming" else -item["quantity"]
                        display_data.append({
                            "File": item["filename"], "Product Code": item["product_code"], "Product Name": item["product_name"],
                            "Category": item["category"], "Qty Change": f"{'+' if qty_change > 0 else ''}{qty_change}",
                            "Base Price": f"₹{item['base_price']}", "MRP": item["mrp"] if item["mrp"] else "N/A", "Package": item["package"]
                        })
                    st.dataframe(pd.DataFrame(display_data), use_container_width=True, hide_index=True)

                    # Save to DB
                    with st.spinner("Recording & updating inventory..."):
                        all_sheets = {}
                        inv_cols = ["Product Code", "Product Name", "Quantity", "Base Price", "MRP", "Package"]
                        if os.path.exists(EXCEL_FILE):
                            try:
                                xls_inv = pd.ExcelFile(EXCEL_FILE)
                                for sn in xls_inv.sheet_names:
                                    if sn.lower() not in ("sheet1", "sheet 1"):
                                        all_sheets[sn] = pd.read_excel(xls_inv, sheet_name=sn)
                            except Exception: pass

                        errors = []
                        for item in extracted_items:
                            p_code = item["product_code"]
                            p_name = item["product_name"]
                            qty_change = item["quantity"] if transaction_type == "Incoming" else -item["quantity"]
                            sheet_name = item["category"] if item["category"] != "N/A" else "Uncategorized"
                            status = "Success"
                            reason = ""

                            if item["category"] == "N/A":
                                status = "Partial"
                                reason = "Not in Price List"

                            if sheet_name not in all_sheets:
                                all_sheets[sheet_name] = pd.DataFrame(columns=inv_cols)
                            df = all_sheets[sheet_name]

                            if str(p_code) in df["Product Code"].astype(str).values:
                                idx = df.index[df["Product Code"].astype(str) == str(p_code)].tolist()[0]
                                new_inv_qty = df.at[idx, "Quantity"] + qty_change
                                if new_inv_qty < 0:
                                    errors.append(f"'{p_code}': Out of stock")
                                    status = "Failed"
                                    reason = "Insufficient Stock"
                                else:
                                    df.at[idx, "Quantity"] = new_inv_qty
                                    df.at[idx, "Product Name"] = p_name
                            else:
                                if qty_change < 0:
                                    errors.append(f"'{p_code}': Not in inventory")
                                    status = "Failed"
                                    reason = "Not in Stock"
                                else:
                                    df = pd.concat([df, pd.DataFrame([{"Product Code": p_code, "Product Name": p_name, "Quantity": qty_change, "Base Price": item["base_price"], "MRP": item["mrp"], "Package": item["package"]}])], ignore_index=True)

                            all_sheets[sheet_name] = df
                            
                            # Log Transaction
                            log_data = pd.DataFrame([{
                                "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "User": st.session_state["user"],
                                "Type": transaction_type,
                                "Product Code": p_code, "Product Name": p_name, "Category": sheet_name,
                                "Qty Diff": qty_change, "Status": status, "Reason": reason
                            }])
                            if os.path.exists(TXN_FILE):
                                log_data.to_csv(TXN_FILE, mode='a', header=False, index=False)
                            else:
                                log_data.to_csv(TXN_FILE, index=False)

                        with pd.ExcelWriter(EXCEL_FILE, engine="openpyxl") as writer:
                            for sn, sdf in all_sheets.items():
                                sdf.to_excel(writer, sheet_name=sn[:31], index=False)

                    if errors:
                        for e in errors: st.error(f"⚠️ Skipped {e}")
                    st.balloons()
                    st.success(f"✅ Finished Processing!")
# 6. DATABASES PAGE (Admin Only)
elif current_page == "Databases" and st.session_state.get("user") == "admin":
    st.title("🗂️ Active Databases")
    st.info("List of 'Golden Databases' currently loaded into the lookup engine. To add or modify files, please push them to the GitHub repository.")
    
    xlsx_files = sorted([f for f in os.listdir(DATABASES_DIR) if f.endswith(".xlsx")])
    
    if not xlsx_files:
        st.warning("No price list databases found in the system. Use the GitHub repository to add files.")
    else:
        # Show Metrics 
        total_prods = len(price_list_df) if price_list_df is not None else 0
        st.metric("Total Golden Items Indexed", f"{total_prods:,}")
        
        st.divider()
        st.write("### 📜 Loaded File Registry")
        for f in xlsx_files:
            file_path = os.path.join(DATABASES_DIR, f)
            try:
                size_kb = os.path.getsize(file_path) / 1024
                st.markdown(f"📦 **{f}**  \n`Size: {size_kb:.1f} KB` | `Status: Indexed` ")
            except:
                st.markdown(f"📦 **{f}** (Status: Error reading file info)")

    st.divider()
    if st.button("🔄 Force Re-index / Clear Cache", use_container_width=True, help="Use this if you just pushed a new file to GitHub and it hasn't appeared yet."):
        if os.path.exists(PRICE_CACHE): os.remove(PRICE_CACHE)
        st.cache_data.clear()
        st.rerun()

# 7. FILE EXPLORER PAGE (Admin Only)
elif current_page == "File Explorer" and st.session_state.get("user") == "admin":
    st.title("📁 System File Explorer")
    st.info("Direct view of the Streamlit Cloud environment's disk storage. This lists all files currently present in the app's root and subdirectories.")
    
    # 1. Root Directory Scan
    root_files = []
    for root, dirs, files in os.walk("."):
        # Skip certain internal/sensitive folders
        if any(skip in root for skip in [".git", "__pycache__", ".streamlit", "brain", ".gemini"]):
            continue
            
        for f in files:
            fpath = os.path.join(root, f)
            try:
                stats = os.stat(fpath)
                root_files.append({
                    "Path": fpath,
                    "Size": f"{stats.st_size / 1024:.1f} KB",
                    "Modified": datetime.fromtimestamp(stats.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                })
            except:
                pass
                
    if root_files:
        df_files = pd.DataFrame(root_files).sort_values("Path")
        
        # Categorize by Folder
        st.subheader("📂 Files on Disk")
        st.dataframe(df_files, use_container_width=True, hide_index=True)
        
        # 2. Specific Quick Look for Critical Files
        st.divider()
        st.subheader("🔍 Critical File Status")
        crit_files = [EXCEL_FILE, TXN_FILE, PRICE_CACHE, THRESHOLD_FILE, ORDERS_FILE, "users.json"]
        cols = st.columns(len(crit_files))
        for i, cf in enumerate(crit_files):
            exists = os.path.exists(cf)
            cols[i].metric(cf, "✅ OK" if exists else "❌ Missing")
    else:
        st.warning("No files indexed.")

    st.divider()
    if st.button("🔄 Refresh View", use_container_width=True):
        st.rerun()
