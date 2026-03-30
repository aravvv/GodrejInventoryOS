import pandas as pd
import json
df = pd.read_excel('priceListHomeFurniture.xlsx')
cols = df.columns.tolist()
head = df.head(10).fillna("").to_dict('records')
with open('info.json', 'w') as f:
    json.dump({'columns': cols, 'head': head}, f, indent=2)
