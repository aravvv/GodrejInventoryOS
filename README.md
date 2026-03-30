# AI Inventory Extractor (UI Dashboard)

A simple click-and-play dashboard that allows you to easily extract details off inventory images and cleanly save them into a local Excel file `inventory.xlsx`.

## Setup & Running 

The dashboard runs right from your browser automatically using Streamlit.

1. Install project dependencies in your directory:
   ```powershell
   cd d:\inventoryOS
   pip install -r requirements.txt
   ```

2. Configure your Personal API keys
   Make a copy of `.env.example` called `.env` in this directory:
   ```powershell
   copy .env.example .env
   ```
   Add your keys directly into the file. The OCR key defaults to the free test limit, but your `GROQ_API_KEY` is required.

3. Launch Application
   ```powershell
   streamlit run app.py
   ```
   The UI will open securely inside your browser natively at `http://localhost:8501`.
