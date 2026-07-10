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

def generate_fast_timeline(df: pd.DataFrame, current_portfolio_value: float) -> pd.DataFrame:
    """Generates a reliable timeline without heavy external web scraping."""
    df_sorted = df.copy()
    df_sorted["Time"] = pd.to_datetime(df_sorted["Time"]).dt.date
    df_sorted = df_sorted.sort_values(by="Time")
    
    daily = df_sorted.groupby("Time").agg(
        Daily_Amount=("Amount", "sum"),
        Daily_Deposits=("Amount", lambda x: x[df_sorted.loc[x.index, "Type"].isin(["Deposit", "Transfer"])].sum())
    ).reset_index()
    
    daily["Wpłaty Rzeczywiste (Wkład)"] = daily["Daily_Deposits"].cumsum()
    # Szacowana wartość oparta na bilansie księgowym wpłat/wypłat i transakcji
    daily["Wartość Księgowa Portfela"] = daily["Daily_Amount"].cumsum()
    
    # Żeby wykres kończył się aktualną wyceną rynkową:
    if not daily.empty:
        daily.loc[daily.index[-1], "Całkowita Wartość Portfela"] = current_portfolio_value + (daily.loc[daily.index[-1], "Wartość Księgowa Portfela"] - daily.loc[daily.index[-1], "Wpłaty Rzeczywiste (Wkład)"])
        daily["Całkowita Wartość Portfela"] = daily["Całkowita Wartość Portfela"].fillna(daily["Wartość Księgowa Portfela"])
    else:
        daily["Całkowita Wartość Portfela"] = daily["Wartość Księgowa Portfela"]
        
    return daily

# --- UI EXECUTION FLOW ---
try:
    GOOGLE_DRIVE_FILE_ID = "1icRPA0GdmAXU-U-WF_65QD1RxAfSc8oH"
    DRIVE_DOWNLOAD_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_DRIVE_FILE_ID}/export?format=xlsx"

    with st.spinner("Pobieranie i błyskawiczna analiza danych..."):
        response = requests.get(DRIVE_DOWNLOAD_URL, timeout=10)

    if response.status_code == 200:
        raw_df = pd.read_excel(io.BytesIO(response.content), skiprows=4)
        raw_df.columns = raw_df.columns.str.strip()
        clean_df = raw_df.dropna(subset=["ID"])

        base_portfolio, realized_pnl = calculate_accurate_portfolio(clean_df)
        final_portfolio = fetch_market_and_fx_data(base_portfolio)
        cash = calculate_cash_stats(clean_df)
        
        total_value_stocks = final_portfolio["Current_Value_PLN"].sum() if not final_portfolio.empty else 0.0
        timeline_df = generate_fast_timeline(clean_df, total_value_stocks)

        total_gain_pln = (total_value_stocks + cash["dividends"] + cash["interest"] + realized_pnl) - cash["deposits"]
        roi = (total_gain_pln / cash["deposits"]) * 100 if cash["deposits"] > 0 else 0

        # --- PANEL METRYK ---
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Wycena Akcji (PLN)", f"{total_value_stocks:,.2f} zł")
        col2.metric("Suma Twoich Wpłat", f"{cash['deposits']:,.2f} zł")
        col3.metric("Zrealizowany Zysk 🟢", f"{realized_pnl:,.2f} zł")
        col