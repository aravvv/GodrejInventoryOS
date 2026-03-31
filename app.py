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
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)

GROQ_API_KEY = get_secret("GROQ_API_KEY")
OCR_SPACE_API_KEY = get_secret("OCR_SPACE_API_KEY", "helloworld")
COOKIE_SECRET = get_secret("COOKIE_SECRET", "godrej-inv-secret-key-2024")
PRICE_LIST_EXCEL = "priceListHomeFurniture.xlsx"
EXCEL_FILE = "inventory.xlsx"
TXN_FILE = "transactions.csv"
THRESHOLD_FILE = "thresholds.json"
ORDERS_FILE = "orders.json"

def load_thresholds():
    if os.path.exists(THRESHOLD_FILE):
        try:
            with open(THRESHOLD_FILE, "r") as f:
                return json.load(f)
        except Exception: pass
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
    """Loads users from users.json, then falls back to st.secrets, then a minimal default."""
    if os.path.exists("users.json"):
        try:
            with open("users.json", "r") as f:
                return json.load(f)
        except Exception: pass
    
    # Check Streamlit Cloud Secrets (Expected format: {"admin": {"password": "123", "name": "Admin"}})
    if "USERS" in st.secrets:
        try:
            return dict(st.secrets["USERS"])
        except Exception: pass
    
    # Minimal hardcoded fallback if everything fails (Local dev only)
    return {"admin": {"password": "123", "name": "Administrator"}}

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
    
    VALID_USERS = load_users()
    
    if not os.path.exists("users.json") and "USERS" not in st.secrets:
        st.info("💡 **Local Hint**: Using default admin credentials (123). Set 'USERS' in Streamlit Secrets for cloud deployment.")

    with st.form("Login"):
        username = st.text_input("Username").strip()
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Login"):
            if username in VALID_USERS and str(VALID_USERS[username]["password"]) == str(password):
                # Set persistent cookies (6-hour session)
                cookies["inv_user"] = username
                cookies["inv_login_time"] = datetime.now().isoformat()
                cookies.save()
                st.session_state["authenticated"] = True
                st.session_state["user"] = username
                st.session_state["name"] = VALID_USERS[username]["name"]
                st.session_state["page"] = "Inventory" if username == "admin" else "Update Stock"
                st.session_state["transaction_type"] = None
                st.rerun()
            else:
                st.error("Invalid Credentials!")
    st.stop()

# --- CACHED RESOURCES ---
PRICE_CACHE = "_price_list_cache.pkl"
PRICE_LIST_PATH = "priceListHomeFurniture.xlsx"

@st.cache_data
def load_price_list():
    """Smart loader that handles varied column names (LN Code vs Product Code)."""
    try:
        # 1. Excel File Check
        xls_path = PRICE_LIST_PATH
        if not os.path.exists(xls_path): return None
        
        # 2. Cache Check
        if os.path.exists(PRICE_CACHE):
            if os.path.getmtime(PRICE_CACHE) > os.path.getmtime(xls_path):
                return pd.read_pickle(PRICE_CACHE)
        
        # 3. Flexible Column Synonyms
        CODE_ALIAES = ["LN Code", "Product Code", "Item Code", "Code"]
        DESC_ALIAES = ["LN Description", "Product Name", "Description", "Item Name"]
        
        xls = pd.ExcelFile(xls_path)
        all_dfs = []
        for sheet_name in xls.sheet_names:
            df = pd.read_excel(xls, sheet_name=sheet_name, header=5)
            
            # Find Best Columns
            col_code = next((c for c in CODE_ALIAES if c in df.columns), None)
            col_desc = next((c for c in DESC_ALIAES if c in df.columns), None)
            
            if col_code and col_desc:
                subset = df[[col_code, col_desc]].copy()
                # Use standardized internal names
                subset.columns = ["LN Code", "LN Description"]
                subset["Unit Consumer Basic"] = df.get("Unit Consumer Basic", pd.Series(dtype="object"))
                subset["Category"] = sheet_name
                all_dfs.append(subset)
        
        if all_dfs:
            combined = pd.concat(all_dfs, ignore_index=True)
            combined = combined.dropna(subset=["LN Code"])
            combined["LN Code"] = combined["LN Code"].astype(str).str.strip()
            combined.to_pickle(PRICE_CACHE)
            return combined
        return None
    except Exception as e:
        st.session_state["last_ocr_err"] = f"Price List Loading Error: {e}"
        return None

price_list_df = load_price_list()

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

if st.sidebar.button("📋 Orders", use_container_width=True):
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
    
    with st.sidebar.expander("📂 Initial Data Setup", expanded=False):
        st.info("Upload your local files to populate the live site.")
        
        # 1. Inventory Upload
        up_inv = st.file_uploader("Upload inventory.xlsx", type="xlsx", key="up_inv")
        if up_inv:
            with open(EXCEL_FILE, "wb") as f:
                f.write(up_inv.getbuffer())
            st.success("Inventory uploaded!")
            st.rerun()
            
        # 2. Price List Upload
        up_price = st.file_uploader("Upload priceListHomeFurniture.xlsx", type="xlsx", key="up_price")
        if up_price:
            with open(PRICE_LIST_PATH, "wb") as f:
                f.write(up_price.getbuffer())
            # Clear cache for the new price list
            if os.path.exists(PRICE_CACHE): os.remove(PRICE_CACHE)
            st.cache_data.clear()
            st.success("Price list uploaded!")
            st.rerun()

