import pandas as pd
import os

TXN_FILE = "transactions.csv"
NEW_COLUMNS = ["Timestamp", "User", "Type", "Product Code", "Product Name", "Category", "Qty Diff", "Status", "Reason"]

if os.path.exists(TXN_FILE):
    try:
        # Read the file with minimal assumptions about column counts
        df = pd.read_csv(TXN_FILE, on_bad_lines='skip')
        
        # Ensure all columns exist
        for col in NEW_COLUMNS:
            if col not in df.columns:
                df[col] = "Legacy" if col in ("Status", "Reason") else "N/A"
        
        # Select and reorder columns
        df = df[NEW_COLUMNS]
        df.to_csv(TXN_FILE, index=False)
        print("Standardized transactions.csv successfully via pandas.")
    except Exception as e:
        print(f"Pandas fix failed: {e}")
else:
    print("No transactions.csv found.")
