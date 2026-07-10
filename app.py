import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import yfinance as yf
import logging
import re
import io
import requests

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --- SET CONFIGURATION ---
st.set_page_config(page_title="XTB Dashboard", page_icon="💰", layout="wide")

st.title("💰 Prywatny Dashboard Finansowy XTB")
st.markdown("---")

# --- BACKEND LOGIC ---
def parse_xtb_comment(row: pd.Series) -> tuple:
    comment = row.get("Comment")
    tx_type = row.get("Type")
    if not isinstance(comment, str): return None, None
    pattern = r"(?:OPEN|CLOSE)\s+(?:BUY|SELL)\s+([\d.]+)(?:/[\d.]+)?\s+@\s+([\d.]+)"
    match = re.search(pattern, comment)
    if match:
        volume = float(match.group(1))
        price = float(match.group(2))
        if tx_type == "Stock purchase": return volume, price
        elif tx_type == "Stock sell": return -volume, price
    return None, None

def calculate_accurate_portfolio(df: pd.DataFrame) -> tuple:
    asset_types = ["Stock purchase", "Stock sell"]
    trade_df = df[df["Type"].isin(asset_types)].copy()
    if trade_df.empty: 
        return pd.DataFrame(), 0.0
    
    parsed_data = trade_df.apply(parse_xtb_comment, axis=1)
    trade_df["Volume_Adjusted"] = [x[0] if x is not None else None for x in parsed_data]
    trade_df["Price"] = [x[1] if x is not None else None for x in parsed_data]
    
    portfolio = trade_df.groupby("Ticker").agg(
        Shares_Owned=("Volume_Adjusted", "sum"),
        Net_Cash_Flow=("Amount", "sum")
    ).reset_index()
    portfolio["Shares_Owned"] = portfolio["Shares_Owned"].round(6)
    active_portfolio = portfolio[portfolio["Shares_Owned"] > 0].copy()
    active_portfolio["Total_Invested_Raw"] = -active_portfolio["Net_Cash_Flow"]
    
    realized_df = portfolio[portfolio["Shares_Owned"] <= 0].copy()
    realized_pnl = realized_df["Net_Cash_Flow"].sum() if not realized_df.empty else 0.0
    
    for idx, row in portfolio[portfolio["Shares_Owned"] > 0].iterrows():
        if row["Net_Cash_Flow"] > 0:
            realized_pnl += row["Net_Cash_Flow"]

    return active_portfolio.drop(columns=["Net_Cash_Flow"]), realized_pnl

def fix_ticker_for_yahoo(xtb_ticker: str) -> str:
    if not isinstance(xtb_ticker, str): return xtb_ticker
    if xtb_ticker.endswith(".US"): return xtb_ticker.replace(".US", "")
    if xtb_ticker.endswith(".PL"): return xtb_ticker.replace(".PL", ".WA")
    if "." not in xtb_ticker: return f"{xtb_ticker}.WA"
    return xtb_ticker

@st.cache_data(ttl=600)
def fetch_market_and_fx_data(portfolio_df: pd.DataFrame):
    if portfolio_df.empty: return portfolio_df
    updated_portfolio = portfolio_df.copy()
    updated_portfolio["Yahoo_Ticker"] = updated_portfolio["Ticker"].apply(fix_ticker_for_yahoo)
    
    current_prices, currencies = {}, {}
    tickers_to_fetch = updated_portfolio["Yahoo_Ticker"].unique()
    
    for y_ticker in tickers_to_fetch:
        try:
            t = yf.Ticker(y_ticker)
            hist = t.history(period="1d", timeout=3)
            price = hist["Close"].iloc[-1] if not hist.empty else t.info.get("previousClose")
            currency = t.info.get("currency", "USD")
            if price is not None and currency in ["ILA", "GBX"] and y_ticker.endswith(".WA"):
                price /= 100.0
                currency = "PLN"
            current_prices[y_ticker] = price
            currencies[y_ticker] = currency
        except:
            current_prices[y_ticker], currencies[y_ticker] = None, "USD"

    updated_portfolio["Current_Price"] = updated_portfolio["Yahoo_Ticker"].map(current_prices)
    updated_portfolio["Asset_Currency"] = updated_portfolio["Yahoo_Ticker"].map(currencies).fillna("USD")
    updated_portfolio["Current_Value_Native"] = updated_portfolio["Shares_Owned"] * updated_portfolio["Current_Price"]
    
    unique_currencies = set(updated_portfolio["Asset_Currency"].dropna().unique()) - {"PLN"}
    fx_rates = {"PLN": 1.0}
    
    for curr in unique_currencies:
        try:
            fx_ticker = f"{curr}PLN=X"
            t_fx = yf.Ticker(fx_ticker)
            hist_fx = t_fx.history(period="1d", timeout=3)
            fx_rates[curr] = float(hist_fx["Close"].iloc[-1]) if not hist_fx.empty else (4.00 if curr == "USD" else 4.30)
        except:
            fx_rates[curr] = 4.00 if curr == "USD" else 4.30

    updated_portfolio["FX_Rate"] = updated_portfolio["Asset_Currency"].map(fx_rates).fillna(1.0)
    updated_portfolio["Current_Value_PLN"] = updated_portfolio["Current_Value_Native"] * updated_portfolio["FX_Rate"]
    return updated_portfolio

def calculate_cash_stats(df: pd.DataFrame) -> dict:
    deposits = df[df["Type"].isin(["Deposit", "Transfer"])]["Amount"].sum()
    div_gross = df[df["Type"] == "Dividend"]["Amount"].sum()
    wht_tax = df[df["Type"] == "Withholding tax"]["Amount"].sum()
    interest = df[df["Type"] == "Free funds interest"]["Amount"].sum()
    interest_tax = df[df["Type"] == "Free funds interest tax"]["Amount"].sum()
    return {
        "deposits": deposits,
        "dividends": div_gross + wht_tax,
        "interest": interest + interest_tax
    }