st.sidebar.divider()
if st.session_state.get("user") == "admin":
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
def sanitize_quantity(val):
    """Safety guard: no single auto-extraction should exceed 500 units."""
    try:
        qty = int(val)
        if qty > 500: return 1 # Reset to 1 if it smells like a phone number
        return max(1, qty)
    except Exception:
        return 1

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
    if max(pil_img.size) > max_size:
        ratio = max_size / float(max(pil_img.size))
        new_size = (int(pil_img.size[0] * ratio), int(pil_img.size[1] * ratio))
        return pil_img.resize(new_size, Image.LANCZOS)
    return pil_img

# --- CORE UTILS ---
client = Groq(api_key=GROQ_API_KEY)

def get_ocr_text(img_t):
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
                else: st.session_state["last_ocr_err"] = "No Parsed Results"
            else: st.session_state["last_ocr_err"] = ocr_result.get("ErrorMessage", "OCR Error")
        else: st.session_state["last_ocr_err"] = f"API Error {response.status_code}"
    except Exception as e: st.session_state["last_ocr_err"] = str(e)
    return ""

def extract_from_image(uploaded_file):
    try:
        uploaded_file.seek(0)
        orig_pil = Image.open(uploaded_file)
        proc_img_crop = sharpen_image(scan_document(compress_image(orig_pil)))
        text_crop = get_ocr_text(proc_img_crop)
        res_crop = parse_and_lookup(text_crop) if text_crop else None
        if res_crop and res_crop.get("product_name") != "Unknown Product":
            st.session_state["last_ocr_err"] = "Scan Success (Stage: Crop)"
            return res_crop
        proc_img_full = sharpen_image(compress_image(orig_pil))
        text_full = get_ocr_text(proc_img_full)
        res_full = parse_and_lookup(text_full) if text_full else None
        if res_full:
            st.session_state["last_ocr_err"] = "Scan Success (Stage: Full Image)"
            return res_full
    except Exception as e: st.session_state["last_ocr_err"] = str(e)
    return None

