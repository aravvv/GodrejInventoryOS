import pandas as pd
import os

TXN_FILE = "transactions.csv"
NEW_COLUMNS = ["Timestamp", "User", "Type", "Product Code", "Product Name", "Category", "Qty Diff", "Status", "Reason"]

if os.path.exists(TXN_FILE):
    # Read the file line by line to handle different column counts
    rows = []
    with open(TXN_FILE, "r") as f:
        # Skip original header if it exists or use it to map
        header = f.readline().strip().split(",")
        for line in f:
            parts = line.strip().split(",")
            if not parts or len(parts) < 2: continue
            
            # Map parts to the 9-column structure
            row = ["N/A"] * len(NEW_COLUMNS)
            for i in range(min(len(parts), len(NEW_COLUMNS))):
                row[i] = parts[i]
            
            # Fix column alignment if necessary (manual mapping for old 6/7 col formats)
            # Old 6: Timestamp,User,Type,Product Code,Product Name,Qty Diff
            # Old 7: Timestamp,User,Type,Product Code,Product Name,Category,Qty Diff
            # New 9: Timestamp,User,Type,Product Code,Product Name,Category,Qty Diff,Status,Reason
            
            if len(parts) == 6:
                # TS, User, Type, PC, PN, Qty
                row = [parts[0], parts[1], parts[2], parts[3], parts[4], "Uncategorized", parts[5], "Success", "Legacy"]
            elif len(parts) == 7:
                # TS, User, Type, PC, PN, Cat, Qty
                row = [parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6], "Success", "Legacy"]
            
            rows.append(row)

    df = pd.DataFrame(rows, columns=NEW_COLUMNS)
    df.to_csv(TXN_FILE, index=False)
    print("Standardized transactions.csv successfully.")
else:
    print("No transactions.csv found.")
