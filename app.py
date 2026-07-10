import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import yfinance as yf
import logging
import io
import requests

# --- KONFIGURACJA ---
st.set_page_config(page_title="XTB Dashboard", page_icon="💰", layout="wide")
st.title("💰 Prywatny Dashboard Finansowy XTB")

# --- LOGIKA OBLICZENIOWA ---
def calculate_accurate_portfolio(df_closed: pd.DataFrame, df_cash: pd.DataFrame) -> tuple:
    # 1. Zrealizowany zysk = Zysk z pozycji + Dywidendy - Podatki + Odsetki
    realized_pnl_stocks = df_closed["Profit/Loss"].sum()
    
    other_gains = df_cash[df_cash["Type"].isin([
        "Dividend", "Withholding tax", "Free funds interest", "Free funds interest tax"
    ])]["Amount"].sum()
    
    total_realized_pnl = realized_pnl_stocks + other_gains
    
    # 2. Aktywne pozycje (na podstawie Stock purchase)
    # Grupowanie zakupów per Ticker
    active_df = df_cash[df_cash["Type"] == "Stock purchase"].groupby("Ticker").agg(
        Net_Cash_Flow=("Amount", "sum")
    ).reset_index()
    
    # Inwestycja to suma ujemnych przepływów (zakupów)
    active_portfolio = active_df[active_df["Net_Cash_Flow"] < 0].copy()
    active_portfolio["Total_Invested_Raw"] = -active_portfolio["Net_Cash_Flow"]
    active_portfolio = active_portfolio.rename(columns={"Ticker": "Ticker"})
    
    return active_portfolio, round(total_realized_pnl, 2)

# --- UI I WCZYTYWANIE DANYCH ---
try:
    # Zakładamy, że masz plik XLSX z dwiema zakładkami: 'Closed Positions' i 'Cash Operations'
    # Zmień link na swój URL pliku lub użyj lokalnej ścieżki
    GOOGLE_DRIVE_FILE_ID = "1icRPA0GdmAXU-U-WF_65QD1RxAfSc8oH"
    DRIVE_DOWNLOAD_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_DRIVE_FILE_ID}/export?format=xlsx"

    with st.spinner("Pobieranie i analiza raportów..."):
        response = requests.get(DRIVE_DOWNLOAD_URL, timeout=10)
        
        # Wczytanie dwóch różnych zakładek
        df_closed = pd.read_excel(io.BytesIO(response.content), sheet_name="Closed Positions", skiprows=1)
        df_cash = pd.read_excel(io.BytesIO(response.content), sheet_name="Cash Operations", skiprows=1)
        
        # Czyszczenie nagłówków
        df_closed.columns = df_closed.columns.str.strip()
        df_cash.columns = df_cash.columns.str.strip()

        # Obliczenia
        active_portfolio, realized_pnl = calculate_accurate_portfolio(df_closed, df_cash)

        # --- PANEL METRYK ---
        col1, col2, col3 = st.columns(3)
        col1.metric("Zrealizowany Zysk (zgodny z XTB)", f"{realized_pnl:,.2f} zł")
        
        st.subheader("📋 Twoje aktywne inwestycje (podsumowanie)")
        st.dataframe(active_portfolio, use_container_width=True)

except Exception as e:
    st.error(f"Błąd podczas generowania dashboardu: {e}")