def parse_and_lookup(text):
    pc, qty = hard_extract_math(text)
    if not pc: pc = repair_ocr_code(text)
    prompt = f"JSON ONLY. Fields: product_code, quantity (int), mrp (num), package. Text: {text}"
    try:
        res = client.chat.completions.create(
            messages=[{"role": "system", "content": "Return ONLY JSON with 4 keys: product_code, quantity, mrp, package."}, {"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant", temperature=0.1, response_format={"type": "json_object"}
        )
        ext_data = json.loads(res.choices[0].message.content)
    except Exception: ext_data = {}
    final_pc = pc if pc else ext_data.get("product_code")
    if not final_pc: return None
    final_pc = str(final_pc).upper().replace(" ", "").strip()
    st.session_state["raw_ocr_code"] = final_pc 
    p_name, base_price, p_category = "Unknown Product", "N/A", "N/A"
    if price_list_df is not None:
        p_code_str = str(final_pc)
        matches = price_list_df[price_list_df["LN Code"] == p_code_str]
        if matches.empty:
            swaps = {"3": "8", "8": "3", "S": "5", "5": "S", "0": "O", "O": "0", "1": "I", "I": "1"}
            for i, char in enumerate(p_code_str):
                if char in swaps:
                    test_code = p_code_str[:i] + swaps[char] + p_code_str[i+1:]
                    matches = price_list_df[price_list_df["LN Code"] == test_code]
                    if not matches.empty:
                        final_pc = test_code
                        break
        if not matches.empty:
            row = matches.iloc[0]
            p_name = str(row.get("LN Description", p_name))
            base_price = str(row.get("Unit Consumer Basic", base_price))
            p_category = str(row.get("Category", p_category))
    qty_val = qty if qty is not None else sanitize_quantity(ext_data.get("quantity", 1))
    return {"product_code": str(final_pc), "product_name": p_name, "quantity": qty_val, "mrp": ext_data.get("mrp"), "package": ext_data.get("package", "N/A"), "base_price": base_price, "category": p_category}

# --- PAGES ---
current_page = st.session_state.get("page", "Inventory")

# 1. INVENTORY PAGE
if current_page == "Inventory":
    st.title("📦 Premium Inventory Dashboard")
    if os.path.exists(EXCEL_FILE):
        try:
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
            col1, col2, col3 = st.columns(3)
            total_sku = len(main_df["Product Code"].unique())
            total_qty = int(main_df["Quantity"].sum())
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
            search_query = st.text_input("🔍 Global Search", placeholder="Search by Code...").lower()
            categories = ["All Inventory"] + sorted(main_df["Category"].unique())
            tabs = st.tabs(categories)
            is_admin = st.session_state.get("user") == "admin"
            edit_mode = st.toggle("✏️ Edit Inventory Mode") if is_admin else False
            for i, cat_name in enumerate(categories):
                with tabs[i]:
                    if cat_name == "All Inventory":
                        view_df = main_df.copy()
                        if search_query: view_df = view_df[view_df["Product Code"].astype(str).str.lower().str.contains(search_query) | view_df["Product Name"].astype(str).str.lower().str.contains(search_query)]
                        def stock_heatmap_all(row):
                            pc, qty = str(row["Product Code"]), row["Quantity"]
                            t_val = thresholds.get(pc, 0)
                            thresh = t_val.get("min", 0) if isinstance(t_val, dict) else t_val
                            if qty <= 0: return ["background-color: #fee2e2"]*len(row)
                            elif qty < thresh: return ["background-color: #fef3c7"]*len(row)
                            return [""]*len(row)
                        st.dataframe(view_df.style.apply(stock_heatmap_all, axis=1), use_container_width=True, hide_index=True)
                    else:
                        cat_df = main_df[main_df["Category"] == cat_name].copy()
                        if search_query: cat_df = cat_df[cat_df["Product Code"].astype(str).str.lower().str.contains(search_query)]
                        if edit_mode:
                            edited_cat_df = st.data_editor(cat_df, use_container_width=True, hide_index=True, num_rows="dynamic", key=f"ed_{cat_name}")
                            if st.button(f"💾 Save {cat_name}", key=f"sv_{cat_name}"):
                                try:
                                    with pd.ExcelWriter(EXCEL_FILE, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
                                        clean_df = edited_cat_df.drop(columns=["Category"]) if "Category" in edited_cat_df.columns else edited_cat_df
                                        clean_df.to_excel(writer, sheet_name=cat_name, index=False)
                                    st.success("Saved!"); st.rerun()
                                except Exception as e: st.error(str(e))
                        else:
                            st.dataframe(cat_df, use_container_width=True, hide_index=True)
        except Exception as e: st.error(f"Load Error: {e}")
    else:
        st.warning("📊 No Inventory Found. Please upload 'inventory.xlsx'.")
        st.stop()

# 2. HISTORY PAGE
elif current_page == "History":
    st.title("📜 Transaction History")
    sel_date = st.date_input("📅 Date", datetime.now().date())
    if os.path.exists(TXN_FILE):
        try:
            txn_df = pd.read_csv(TXN_FILE)
            txn_df['Date'] = pd.to_datetime(txn_df['Timestamp']).dt.date
            day_df = txn_df[txn_df['Date'] == sel_date]
            display_df = day_df if st.session_state["user"] == "admin" else day_df[day_df["User"] == st.session_state["user"]]
            st.dataframe(display_df, use_container_width=True, hide_index=True)
        except Exception as e: st.error(str(e))
    else: st.info("No records.")

# 4. ORDERS PAGE 
elif current_page == "Orders":
    st.title("📋 Orders")
    mode = st.session_state.get("order_mode", "list")
    orders = load_orders()
    if mode == "list":
        if st.button("➕ New Order"): st.session_state["order_mode"] = "create"; st.rerun()
        for order in reversed(orders):
            with st.expander(f"Order {order['order_id']} - {order['customer_name']}"):
                st.write(order["items"])
                if st.button("🗑️ Delete", key=f"del_{order['order_id']}"): orders.remove(order); save_orders(orders); st.rerun()
    elif mode == "create":
        if st.button("⬅️ Back"): st.session_state["order_mode"] = "list"; st.rerun()
        render_order_form()

# 5. MANAGE STAFF PAGE
elif current_page == "Manage Staff" and st.session_state.get("user") == "admin":
    st.title("🛠️ Staff")
    users = load_users()
    st.write(users)
    with st.form("Add Staff"):
        u, n, p = st.text_input("UID"), st.text_input("Name"), st.text_input("Pass")
        if st.form_submit_button("Add"):
            users[u] = {"password":p, "name":n}
            if os.path.exists("users.json"):
                with open("users.json", "w") as f: json.dump(users, f)
                st.success("Staff added locally.")
            else: st.info("Users file missing. Add to Cloud Secrets instead.")

# 4. UPDATE STOCK PAGE
elif current_page == "Update Stock":
    st.title("🔄 Update Stock")
    if st.session_state.get("transaction_type") is None:
        c1, c2 = st.columns(2)
        if c1.button("➕ INCOMING"): st.session_state["transaction_type"] = "Incoming"; st.rerun()
        if c2.button("➖ OUTGOING"): st.session_state["transaction_type"] = "Outgoing"; st.rerun()
    else:
        if st.button("⬅️ Back"): st.session_state["transaction_type"] = None; st.rerun()
        st.write(f"Mode: {st.session_state['transaction_type']}")
        files = st.file_uploader("Upload", accept_multiple_files=True)
        if files and st.button("Process"):
            for f in files:
                res = extract_from_image(f)
                if res: st.write(f"Processed {res['product_code']}")
