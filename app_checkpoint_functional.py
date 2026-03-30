import streamlit as st
import os
import requests
import json
import pandas as pd
from groq import Groq
from dotenv import load_dotenv
from PIL import Image
import io
import cv2
import numpy as np
import re

# --- ADVANCED HYBRID PARSING (REGEX FALLBACK) ---
def hard_extract_math(text):
    pc, qty = None, None
    pc_match = re.search(r"PRODUCT\s*CODE[\s:]*([A-Z0-9]{8,})", text, re.IGNORECASE)
    if pc_match: pc = pc_match.group(1)
    qty_match = re.search(r"(?:NET\s*QUANTITY|UNITS?|QTY)[\s:]*(\d+)", text, re.IGNORECASE)
    if qty_match: qty = int(qty_match.group(1))
    return pc, qty

def sharpen_image(pil_img):
    img = np.array(pil_img)
    # Intense 3x3 sharpening kernel to force edge contrast mathematically
    kernel = np.array([[0, -1, 0], 
                       [-1, 5, -1], 
                       [0, -1, 0]])
    sharpened = cv2.filter2D(img, -1, kernel)
    return Image.fromarray(sharpened)

# --- COMPUTER VISION DOCUMENT SCANNER ---
def scan_document(pil_image):
    image = np.array(pil_image)
    if len(image.shape) == 3 and image.shape[2] == 4:
        image = cv2.cvtColor(image, cv2.COLOR_RGBA2RGB)
    
    orig = image.copy()
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    
    # 1. Text-Density Geometry (Colorblind Text Tracking)
    # Extract edges to isolate typography regardless of label color or physical tape
    grad = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    _, thresh = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    
    # 2. Forge the text map. 
    # Massive horizontal and vertical kernels literally bleed all adjacent letters and rows together into a huge solid plateau.
    # We use a 50x15 horizontal kernel to fuse columns, and a 15x50 vertical to fuse lines!
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (50, 15)))
    closed = cv2.morphologyEx(closed, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 50)))
    
    cnts, _ = cv2.findContours(closed.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts: return pil_image
    
    # 3. Target the single largest plateau of text (The Godrej Label)
    largest = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(largest) < (image.shape[0] * image.shape[1] * 0.02):
        return pil_image # Failsafe: if plateaus are absurdly tiny, it's not a label.
        
    # 4. Strictly flat 2D bounding box (Bypassing 3D Perspective Squashing bugs)
    x, y, w, h = cv2.boundingRect(largest)
    
    # 5. Lock coordinates exactly to the text bounding box (0% padding)
    pad_y = 0
    pad_x = 0
    
    y1 = max(0, y - pad_y)
    y2 = min(image.shape[0], y + h + pad_y)
    x1 = max(0, x - pad_x)
    x2 = min(image.shape[1], x + w + pad_x)
    
    cropped = orig[y1:y2, x1:x2]
    return Image.fromarray(cropped)

# --- CONFIGURATION ---
st.set_page_config(page_title="AI Inventory Extractor", page_icon="📦")
st.title("📦 AI Inventory Extractor")
st.info("Upload an image to extract product details and save them to Excel.")

# --- API KEYS ---
load_dotenv()
OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY", "helloworld")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not GROQ_API_KEY:
    st.error("⚠️ `GROQ_API_KEY` is not set. Please add it to your `.env` file in this directory.")
    st.stop()

# Initialize API Clients
client = Groq(api_key=GROQ_API_KEY)
EXCEL_FILE = "inventory.xlsx"

# --- UI WORKFLOW ---
uploaded_file = st.file_uploader("Drop Image Here", type=["jpg", "png", "jpeg", "webp"])

if uploaded_file is not None:
    # 1. Immediate OpenCV Auto-Crop & Sharpen
    with st.spinner("Isolating Document from Background..."):
        cropped_img = sharpen_image(scan_document(Image.open(uploaded_file)))
        
    st.image(cropped_img, caption="Isolated & Sharpened Document Preview", width=400)
    
    # Process Buttons
    extract_btn = st.button("Extract Data & Save to Excel", type="primary", use_container_width=True)

    if extract_btn:
        def get_ocr_text(img_t, prefix=""):
            ext_text = ""
            max_r = 3
            sf = 1.10
            for attempt in range(max_r + 1):
                if attempt > 0:
                    with st.spinner(f"{prefix}Zooming 10% (Attempt {attempt}/{max_r})..."):
                        img_t = img_t.resize((int(img_t.width * sf), int(img_t.height * sf)), Image.Resampling.LANCZOS)
                with st.spinner(f"{prefix}Reading OCR..."):
                    img_byte_arr = io.BytesIO()
                    img_t.save(img_byte_arr, format='PNG')
                    files = {"file": ("image.png", img_byte_arr.getvalue(), "image/png")}
                    data = {"apikey": OCR_SPACE_API_KEY, "language": "eng", "isTable": True, "scale": True, "OCREngine": 2, "isOverlayRequired": False}
                    try:
                        response = requests.post("https://api.ocr.space/parse/image", files=files, data=data, timeout=15)
                        if response.status_code == 200:
                            ocr_result = response.json()
                            if not ocr_result.get("IsErroredOnProcessing"):
                                pr = ocr_result.get("ParsedResults", [])
                                if pr:
                                    t = pr[0].get("ParsedText", "").strip()
                                    if t:
                                        ext_text = t
                                        break
                    except: pass
            return ext_text
            
        # 1. Competitive Extraction Pipelines
        col_c, col_r = st.columns(2)
        with col_c:
            text_crop = get_ocr_text(cropped_img.copy(), "[Cropped Scan] ")
        with col_r:
            text_raw = get_ocr_text(sharpen_image(Image.open(uploaded_file)), "[Raw Photo] ")
            
        # 2. Score Both Pipelines via Rigid Regex Engine
        c_pc, c_qty = hard_extract_math(text_crop)
        r_pc, r_qty = hard_extract_math(text_raw)
        
        score_crop = (2 if c_pc else 0) + (2 if c_qty is not None else 0)
        score_raw = (2 if r_pc else 0) + (2 if r_qty is not None else 0)
        
        # 3. Declare Winner
        if score_crop >= score_raw:
            extracted_text = text_crop
            img = cropped_img.copy()
            st.success(f"🏆 Winner: Cropped Document (Regex Score: {score_crop} vs {score_raw})")
        else:
            extracted_text = text_raw
            img = Image.open(uploaded_file)
            st.warning(f"🏆 Winner: Raw Image! Automatic CV crop missed vital numbers (Regex Score: {score_raw} vs {score_crop})")
            
        if not extracted_text:
            st.error("Text could not be extracted from fundamentally either image via OCR.Space.")
            st.stop()
            
        with st.expander("👀 View Winning Raw OCR Text"):
            st.text(extracted_text)
                
        # 2. Groq AI Interpretation
        with st.spinner("Step 2: Parsing Variables using AI..."):
            prompt = f"""Extract the following fields from the text:
- product_code: The exact alphanumeric code (e.g., '30161803SD01708' next to 'PRODUCT CODE')
- product_name: The complete, full descriptive name of the item encompassing multiple lines if necessary (e.g., 'WARDROBE STORWEL ACE 2DR FULLLK RUSSETT')
- quantity: Just the numerical value for 'NET QUANTITY' or 'UNITS' (e.g., if '1 UNIT', return 1)
- mrp: Just the numerical price strictly next to 'MRP' (Do NOT extract the 'USP' value)
- package: The package count (e.g., '1 OF 1')

Rules:
* Return ONLY valid JSON
* Try to match fields even if the layout is messy (e.g. table cells might be read out of order)
* If a field is missing, return null

Example Context 1:
Text: "PRODUCT CODE 30161803SD01111 PRODUCT WARDROBE KREX3 DR BDL NET QUANTITY 1 UNIT MRP 7652 USP 7652.00 PACKAGE 1 OF 1"
Output: {{"product_code": "30161803SD01111", "product_name": "WARDROBE KREX3 DR BDL", "quantity": 1, "mrp": 7652, "package": "1 OF 1"}}

Example Context 2:
Text: "MARKETED & MANUFACTURED BY PRODUCT CODE 30161803SD01708 PRODUCT WARDROBE STORWEL ACE 2DR FULLLK RUSSETT NET QUANTITY 1 UNIT MRP 27376 (INCL) USP 27376.00 PACKAGE 1 OF 1"
Output: {{"product_code": "30161803SD01708", "product_name": "WARDROBE STORWEL ACE 2DR FULLLK RUSSETT", "quantity": 1, "mrp": 27376, "package": "1 OF 1"}}

Text:
{extracted_text}
"""
            try:
                chat_completion = client.chat.completions.create(
                    messages=[
                        {"role": "system", "content": "Return valid JSON ONLY. No markdown wrapping. No explanations."},
                        {"role": "user", "content": prompt}
                    ],
                    model="llama-3.1-8b-instant",
                    temperature=0.1,
                    response_format={"type": "json_object"}
                )
                
                llm_response = chat_completion.choices[0].message.content
                extracted_data = json.loads(llm_response)
                
            except json.JSONDecodeError:
                st.error("AI returned malformed JSON payload.")
                st.stop()
            except Exception as e:
                st.error(f"Groq API Error: {str(e)}")
                st.stop()

        # Data Cleaning + Two-Tier Merge
        regex_pc, hard_qty = hard_extract_math(extracted_text)
        
        product_code = regex_pc if regex_pc else extracted_data.get("product_code")
        product_name = extracted_data.get("product_name")
        package_label = extracted_data.get("package")
        
        # We need a primary key (product_code) to ensure inventory logic succeeds
        if not product_code:
            st.warning("⚠️ Initial pass missed 'product_code'. Initiating Advanced Regional Crop & Zoom...")
            import time
            
            # Quadrant Slicing Algorithm
            width, height = img.size
            w = int(width * 0.6) # 60% of width (20% overlap)
            h = int(height * 0.6)
            
            quadrants = [
                img.crop((0, 0, w, h)), 
                img.crop((width - w, 0, width, h)),
                img.crop((0, height - h, w, height)),
                img.crop((width - w, height - h, width, height))
            ]
            
            combined_text = ""
            for i, quad in enumerate(quadrants):
                with st.spinner(f"Scanning Cropped Region {i+1}/4..."):
                    # Zoom each cropped section by 1.1x internally
                    zoomed = quad.resize((int(w * 1.1), int(h * 1.1)), Image.Resampling.LANCZOS)
                    img_byte_arr = io.BytesIO()
                    zoomed.save(img_byte_arr, format='PNG')
                    
                    files = {"file": (f"quad_{i}.png", img_byte_arr.getvalue(), "image/png")}
                    data = {"apikey": OCR_SPACE_API_KEY, "language": "eng", "isTable": True, "scale": True, "OCREngine": 2, "isOverlayRequired": False}
                    time.sleep(1) # Prevent OCR.space free tier rate limiting
                    
                    try:
                        quad_resp = requests.post("https://api.ocr.space/parse/image", files=files, data=data, timeout=15)
                        if quad_resp.status_code == 200:
                            q_json = quad_resp.json()
                            if not q_json.get("IsErroredOnProcessing"):
                                q_res = q_json.get("ParsedResults", [])
                                if q_res:
                                    t = q_res[0].get("ParsedText", "").strip()
                                    if t: combined_text += t + "\n"
                    except:
                        pass
            
            if not combined_text.strip():
                st.error("Fallback Failed! Zero text was readable from any zoomed crop.")
                st.stop()
                
            with st.expander("👀 View Raw OCR Text (Fallback Crops)"):
                st.text(combined_text)
                
            # Re-Evaluate merged dense text via LLM
            with st.spinner("Re-evaluating overlapping crops with AI..."):
                prompt = f"""Extract the following structured fields from the merged cropped zoomed quadrants:
- product_code: The exact alphanumeric code (e.g., '30161803SD01708' next to 'PRODUCT CODE')
- product_name: The complete, full descriptive name of the item encompassing multiple lines if necessary (e.g., 'WARDROBE STORWEL ACE 2DR FULLLK RUSSETT')
- quantity: Just the numerical value for 'NET QUANTITY' or 'UNITS' (e.g., if '1 UNIT', return 1)
- mrp: Just the numerical price strictly next to 'MRP' (Do NOT extract the 'USP' value)
- package: The package count (e.g., '1 OF 1')

Rules:
* Return ONLY valid JSON
* Try to match fields even if the layout is messy (e.g. table cells might be read out of order)
* If a field is missing, return null

Example Context 1:
Text: "PRODUCT CODE 30161803SD01111 PRODUCT WARDROBE KREX3 DR BDL NET QUANTITY 1 UNIT MRP 7652 USP 7652.00 PACKAGE 1 OF 1"
Output: {{"product_code": "30161803SD01111", "product_name": "WARDROBE KREX3 DR BDL", "quantity": 1, "mrp": 7652, "package": "1 OF 1"}}

Example Context 2:
Text: "MARKETED & MANUFACTURED BY PRODUCT CODE 30161803SD01708 PRODUCT WARDROBE STORWEL ACE 2DR FULLLK RUSSETT NET QUANTITY 1 UNIT MRP 27376 (INCL) USP 27376.00 PACKAGE 1 OF 1"
Output: {{"product_code": "30161803SD01708", "product_name": "WARDROBE STORWEL ACE 2DR FULLLK RUSSETT", "quantity": 1, "mrp": 27376, "package": "1 OF 1"}}

Text:
{combined_text}
"""
                try:
                    chat_completion = client.chat.completions.create(
                        messages=[{"role": "system", "content": "Return valid JSON ONLY. No markdown wrapping."}, {"role": "user", "content": prompt}],
                        model="llama-3.1-8b-instant",
                        temperature=0.1,
                        response_format={"type": "json_object"}
                    )
                    extracted_data = json.loads(chat_completion.choices[0].message.content)
                    
                    reg_pc, hard_qty = hard_extract_math(combined_text)
                    product_code = reg_pc if reg_pc else extracted_data.get("product_code")
                    product_name = extracted_data.get("product_name")
                    package_label = extracted_data.get("package")
                except:
                    pass
        
        # Second Validation Stop
        if not product_code:
            st.warning("⚠️ Exhausted all deep zooms. Missing 'product_code'. Saving all other discovered fields with a NULL code!")
            with st.expander("Show AI Raw Data Attempt"):
                st.json(extracted_data)
        
        # Clean QTY Let default = 1
        try:
            if 'hard_qty' in locals() and hard_qty is not None:
                qty = int(hard_qty)
            else:
                qty = int(extracted_data.get("quantity")) if extracted_data.get("quantity") is not None else 1
        except (ValueError, TypeError):
            qty = 1
            
        # Clean MRP
        mrp_raw = extracted_data.get("mrp")
        mrp = None
        if mrp_raw is not None:
            try:
                if isinstance(mrp_raw, str):
                    mrp = float(mrp_raw.replace('$', '').replace('€', '').replace('£', '').replace(',', '').strip())
                else:
                    mrp = float(mrp_raw)
            except (ValueError, TypeError):
                mrp = None

        # Display Final Parsed Values explicitly
        st.success("Extraction Successful!")
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Code", str(product_code))
        col2.metric("Name", str(product_name))
        col3.metric("Qty", qty)
        col4.metric("MRP", f"${mrp:.2f}" if mrp is not None else "N/A")
        col5.metric("Package", str(package_label) if package_label is not None else "N/A")

        # 3. Excel Database Upsert Logic
        with st.spinner("Step 3: Storing record in Excel (.xlsx)..."):
            new_record = pd.DataFrame([{
                "Product Code": product_code,
                "Product Name": product_name,
                "Quantity": qty,
                "MRP": mrp,
                "Package": package_label
            }])
            
            if os.path.exists(EXCEL_FILE):
                try:
                    df = pd.read_excel(EXCEL_FILE)
                    
                    # Logic: If item exists, increment quantity
                    if product_code and str(product_code) != "nan" and product_code in df["Product Code"].values:
                        idx = df.index[df["Product Code"] == product_code].tolist()[0]
                        df.at[idx, "Quantity"] += qty
                        
                        # Populate blanks if we have fresh data
                        if product_name and pd.isna(df.at[idx, "Product Name"]):
                            df.at[idx, "Product Name"] = product_name
                        if mrp and pd.isna(df.at[idx, "MRP"]):
                            df.at[idx, "MRP"] = mrp
                            
                        if "Package" not in df.columns:
                            df["Package"] = None
                        if package_label and pd.isna(df.at[idx, "Package"]):
                            df.at[idx, "Package"] = package_label
                            
                        st.info(f"✅ Found existing item '{product_code}', incremented quantity to {df.at[idx, 'Quantity']}.")
                    else:
                        # Direct Append
                        df = pd.concat([df, new_record], ignore_index=True)
                        st.info(f"✅ Added brand new inventory item '{product_code}'.")
                        
                    df.to_excel(EXCEL_FILE, index=False)
                except Exception as e:
                    st.error(f"Excel read/write failed. Is the file open in Excel? Close it and try again. Error: {e}")
                    st.stop()
            else:
                # Direct Create
                try:
                    new_record.to_excel(EXCEL_FILE, index=False)
                    st.info(f"✅ Created a brand new `{EXCEL_FILE}` tracking sheet and inserted '{product_code}'.")
                except Exception as e:
                    st.error(f"Error creating excel sheet: {e}")
                    st.stop()
                    
            st.balloons()

# --- EXCEL INVENTORY DISPLAY ---
if os.path.exists(EXCEL_FILE):
    st.divider()
    
    col_header, col_btn = st.columns([0.8, 0.2])
    with col_header:
        st.subheader("📚 Live Local Inventory")
    with col_btn:
        if st.button("🗑️ Clear Inventory", use_container_width=True):
            try:
                os.remove(EXCEL_FILE)
                st.success("Inventory cleared successfully!")
                st.rerun()
            except Exception as e:
                st.error(f"Failed to clear: {e}. Close the Excel file securely first.")
                
    if os.path.exists(EXCEL_FILE):
        try:
            final_df = pd.read_excel(EXCEL_FILE)
            st.dataframe(final_df, use_container_width=True, hide_index=True)
        except:
            st.warning(f"Could not load the `{EXCEL_FILE}` preview. Is the file open or corrupted?")