@st.cache_data(ttl=3600)
def generate_real_historical_timeline(df: pd.DataFrame) -> pd.DataFrame:
    """Generates a day-by-day timeline reconstructing cash + actual historical stock values."""
    df_sorted = df.copy()
    df_sorted["Time"] = pd.to_datetime(df_sorted["Time"]).dt.date
    df_sorted = df_sorted.sort_values(by="Time")
    
    start_date = df_sorted["Time"].min()
    end_date = df_sorted["Time"].max()
    all_days = pd.date_range(start=start_date, end=end_date).date
    
    # Wyciągamy ruchy akcji dzień po dniu
    asset_types = ["Stock purchase", "Stock sell"]
    trade_df = df_sorted[df_sorted["Type"].isin(asset_types)].copy()
    
    parsed_data = trade_df.apply(parse_xtb_comment, axis=1)
    trade_df["Volume_Adjusted"] = [x[0] if x is not None else None for x in parsed_data]
    trade_df["Yahoo_Ticker"] = trade_df["Ticker"].apply(fix_ticker_for_yahoo)
    
    unique_tickers = trade_df["Yahoo_Ticker"].dropna().unique()
    
    # Pobieramy ceny historyczne ze wszystich dni dla wykresu
    historical_prices = {}
    if len(unique_tickers) > 0:
        try:
            hist_data = yf.download(list(unique_tickers), start=start_date, end=end_date + pd.Timedelta(days=1), group_by='ticker', progress=False)
            for ticker in unique_tickers:
                if len(unique_tickers) == 1:
                    historical_prices[ticker] = hist_data["Close"]
                else:
                    historical_prices[ticker] = hist_data[ticker]["Close"]
        except:
            pass

    timeline_records = []
    
    # Rekonstrukcja pętli dzień po dniu
    for current_day in all_days:
        # 1. Obliczanie wolnej gotówki na ten konkretny dzień
        sub_df = df_sorted[df_sorted["Time"] <= current_day]
        cash_balance = sub_df["Amount"].sum()
        
        # 2. Obliczanie sumy wpłat do tego dnia
        total_deposits = sub_df[sub_df["Type"].isin(["Deposit", "Transfer"])]["Amount"].sum()
        
        # 3. Obliczanie wartości akcji na ten dzień
        sub_trades = trade_df[trade_df["Time"] <= current_day]
        stock_value_pln = 0.0
        
        if not sub_trades.empty:
            shares_per_ticker = sub_trades.groupby("Yahoo_Ticker")["Volume_Adjusted"].sum()
            for t_symbol, shares in shares_per_ticker.items():
                if shares > 0:
                    price = None
                    if t_symbol in historical_prices:
                        # Próbujemy znaleźć najbliższą cenę zamknięcia z przeszłości (pomijamy weekendy)
                        available_prices = historical_prices[t_symbol][:current_day]
                        if not available_prices.empty:
                            price = available_prices.iloc[-1]
                    
                    if pd.isna(price) or price is None:
                        # fallback do ceny transakcji
                        price = sub_trades[sub_trades["Yahoo_Ticker"] == t_symbol]["Price"].iloc[-1]
                    
                    # Prosty przelicznik walutowy (uproszczony dla historii)
                    fx_rate = 1.0
                    if t_symbol.endswith(".US"): fx_rate = 4.00
                    elif not t_symbol.endswith(".WA"): fx_rate = 4.30
                    
                    stock_value_pln += shares * price * fx_rate
                    
        # Całkowita wartość = wolna gotówka + wartość rynkowa posiadanych akcji
        total_portfolio_value = cash_balance + stock_value_pln
        
        timeline_records.append({
            "Time": current_day,
            "Wpłaty Rzeczywiste (Wkład)": total_deposits,
            "Całkowita Wartość Portfela": total_portfolio_value
        })
        
    return pd.DataFrame(timeline_records)

# --- UI EXECUTION FLOW ---
try:
    GOOGLE_DRIVE_FILE_ID = "1icRPA0GdmAXU-U-WF_65QD1RxAfSc8oH"
    DRIVE_DOWNLOAD_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_DRIVE_FILE_ID}/export?format=xlsx"

    with st.spinner("Pobieranie i precyzyjna analiza osi czasu Twojego portfela..."):
        response = requests.get(DRIVE_DOWNLOAD_URL, timeout=10)

    if response.status_code == 200:
        raw_df = pd.read_excel(io.BytesIO(response.content), skiprows=4)
        raw_df.columns = raw_df.columns.str.strip()
        clean_df = raw_df.dropna(subset=["ID"])

        # Obliczenia bazowe
        base_portfolio, realized_pnl = calculate_accurate_portfolio(clean_df)
        final_portfolio = fetch_market_and_fx_data(base_portfolio)
        cash = calculate_cash_stats(clean_df)
        timeline_df = generate_real_historical_timeline(clean_df)

        # Globalne Metryki
        total_value_stocks = final_portfolio["Current_Value_PLN"].sum() if not final_portfolio.empty else 0.0
        total_gain_pln = (total_value_stocks + cash["dividends"] + cash["interest"] + realized_pnl) - cash["deposits"]
        roi = (total_gain_pln / cash["deposits"]) * 100 if cash["deposits"] > 0 else 0

        # --- PANEL METRYK ---
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Wycena Akcji (PLN)", f"{total_value_stocks:,.2f} zł")
        col2.