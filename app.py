import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import yfinance as yf
import logging
import re
import io
import requests

# --- KONFIGURACJA ---
st.set_page_config(page_title="XTB Dashboard PLN", page_icon="💰", layout="wide")

st.title("💰 Prywatny Dashboard Finansowy XTB (Portfel PLN)")
st.markdown("---")

# --- LOGIKA OBLICZENIOWA ---
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

def fix_ticker_for_yahoo(xtb_ticker: str) -> str:
    if not isinstance(xtb_ticker, str): return xtb_ticker
    if xtb_ticker.endswith(".US"): return xtb_ticker.replace(".US", "")
    if xtb_ticker.endswith(".PL"): return xtb_ticker.replace(".PL", ".WA")
    if "." not in xtb_ticker: return f"{xtb_ticker}.WA"
    return xtb_ticker

def calculate_accurate_portfolio(df_closed: pd.DataFrame, df_cash: pd.DataFrame) -> tuple:
    realized_pnl_stocks = df_closed["Profit/Loss"].sum() if "Profit/Loss" in df_closed.columns else 0.0
    
    other_gains = df_cash[df_cash["Type"].isin([
        "Dividend", "Withholding tax", "Free funds interest", "Free funds interest tax"
    ])]["Amount"].sum()
    
    total_realized_pnl = realized_pnl_stocks + other_gains
    
    asset_types = ["Stock purchase", "Stock sell"]
    trade_df = df_cash[df_cash["Type"].isin(asset_types)].copy()
    
    if trade_df.empty:
        return pd.DataFrame(), round(total_realized_pnl, 2)
        
    parsed_data = trade_df.apply(parse_xtb_comment, axis=1)
    trade_df["Volume_Adjusted"] = [x[0] if x is not None else None for x in parsed_data]
    
    portfolio = trade_df.groupby("Ticker").agg(
        Shares_Owned=("Volume_Adjusted", "sum"),
        Net_Cash_Flow=("Amount", "sum")
    ).reset_index()
    
    portfolio["Shares_Owned"] = portfolio["Shares_Owned"].round(6)
    active_portfolio = portfolio[portfolio["Shares_Owned"] > 0.0001].copy()
    active_portfolio["Total_Invested_Raw"] = -active_portfolio["Net_Cash_Flow"]

    return active_portfolio.drop(columns=["Net_Cash_Flow"]), round(total_realized_pnl, 2)

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
            price = hist["Close"].iloc[-1] if not hist.empty else (t.info.get("previousClose") or 0.0)
            currency = t.info.get("currency", "USD")
            if price is not None and currency in ["ILA", "GBX"] and y_ticker.endswith(".WA"):
                price /= 100.0
                currency = "PLN"
            current_prices[y_ticker] = price
            currencies[y_ticker] = currency
        except Exception:
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
            fx_rates[curr] = float(hist_fx["Close"].iloc[-1]) if not hist_fx.empty else 4.00
        except:
            fx_rates[curr] = 4.00

    updated_portfolio["FX_Rate"] = updated_portfolio["Asset_Currency"].map(fx_rates).fillna(1.0)
    updated_portfolio["Current_Value_PLN"] = updated_portfolio["Current_Value_Native"] * updated_portfolio["FX_Rate"]
    return updated_portfolio

@st.cache_data(ttl=3600)
def generate_weekly_historical_timeline(df_cash: pd.DataFrame) -> pd.DataFrame:
    df_sorted = df_cash.copy()
    df_sorted["Time"] = pd.to_datetime(df_sorted["Time"])
    df_sorted = df_sorted.sort_values(by="Time")
    
    if df_sorted.empty: return pd.DataFrame()
    
    start_date = df_sorted["Time"].min().date()
    end_date = df_sorted["Time"].max().date()
    
    weekly_range = pd.date_range(start=start_date, end=end_date, freq="W-FRI").date
    if len(weekly_range) == 0 or weekly_range[-1] < end_date:
        weekly_range = list(weekly_range) + [end_date]

    asset_types = ["Stock purchase", "Stock sell"]
    trade_df = df_sorted[df_sorted["Type"].isin(asset_types)].copy()
    
    if not trade_df.empty:
        parsed_data = trade_df.apply(parse_xtb_comment, axis=1)
        trade_df["Volume_Adjusted"] = [x[0] if x is not None else None for x in parsed_data]
        trade_df["Yahoo_Ticker"] = trade_df["Ticker"].apply(fix_ticker_for_yahoo)
    
    unique_tickers = trade_df["Yahoo_Ticker"].dropna().unique() if not trade_df.empty else []
    
    weekly_prices = {}
    if len(unique_tickers) > 0:
        try:
            hist_data = yf.